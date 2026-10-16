"""`conductor finalize` - Jira housekeeping after a release has shipped.

The final step of the release pipeline. Run after production is
synced and verified (`conductor cluster sync --wait`). Performs four operations on
the release's Jira version (the fourth is skippable with --no-shift-labels):

  1. Strip ` (CURRENT)` from the version name (e.g. "2026-05-28 (CURRENT)"
     becomes "2026-05-28"). Skipped if the suffix isn't present.

  2. Mark the version released, with releaseDate = the cycle date from the
     manifest's release_label. Equivalent to the "Release" button in the UI
     with "Ignore unresolved work items" selected - no moveUnfixedIssuesTo
     is passed, so any tickets still associated with this fixVersion stay
     associated.

  3. Close any Bug-type tickets in this release that aren't already Closed.
     Bug tickets sometimes get stuck at "Ready for Release" instead of auto-
     transitioning to Closed/Fixed; this step finishes the transition manually
     with resolution = Fixed. A Bug's OPEN subtasks are closed first (Jira gates a
     parent on all-subtasks-resolved; a ticket in the release train is sign-off
     that its subtasks are done) so the parent can then close - opt out with
     --no-close-subtasks. If a bug's workflow offers no direct Closed transition
     even after that (e.g. "Ready for Release" whose only move is "Back to In
     Review"), it's reported as a manual follow-up - NOT a failure, since
     re-running can't fix a workflow limitation.

  4. Shift future-cycle labels one slot forward to reflect the new pipeline
     position: (NEXT) → (CURRENT), (OVERNEXT) → (NEXT), (SUBSEQUENT) →
     (OVERNEXT). Each rename is independent: missing labels are simply
     skipped. Disable with --no-shift-labels if you'd rather do this by hand.

  5. Close any remaining release-ticket subtasks (the finalize subtask itself,
     lingering manual steps, etc.). Finalize is the end of the train, so
     whatever's still Open/In Progress gets wrapped up - complementing the
     per-step close-the-prior-prefix that runs during the pipeline.

  6. Create any missing future versions so the CURRENT → NEXT → OVERNEXT →
     SUBSEQUENT ladder stays four deep. The label shift (step 4) vacates the
     SUBSEQUENT slot; this fills it (and any other gap) on the release train's
     Tuesday/Thursday cadence - each new version's release date is its slot date
     and its start date is the day the prior release goes out (= the previous
     slot's release date). Existing versions are never modified, and a date
     already present is skipped. Disable with --no-extend-ladder; also skipped
     when --no-shift-labels is set (you're managing the ladder by hand).

  7. Clone the "[Release] Release Steps ... [TEMPLATE]" ticket (REL-47502) for
     each train created in step 6 - a parent Task + its subtasks, summary
     date-swapped. Keeps every new version paired with its checklist ticket.
     Skipped for a date that already has one (no duplicates). Disable with
     --no-release-steps. See `conductor release steps` for the standalone version.

  8. Promote this cycle's GitHub release tags from pre-release to full/latest.
     `release tag` cuts them as pre-releases; once the release has shipped they
     should be the repo's "latest". Flips `prerelease=false` + `make_latest=true`
     on each tagged platform/service repo's release. Skips ones already promoted
     or without a Release object. Needs a GitHub token (skipped if absent).
     Disable with --no-release-latest.

  9. Close the "[Release] Release Steps …" parent ticket itself, after its
     subtasks (Jira gates the parent on subtask resolution).

Also surfaces a reminder to publish a service article (a support requirement)
when any pre-bump service-* repo shipped this cycle - surface-only, never actioned.

Idempotent: re-running is safe (steps 4–7 that ADVANCE the train only fire while
this version is still (CURRENT); steps 1–3, 8, 9 are inherently idempotent). If the version is already named correctly,
already released, or a ticket is already Closed, those steps are skipped.

Defaults to a dry-run preview unless --apply is passed.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import config, github_client, jira_client, jira_progress, manifest as M
from . import release_steps


CURRENT_SUFFIX = " (CURRENT)"

# Keys of the open tickets the last `run()` found whose work didn't ship (an
# unmerged PR, or no merged PR at all). The menu reads this after its preview to
# ask whether to move them before it applies.
_last_unshipped: List[str] = []
TARGET_RESOLUTION = "Fixed"
TARGET_STATUS_NAME = "Closed"  # the status we want bugs to land in

# The full future ladder, earliest→latest. Every finalize keeps all four
# populated: labels shift forward one slot, and any slot left empty is created.
LADDER_SUFFIXES: List[str] = [
    " (CURRENT)", " (NEXT)", " (OVERNEXT)", " (SUBSEQUENT)",
]

# The release train runs every Tuesday and Thursday.
_TRAIN_WEEKDAYS = (1, 3)  # Monday=0 … so Tue=1, Thu=3


def _next_train_date(d: date) -> date:
    """The next release-train date strictly after `d` (Tue→Thu, Thu→next Tue)."""
    for i in range(1, 8):
        nd = d + timedelta(days=i)
        if nd.weekday() in _TRAIN_WEEKDAYS:
            return nd
    return d  # unreachable - a train day always falls within 7 days


def _prev_train_date(d: date) -> date:
    """The release-train date immediately before `d`. A version's START date is
    the day the *prior* release goes out, which is exactly this."""
    for i in range(1, 8):
        nd = d - timedelta(days=i)
        if nd.weekday() in _TRAIN_WEEKDAYS:
            return nd
    return d  # unreachable


def _parse_release_date(name: str) -> Optional[date]:
    """Parse the leading `YYYY-MM-DD` from a version name (any ladder suffix
    stripped). Returns None if the name doesn't start with an ISO date."""
    base = name or ""
    for suf in LADDER_SUFFIXES:
        if base.endswith(suf):
            base = base[: -len(suf)]
            break
    base = base.strip()
    try:
        return date.fromisoformat(base[:10])
    except ValueError:
        return None


def _plan_ladder_creations(
    versions: List[Dict[str, Any]],
    shifts: List[Tuple[str, str, str]],
    released_date: Optional[date],
) -> List[Tuple[str, str, str]]:
    """Plan the versions to CREATE so the CURRENT→NEXT→OVERNEXT→SUBSEQUENT ladder
    is complete AFTER the label shift. Returns [(name, start_iso, release_iso)].

    The shift vacates the SUBSEQUENT slot (and any slot whose feeder was missing);
    we fill every empty slot by walking the ladder in order, chaining each new
    date off the previous slot's date on the Tue/Thu cadence. A created version's
    release date is its slot date; its start date is the prior train date (= the
    previous slot's release date). Existing versions are never touched, and a
    date already present on any version is skipped (no duplicates).
    """
    # Where each ladder suffix lands after the shift (from the shift targets).
    occ: Dict[str, Optional[date]] = {}
    for _vid, _old, new in shifts:
        for suf in LADDER_SUFFIXES:
            if new.endswith(suf):
                occ[suf] = _parse_release_date(new)
                break

    existing_dates = {
        d for v in versions if not v.get("archived")
        for d in [_parse_release_date(v.get("name") or "")] if d
    }

    creations: List[Tuple[str, str, str]] = []
    prev_date: Optional[date] = None
    for suf in LADDER_SUFFIXES:
        if suf in occ:                 # slot filled by the shift - don't create
            prev_date = occ[suf]       # chain from it (may be None if unparseable)
            continue
        if prev_date is not None:
            cur = _next_train_date(prev_date)
        elif released_date is not None:
            cur = _next_train_date(released_date)  # empty CURRENT → next train out
        else:
            prev_date = None           # can't anchor; leave the rest alone
            continue
        if cur not in existing_dates:  # dedupe against any already-present date
            creations.append(
                (cur.isoformat() + suf, _prev_train_date(cur).isoformat(), cur.isoformat())
            )
            existing_dates.add(cur)
        prev_date = cur
    return creations


def _strip_current(name: str) -> str:
    """Remove a trailing ` (CURRENT)` (case-insensitive) if present."""
    if name.lower().endswith(CURRENT_SUFFIX.lower()):
        return name[: -len(CURRENT_SUFFIX)]
    return name


def _ladder_suffix(name: str) -> Optional[str]:
    """The ladder suffix a version name carries (' (CURRENT)' …), or None."""
    for suf in LADDER_SUFFIXES:
        if name.endswith(suf):
            return suf
    return None


def _plan_label_shifts(
    versions: List[Dict[str, Any]],
    skip_version_id: Optional[str] = None,
    after_date: Optional[date] = None,
) -> List[Tuple[str, str, str]]:
    """Assign CURRENT/NEXT/OVERNEXT/SUBSEQUENT to the active laddered versions in
    DATE order, planning a rename for each whose current suffix is wrong. Returns
    [(version_id, old_name, new_name)] in ladder order.

    Date-anchored, NOT label-anchored: the earliest future train always becomes
    (CURRENT), the next (NEXT), and so on - so a DEGRADED ladder self-heals. The
    old label-anchored logic promoted each rung one slot by matching its existing
    suffix; when a rung was missing (e.g. no (NEXT)), the shift left NO (CURRENT)
    at all - which is exactly how a stale, archived `2026-06-25 (CURRENT)` ended
    up being the only current-looking version. Anchoring by date can't leave that
    gap.

    Only versions that already carry a ladder suffix are reshuffled (a stray
    future version without one is never relabeled). `skip_version_id` drops this
    release's own version (just stripped); `after_date` (the shipped release
    date) drops any stale past-dated laddered version so it can't grab (CURRENT).
    """
    laddered: List[Tuple[date, str, str, str]] = []  # (date, id, name, suffix)
    for v in versions:
        if v.get("archived") or v.get("released"):
            continue
        vid = str(v.get("id"))
        if skip_version_id and vid == str(skip_version_id):
            continue
        name = v.get("name") or ""
        suf = _ladder_suffix(name)
        if not suf:
            continue
        d = _parse_release_date(name)
        if d is None or (after_date is not None and d <= after_date):
            continue
        laddered.append((d, vid, name, suf))

    laddered.sort(key=lambda t: t[0])
    shifts: List[Tuple[str, str, str]] = []
    for idx, (_d, vid, name, suf) in enumerate(laddered):
        if idx >= len(LADDER_SUFFIXES):
            break  # beyond SUBSEQUENT - leave any extra laddered versions alone
        target = LADDER_SUFFIXES[idx]
        if suf != target:
            shifts.append((vid, name, name[: -len(suf)] + target))
    return shifts


def _plan_release_latest(
    mf: M.Manifest, gh_token: str,
) -> List[Tuple[str, str, Optional[str], str]]:
    """For each tagged platform/service repo, look up its GitHub release and plan
    to promote the pre-release to the repo's full/latest release. Returns
    [(repo, tag, release_id, state)] where state is:
      'flip'    - currently a pre-release → promote it,
      'already' - already a full/latest release,
      'missing' - no GitHub release object at that tag.
    Best-effort; one GET per repo."""
    plans: List[Tuple[str, str, Optional[str], str]] = []
    for r in mf.repos:
        if not r.release_tag or r.classification not in ("platform", "service"):
            continue
        rel = github_client.get_release_by_tag(r.repo, r.release_tag, gh_token)
        if not rel:
            plans.append((r.repo, r.release_tag, None, "missing"))
        elif rel.get("prerelease"):
            plans.append((r.repo, r.release_tag, str(rel.get("id")), "flip"))
        else:
            plans.append((r.repo, r.release_tag, str(rel.get("id")), "already"))
    return plans


def _pick_close_transition(
    transitions: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Pick the transition whose target status is Closed (case-insensitive).

    Returns None if no such transition exists from the current status (e.g.
    the issue is in a state from which Close isn't reachable).
    """
    for t in transitions:
        to_name = ((t.get("to") or {}).get("name") or "").strip()
        if to_name.lower() == TARGET_STATUS_NAME.lower():
            return t
    return None


def _issue_type_name(issue: Dict[str, Any]) -> str:
    return ((issue.get("fields") or {}).get("issuetype") or {}).get("name", "") or ""


def _issue_status_name(issue: Dict[str, Any]) -> str:
    return ((issue.get("fields") or {}).get("status") or {}).get("name", "") or ""


def _issue_resolution_name(issue: Dict[str, Any]) -> str:
    res = (issue.get("fields") or {}).get("resolution")
    return (res or {}).get("name", "") if res else ""


def _closing_blockers(key: str, token: str) -> List[str]:
    """Explain WHY a ticket has no Close transition: open subtasks (Jira's
    all-subtasks-resolved gate) and linked work items for context. Surface-only -
    we never close these (a subtask is same-project, but linked items are often
    other teams' tickets; see the no-cross-project-close rule). Returns display
    lines; empty if nothing informative or the lookup fails."""
    try:
        issue = jira_client.fetch_issue(key, token, fields=["subtasks", "issuelinks"])
    except Exception:
        return []
    f = issue.get("fields") or {}
    lines: List[str] = []

    open_subs = []
    for st in (f.get("subtasks") or []):
        sstat = ((st.get("fields") or {}).get("status") or {}).get("name") or ""
        if sstat.lower() != TARGET_STATUS_NAME.lower():
            open_subs.append(f"{st.get('key')} [{sstat}]")
    if open_subs:
        # Open subtasks are the usual hard gate - Jira won't offer the parent a
        # resolve transition until they're closed.
        lines.append(f"Blocked by open subtask(s): {', '.join(open_subs)}")

    link_strs = []
    for l in (f.get("issuelinks") or []):
        typ = (l.get("type") or {}).get("name") or "linked"
        other = l.get("inwardIssue") or l.get("outwardIssue") or {}
        okey = other.get("key")
        ostat = ((other.get("fields") or {}).get("status") or {}).get("name") or ""
        if okey:
            link_strs.append(f"{okey} [{ostat}] ({typ})")
    if link_strs:
        lines.append(f"Linked (context, may not block): {', '.join(link_strs)}")

    return lines


def _release_pin_if_this_manifest(manifest_path: Path, label: str) -> None:
    """Clear the active pin when it points at `manifest_path` - the release we
    just finalized (or confirmed already finalized). Otherwise the menu's Refresh
    and `manifest build current` keep rebuilding this DONE cycle instead of
    advancing to the new CURRENT. A pin on a *different* in-flight release is left
    alone. Best-effort - never fails finalize over the pin."""
    try:
        active = M.get_active_path()
        if active and active.resolve() == manifest_path.resolve():
            M.clear_active()
            print(f"  ↪ released the active pin ({label}) - Refresh / "
                  f"`manifest build current` now moves to the new CURRENT cycle")
    except Exception:
        pass


def run(args) -> int:
    # 1. Load manifest
    if args.manifest:
        manifest_path = Path(args.manifest).expanduser().resolve()
    else:
        manifest_path = M.default_manifest_path()
        if not manifest_path:
            sys.exit("no manifest in ~/.conductor/state/; run `conductor manifest build` first")
        print(f"  (using latest manifest: {manifest_path.name})")
    mf = M.read_manifest(manifest_path)
    version_id = mf.release_version_id
    release_label = mf.release_label

    apply_changes = bool(args.apply)
    label = "" if apply_changes else "  [dry-run]"
    print(f"\nFinalize Jira for release {release_label}{label}")
    print(f"  version_id: {version_id}")

    # 2. Auth
    jira_token = config.get_jira_token()
    config.get_jira_email()

    # 3. Inspect current version state
    try:
        version = jira_client.fetch_version(version_id, jira_token)
    except Exception as e:
        sys.exit(f"error fetching version {version_id}: {e}")
    current_name = version.get("name", "") or ""
    already_released = bool(version.get("released"))
    current_release_date = version.get("releaseDate") or ""

    # 4. Plan: rename
    plan_rename: Optional[Tuple[str, str]] = None
    if current_name.lower().endswith(CURRENT_SUFFIX.lower()):
        new_name = _strip_current(current_name)
        plan_rename = (current_name, new_name)

    # 5. Plan: release
    plan_release: Optional[Tuple[bool, str]] = None
    if not already_released:
        plan_release = (True, release_label)
    elif current_release_date != release_label:
        # Already released but with a different date - surface as info, don't
        # overwrite a manually-set date.
        print(f"  · version already released on {current_release_date} (not changing)")

    # 6. Plan: close unfinished Bug tickets
    print("→ scanning Bug tickets in this release for unclosed ones…")
    try:
        issues = jira_client.fetch_version_issues(
            version_id, jira_token,
            fields=["summary", "status", "issuetype", "resolution"],
        )
    except Exception as e:
        sys.exit(f"error fetching issues for version {version_id}: {e}")

    bugs_to_close: List[Tuple[str, str, str]] = []  # (key, summary, status_name)
    bugs_already_closed = 0
    other_open_bugs: List[Tuple[str, str]] = []  # (key, status) - couldn't find transition

    for issue in issues:
        if _issue_type_name(issue).lower() != "bug":
            continue
        status = _issue_status_name(issue)
        if status.lower() == TARGET_STATUS_NAME.lower():
            bugs_already_closed += 1
            continue
        key = issue.get("key", "")
        summary = ((issue.get("fields") or {}).get("summary") or "")[:60]
        bugs_to_close.append((key, summary, status))

    # 6a-2. Plan: close each unclosed Bug's OPEN subtasks first. Jira gates a parent
    # on all-subtasks-resolved, so a Bug like REL-51429 (subtasks REL-52587/52588
    # still Open) is offered no Closed transition until they're done. Policy: a ticket
    # in the release train is sign-off that its subtasks are complete, so finalize
    # closes them as part of the sweep (they're same-project subtasks - inside the
    # never-touch-cross-project rule). Opt out with --no-close-subtasks. One fetch
    # per unclosed bug; skipped entirely when there are none.
    close_subtasks_enabled = not getattr(args, "no_close_subtasks", False)
    bug_open_subtasks: Dict[str, List[Tuple[str, str, str]]] = {}  # bug → [(sub,summary,status)]
    if close_subtasks_enabled:
        for key, _summary, _status in bugs_to_close:
            try:
                subs = jira_client.fetch_subtasks(key, jira_token)
            except (Exception, SystemExit):
                continue
            open_subs = []
            for s in subs:
                sf = s.get("fields") or {}
                sstat = (sf.get("status") or {}).get("name") or ""
                if sstat.lower() != TARGET_STATUS_NAME.lower():
                    open_subs.append((s.get("key"), (sf.get("summary") or "")[:50], sstat))
            if open_subs:
                bug_open_subtasks[key] = open_subs

    # 6a-3. Plan: other tickets still open on this version. Releasing it leaves them
    # attached ("ignore unresolved"), so name them, and single out the ones whose
    # work didn't ship (an unmerged PR, or no merged PR found) - those belong on the
    # next train. Bugs are handled above; the release-steps ticket closes below.
    global _last_unshipped
    release_ticket_key = jira_progress._release_ticket(mf)  # noqa: SLF001 - same package
    by_key = {t.key: t for t in mf.tickets}
    leftovers: List[Tuple[str, str, str, bool, str]] = []  # (key, summary, status, unshipped, why)
    for issue in issues:
        key = issue.get("key", "")
        status = _issue_status_name(issue)
        if (_issue_type_name(issue).lower() == "bug" or key == release_ticket_key
                or status.lower() == TARGET_STATUS_NAME.lower()):
            continue
        summary = ((issue.get("fields") or {}).get("summary") or "")[:60]
        t = by_key.get(key)
        if t is None:
            leftovers.append((key, summary, status, False, "not in the manifest; refresh to check"))
        elif t.unmerged_prs:
            n = len(t.unmerged_prs)
            leftovers.append((key, summary, status, True, f"{n} unmerged PR{'s' if n != 1 else ''}"))
        elif not t.pr_urls:
            leftovers.append((key, summary, status, True, "no merged PR found"))
        else:
            leftovers.append((key, summary, status, False, "merged PR(s) shipped"))
    unshipped = [k for k, _s, _st, u, _w in leftovers if u]
    _last_unshipped = list(unshipped)
    move_unshipped = getattr(args, "move_unshipped", None)  # True / False / None = ask

    # 6b. Plan: shift future-cycle labels (NEXT → CURRENT, OVERNEXT → NEXT, etc.)
    # and then create any missing ladder slots so CURRENT/NEXT/OVERNEXT/SUBSEQUENT
    # stay fully populated. Both need the project's versions - fetch once.
    #
    # IDEMPOTENCY: advancing the train is the counterpart of stripping THIS
    # version's (CURRENT) suffix - they must happen together, ONCE. If the rename
    # is a no-op (this version was already stripped, i.e. finalize already ran),
    # re-shifting would advance the ladder a SECOND time (NEXT→CURRENT again) and
    # create an extra version. So the shift only fires when we're also renaming
    # this run (plan_rename set). `already_finalized` drives the skip message.
    # Gated additionally by --no-shift-labels for manual control.
    shifts: List[Tuple[str, str, str]] = []
    ladder_creations: List[Tuple[str, str, str]] = []
    project_id = version.get("projectId")
    already_finalized = plan_rename is None and already_released
    advancing = (not getattr(args, "no_shift_labels", False)) and (plan_rename is not None)
    if advancing:
        try:
            all_versions = jira_client.fetch_project_versions(config.JIRA_PROJECT, jira_token)
        except Exception:
            all_versions = []
        shifts = _plan_label_shifts(
            all_versions, skip_version_id=version_id,
            after_date=_parse_release_date(current_name),
        )
        if not getattr(args, "no_extend_ladder", False):
            ladder_creations = _plan_ladder_creations(
                all_versions, shifts, _parse_release_date(current_name),
            )

    # 6c-2. Plan: clone the [Release] Release Steps ticket for each newly-created
    # train, so every new version gets its checklist ticket in lockstep. Only for
    # the versions we're creating (existing trains already have theirs).
    # release_steps_plan: [(date, new_summary, n_subtasks, existing_keys)].
    release_steps_plan: List[Tuple[str, str, int, List[str]]] = []
    rs_tmpl = rs_subs = None
    if ladder_creations and not getattr(args, "no_release_steps", False):
        try:
            rs_tmpl, rs_subs = release_steps.fetch_template(jira_token)
        except Exception:
            rs_tmpl = None
        if rs_tmpl:
            tmpl_summary = (rs_tmpl.get("fields") or {}).get("summary") or ""
            for _name, _start, rel in ladder_creations:
                release_steps_plan.append((
                    rel, release_steps._new_summary(tmpl_summary, rel),
                    len(rs_subs or []), release_steps.existing_for_date(rel, jira_token),
                ))

    # 6c-3. Plan: promote this cycle's GitHub pre-release tags to full/latest.
    # Tags are cut as pre-releases (release_tag); finalize is when the release has
    # shipped, so flip them to "latest". Needs a GitHub token - optional here, so
    # resolve without the fatal path and skip gracefully if absent.
    release_latest_plan: List[Tuple[str, str, Optional[str], str]] = []
    gh_token: Optional[str] = None
    if not getattr(args, "no_release_latest", False):
        try:
            gh_token = config.get_github_token()
        except SystemExit:
            gh_token = None
        if gh_token:
            release_latest_plan = _plan_release_latest(mf, gh_token)
    release_latest_flips = [p for p in release_latest_plan if p[3] == "flip"]

    # 6c. Plan: close any remaining release-ticket subtasks. Finalize is the end
    # of the train - anything still Open/In Progress (the finalize subtask itself,
    # a lingering manual step, etc.) gets wrapped up. In-flow closing complements
    # the per-step close-the-prior-prefix that runs during the pipeline.
    open_subtasks = jira_progress.open_release_subtasks(mf, jira_token)

    # 6d. Plan: close the release-steps PARENT ticket itself. Once its subtasks are
    # all done, the "[Release] Release Steps …" ticket should close too. (Closed
    # after the subtasks below, since Jira gates the parent on subtask resolution.)
    release_ticket_close: Optional[Tuple[str, str]] = None
    if release_ticket_key:
        try:
            rt = jira_client.fetch_issue(release_ticket_key, jira_token, fields=["status"])
            rt_status = ((rt.get("fields") or {}).get("status") or {}).get("name") or ""
            if rt_status.lower() != TARGET_STATUS_NAME.lower():
                release_ticket_close = (release_ticket_key, rt_status)
        except Exception:
            pass

    # 6e. Reminder: a pre-bump service-* repo released this cycle needs a published
    # service article (support requirement). Surface-only - never actioned here.
    from . import article as _article
    service_articles = [] if _article.published_url(mf) else _article.needs_article(mf)

    # 7. Print the plan
    print()
    print("Plan:")
    if plan_rename:
        old, new = plan_rename
        print(f"  • rename version: '{old}' → '{new}'")
    else:
        print(f"  · rename version: skipped (current name '{current_name}' has no ' (CURRENT)' suffix)")
    if plan_release:
        _, date = plan_release
        print(f"  • mark released (releaseDate={date}, ignoring unresolved items)")
    else:
        print(f"  · mark released: skipped (already released)")

    if bugs_to_close:
        print(f"  • close {len(bugs_to_close)} Bug ticket(s) with resolution={TARGET_RESOLUTION}:")
        for key, summary, status in bugs_to_close:
            print(f"      {key:15s} [{status}]  {summary}")
            for sub_key, sub_summary, sub_status in bug_open_subtasks.get(key, []):
                print(f"        ↳ first close subtask {sub_key} [{sub_status}]  {sub_summary}")
    else:
        print(f"  · close Bug tickets: nothing to do")
    if bug_open_subtasks:
        n_subs = sum(len(v) for v in bug_open_subtasks.values())
        print(f"  · ({n_subs} open subtask(s) will be closed first so the parent(s) can close"
              f"{' - --no-close-subtasks to skip' if not already_finalized else ''})")
    if bugs_already_closed:
        print(f"  · ({bugs_already_closed} Bug ticket(s) already Closed)")

    if leftovers:
        print(f"  · {len(leftovers)} other open ticket(s) on this version:")
        for key, summary, status, u, why in leftovers:
            print(f"      {key:15s} [{status}]  {summary}  ({why})")
        if unshipped:
            n = len(unshipped)
            when = ("yes (--move-unshipped)" if move_unshipped is True
                    else "no, they stay" if move_unshipped is False
                    else "asked before any change (or pass --move-unshipped)")
            print(f"  • move the {n} whose work didn't ship to the new (CURRENT) train: {when}")

    if already_finalized:
        print(f"  · advance the train (shift labels + extend ladder + steps): skipped -")
        print(f"      this version is already finalized (not '(CURRENT)'); re-running "
              f"won't re-advance the train.")
    elif getattr(args, "no_shift_labels", False):
        print(f"  · shift future-cycle labels: skipped (--no-shift-labels)")
    elif shifts:
        print(f"  • shift {len(shifts)} future-cycle label(s):")
        for _vid, old, new in shifts:
            print(f"      '{old}' → '{new}'")
    else:
        print(f"  · shift future-cycle labels: nothing to shift")

    if already_finalized:
        pass  # covered by the combined note above
    elif getattr(args, "no_shift_labels", False) or getattr(args, "no_extend_ladder", False):
        if not getattr(args, "no_shift_labels", False):
            print(f"  · create missing future versions: skipped (--no-extend-ladder)")
    elif ladder_creations:
        print(f"  • create {len(ladder_creations)} missing future version(s) (Tue/Thu cadence):")
        for name, start, rel in ladder_creations:
            print(f"      '{name}'  start={start}  release={rel}")
    else:
        print(f"  · create missing future versions: ladder already complete")

    if already_finalized:
        pass  # covered by the combined note above
    elif getattr(args, "no_release_steps", False):
        print(f"  · clone release-steps tickets: skipped (--no-release-steps)")
    elif release_steps_plan:
        to_make = [p for p in release_steps_plan if not p[3]]
        if to_make:
            print(f"  • clone {len(to_make)} release-steps ticket(s) (Task + subtasks):")
        for rel, summ, n_sub, existing in release_steps_plan:
            if existing:
                print(f"      · {rel}: already exists ({', '.join(existing)}) - skip")
            else:
                print(f"      '{summ}' (+{n_sub} subtasks)")
    elif ladder_creations:
        # ladder made versions but template fetch failed
        print(f"  · clone release-steps tickets: template unavailable - skipped")

    if getattr(args, "no_release_latest", False):
        print(f"  · promote pre-releases to latest: skipped (--no-release-latest)")
    elif gh_token is None:
        print(f"  · promote pre-releases to latest: skipped (no GitHub token)")
    elif release_latest_flips:
        print(f"  • promote {len(release_latest_flips)} GitHub pre-release(s) to latest:")
        for repo, tag, _id, _st in release_latest_flips:
            print(f"      {repo} {tag}: Pre-release → latest")
        _already = sum(1 for p in release_latest_plan if p[3] == "already")
        _missing = [p[0] for p in release_latest_plan if p[3] == "missing"]
        if _already:
            print(f"      ({_already} already latest)")
        if _missing:
            print(f"      (no GitHub release found: {', '.join(_missing)})")
    else:
        _missing = [p[0] for p in release_latest_plan if p[3] == "missing"]
        note = f" ({len(_missing)} without a GitHub release)" if _missing else ""
        print(f"  · promote pre-releases to latest: none pending{note}")

    if open_subtasks:
        print(f"  • close {len(open_subtasks)} remaining release-ticket subtask(s):")
        for skey, summary, status in open_subtasks:
            print(f"      {skey:15s} [{status}]  {summary[:55]}")
    else:
        print(f"  · close remaining subtasks: none open")

    if release_ticket_close:
        rk, rst = release_ticket_close
        print(f"  • close the release ticket {rk} [{rst}] → {TARGET_STATUS_NAME}")
    elif release_ticket_key:
        print(f"  · close the release ticket: {release_ticket_key} already closed")

    if service_articles:
        print(f"\n  ⚠ Service article reminder (required by support) - publish one for:")
        for r in service_articles:
            print(f"      {r.repo} {r.release_tag}")
        print("    Draft it with: conductor article")

    if not (plan_rename or plan_release or bugs_to_close or shifts or open_subtasks
            or ladder_creations or release_steps_plan or release_latest_flips
            or release_ticket_close or unshipped):
        print("\nNothing to do - version is already finalized.")
        # Already-done releases still need the pin released so you can advance -
        # re-running finalize is the natural thing to try when you're stuck on a
        # finalized cycle, and it was previously a no-op that left the pin in
        # place (only `conductor use --clear` freed it). Apply only.
        if apply_changes:
            _release_pin_if_this_manifest(manifest_path, mf.release_label)
        return 0

    if not apply_changes:
        print("\nDry-run - no changes written. Re-run with --apply to execute.")
        return 0

    if not args.yes:
        try:
            confirm = input("\nApply these changes? (y/N): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 1
        if confirm not in ("y", "yes"):
            print("Cancelled.")
            return 0

    if unshipped and move_unshipped is None:
        if args.yes:
            move_unshipped = False
            print(f"\n  · leaving {len(unshipped)} unshipped ticket(s) on this version "
                  f"(pass --move-unshipped to move them to the new (CURRENT) train)")
        else:
            try:
                ans = input(f"\nMove {len(unshipped)} ticket(s) whose work didn't ship "
                            f"({', '.join(unshipped)}) to the new (CURRENT) train? (y/N): ")
            except (EOFError, KeyboardInterrupt):
                ans = ""
            move_unshipped = ans.strip().lower() in ("y", "yes")

    # 8. Execute
    print()
    failures = 0

    if plan_rename:
        old, new = plan_rename
        try:
            jira_client.update_version(version_id, jira_token, name=new)
            print(f"  ✓ renamed: '{old}' → '{new}'")
        except Exception as e:
            failures += 1
            print(f"  ✗ rename failed: {e}")

    if plan_release:
        _, date = plan_release
        try:
            jira_client.update_version(
                version_id, jira_token, released=True, release_date=date,
            )
            print(f"  ✓ marked released (date={date})")
        except Exception as e:
            failures += 1
            print(f"  ✗ release failed: {e}")

    for key, summary, status in bugs_to_close:
        # Close the bug's open subtasks first (release-train sign-off), so Jira will
        # offer the parent a Closed transition. Best-effort per subtask - one that
        # won't close is noted, and the parent then falls through to the usual
        # "no transition / blocked by open subtask" reporting below.
        for sub_key, sub_summary, sub_status in bug_open_subtasks.get(key, []):
            if jira_progress.close_subtask(sub_key, jira_token):
                print(f"  ✓ {key} subtask {sub_key}: {sub_status or 'Open'} → "
                      f"{TARGET_STATUS_NAME}  ({sub_summary})")
                M.record_action(manifest_path, "jira", f"{sub_key} → Closed (subtask of {key})")
            else:
                print(f"  · {key} subtask {sub_key}: couldn't auto-close (no transition / "
                      f"perms) - close manually")
        try:
            transitions = jira_client.get_issue_transitions(key, jira_token)
        except Exception as e:
            failures += 1
            print(f"  ✗ {key}: couldn't list transitions: {e}")
            continue
        target = _pick_close_transition(transitions)
        if not target:
            # No direct Closed transition from this status (e.g. a Bug at
            # "Ready for Release" whose only move is "Back to In Review"). That's
            # a workflow limitation, not an error - re-running won't help, and
            # forcing the backward transition would be wrong. Note it as a manual
            # follow-up WITHOUT counting a failure (don't taint the exit code /
            # "re-run" message for the release-critical ops that did succeed).
            available = ", ".join(
                (t.get("name") or "?") for t in transitions
            ) or "(none)"
            print(
                f"  · {key}: left at '{status}' - no transition to '{TARGET_STATUS_NAME}' "
                f"(available: {available})."
            )
            for line in _closing_blockers(key, jira_token):
                print(f"      {line}")
            print(f"      Resolve the blocker(s) above, then it can close (or close manually).")
            continue
        try:
            jira_client.transition_issue(
                key, target["id"], jira_token, resolution=TARGET_RESOLUTION,
            )
            print(f"  ✓ {key}: {status} → {TARGET_STATUS_NAME} ({TARGET_RESOLUTION})")
        except Exception as e:
            failures += 1
            # Some workflows reject the resolution field if the transition
            # screen doesn't expose it. Retry once without resolution.
            try:
                jira_client.transition_issue(key, target["id"], jira_token)
                print(f"  ✓ {key}: {status} → {TARGET_STATUS_NAME} (resolution not set on this transition)")
                failures -= 1
            except Exception as e2:
                print(f"  ✗ {key}: transition failed: {e2}")

    # Shift future-cycle labels (after bug closures so a failure here doesn't
    # block the more critical operations from being reported).
    for vid, old_name, new_name in shifts:
        try:
            jira_client.update_version(vid, jira_token, name=new_name)
            print(f"  ✓ shifted: '{old_name}' → '{new_name}'")
        except Exception as e:
            failures += 1
            print(f"  ✗ shift failed: '{old_name}' → '{new_name}': {e}")

    # Create missing future versions AFTER the shift (the shift vacates the
    # SUBSEQUENT slot). Keeps the CURRENT→NEXT→OVERNEXT→SUBSEQUENT ladder full,
    # each on the Tue/Thu cadence with start = prior release date. Capture each
    # new version's id (by release date) so the release-steps clone can tag it.
    created_version_ids: Dict[str, str] = {}
    for name, start, rel in ladder_creations:
        if project_id is None:
            print(f"  ✗ create '{name}' skipped: couldn't determine projectId")
            failures += 1
            continue
        try:
            v = jira_client.create_version(
                project_id, name, jira_token, start_date=start, release_date=rel,
            )
            if v.get("id"):
                created_version_ids[rel] = str(v["id"])
            print(f"  ✓ created version '{name}' (start={start}, release={rel})")
            M.record_action(manifest_path, "jira", f"created version {name}")
        except Exception as e:
            failures += 1
            print(f"  ✗ create failed '{name}': {e}")

    # Move the tickets whose work didn't ship onto the train that is now (CURRENT)
    # (the label shift above made it so). Same fixVersion swap as `scope defer`:
    # drop this version, add the new CURRENT, keep any other fixVersions.
    if unshipped and move_unshipped:
        try:
            target = jira_client.find_current_version(config.JIRA_PROJECT, jira_token)
        except (Exception, SystemExit):
            target = None
        if not target or str(target.get("id")) == str(version_id):
            print("  · couldn't find the new (CURRENT) train - move the unshipped "
                  f"ticket(s) by hand: {', '.join(unshipped)}")
        else:
            tid, tname = str(target.get("id")), target.get("name")
            for key in unshipped:
                try:
                    issue = jira_client.fetch_issue(key, jira_token, fields=["fixVersions"])
                    ids = [str(v.get("id")) for v in
                           ((issue.get("fields") or {}).get("fixVersions") or [])]
                    new_ids = [i for i in ids if i != str(version_id)]
                    if tid not in new_ids:
                        new_ids.append(tid)
                    jira_client.update_issue_fix_versions(key, new_ids, jira_token)
                    print(f"  ✓ {key}: moved to '{tname}' (its work didn't ship on {release_label})")
                    M.record_action(manifest_path, "jira",
                                    f"{key} fixVersion → {tname} (didn't ship on {release_label})")
                except (Exception, SystemExit) as e:
                    failures += 1
                    print(f"  ✗ {key}: move failed ({e})")

    # Clone the release-steps ticket for each newly-created train (after the
    # versions exist), tagging it with that train's version so `announce` finds
    # it. Best-effort: a failure here is reported, not fatal.
    for rel, summ, _n_sub, existing in release_steps_plan:
        if existing:
            print(f"  · release-steps for {rel}: already exists ({', '.join(existing)}) - skip")
            continue
        if rs_tmpl is None:
            continue
        try:
            pkey, _created, sub_fails = release_steps.create_ticket(
                rel, jira_token, rs_tmpl, rs_subs,
                fix_version_id=created_version_ids.get(rel), indent="  ",
            )
            M.record_action(manifest_path, "jira", f"created release-steps {pkey} for {rel}")
            failures += sub_fails
        except Exception as e:
            failures += 1
            print(f"  ✗ release-steps clone for {rel} failed: {e}")

    # Promote this cycle's GitHub pre-releases to full/latest.
    for repo, tag, rid, state in release_latest_plan:
        if state != "flip" or not rid:
            continue
        try:
            github_client.mark_release_latest(repo, rid, gh_token)
            print(f"  ✓ {repo} {tag}: pre-release → latest")
            M.record_action(manifest_path, "release", f"{repo} {tag} → latest")
        except Exception as e:
            failures += 1
            print(f"  ✗ {repo} {tag}: promote to latest failed: {e}")

    # Close any remaining release-ticket subtasks (the finalize subtask itself,
    # lingering manual steps, etc.). Best-effort like the pipeline's per-step
    # closes - a subtask that won't close (workflow/perms) is noted, not failed.
    for skey, summary, status in open_subtasks:
        if jira_progress.close_subtask(skey, jira_token):
            print(f"  ✓ subtask {skey}: {status or 'Open'} → {TARGET_STATUS_NAME}  ({summary[:40]})")
            M.record_action(manifest_path, "jira", f"{skey} → Closed ({summary[:40]})")
        else:
            print(f"  · subtask {skey}: couldn't auto-close (no transition / perms) - close manually")

    # Close the release-steps parent ticket itself - AFTER its subtasks, since
    # Jira gates the parent on all-subtasks-resolved.
    if release_ticket_close:
        rk, rst = release_ticket_close
        if jira_progress.close_subtask(rk, jira_token):
            print(f"  ✓ release ticket {rk}: {rst} → {TARGET_STATUS_NAME}")
            M.record_action(manifest_path, "jira", f"release ticket {rk} → Closed")
        else:
            print(f"  · release ticket {rk}: couldn't auto-close (open subtasks / workflow) - close manually")

    # Re-surface the support reminder after the writes, so it isn't scrolled off.
    if service_articles:
        print(f"\n  ⚠ Don't forget: publish a service article (support requirement) for "
              f"{', '.join(f'{r.repo} {r.release_tag}' for r in service_articles)} "
              f"(draft: conductor article).")

    # Reconcile the manifest's ticket statuses with live Jira, so downstream reads
    # (recap, menu, `manifest show`) reflect the closures finalize just made rather
    # than the stale build-time snapshot - e.g. a Bug finalize closed showing up as
    # "not yet Closed" in the recap follow-ups. One fetch, best-effort; the version
    # id is unchanged by the rename/release above. A bug finalize COULDN'T close
    # (no transition) correctly stays open.
    try:
        fresh = jira_client.fetch_version_issues(version_id, jira_token, fields=["status"])
        status_by_key = {
            i.get("key"): _issue_status_name(i)
            for i in fresh if i.get("key") and _issue_status_name(i)
        }
        M.update_ticket_statuses(manifest_path, status_by_key)
    except (Exception, SystemExit):
        pass

    print()
    if failures == 0:
        print("✓ all housekeeping operations succeeded")
        M.record_action(manifest_path, "finalize",
                        "Jira version finalized (renamed, released, labels shifted, "
                        "ladder extended, subtasks + release ticket closed, tags → latest)")
        _release_pin_if_this_manifest(manifest_path, mf.release_label)
        # The cycle is done - print the recap right here, the one moment someone
        # is guaranteed to be looking. Re-read so the just-recorded finalize step
        # shows in the timeline. Best-effort: never fail finalize over the recap.
        try:
            from . import recap
            print()
            for line in recap.build_recap(M.read_manifest(manifest_path)):
                print(line)
            print("  (paste-friendly: `conductor recap --markdown`)")
        except Exception:
            pass
        return 0
    else:
        print(f"! {failures} operation(s) failed - re-run with --apply after investigating")
        return 1
