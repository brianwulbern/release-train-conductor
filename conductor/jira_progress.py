"""Advance REL release subtasks through their Jira workflow as pipeline steps run.

Operator-chosen policy: when a step runs (and actually pushes), move its subtask
**Open → In Progress**, and **close EVERY release-train subtask ordered before it**
that isn't already closed - including the manual/untracked ones in between (e.g.
"Validate API migrations", "Merge functional test PRs"). Reaching step N means
steps 1..N-1 are done, so the whole prefix gets closed, not just the stages conductor
itself drives. The REL subtask workflow is Open → In Progress → Closed, and the
Closed transition requires a resolution (see _required_resolution).

"Before" is by subtask key number (REL-NNNNN), which for a release train created
from its template is the train order (matches the Jira UI's subtask order).

Everything here is best-effort and chatty: a Jira hiccup prints a note but never
raises into - let alone fails - an otherwise-successful release command. Gated by
the caller (only invoked on --push) and suppressible per-run with --no-transition.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import config, jira_client, manifest as M

IN_PROGRESS = "In Progress"
CLOSED = "Closed"

# Ordered pipeline stages, each paired with a matcher over the (lowercased) REL
# subtask summary. Order matters - the index defines "earlier" steps to close.
# Matchers are tuned to the real subtask names on the release ticket; they mirror
# the per-command discovery heuristics (bump_pre/_stg/_prd, promote).
_STAGES: List[Tuple[str, Callable[[str], bool]]] = [
    ("pre",           lambda s: "bump" in s and ("service" in s or "version" in s)),
    ("stg",           lambda s: "update stg.yaml" in s),
    ("test_platform", lambda s: "platform test" in s or "run platform" in s),
    ("test_services", lambda s: "stack" in s or "services" in s and "test" in s),
    ("prd",           lambda s: "update prd.yaml" in s),
    ("promote",       lambda s: "develop into production" in s or "develop to production" in s
                                or ("merge" in s and "production" in s)),
    ("execution",     lambda s: "release execution" in s),
]
_ORDER = [k for k, _ in _STAGES]
_MATCHER = dict(_STAGES)


def _key_num(key: Optional[str]) -> int:
    """Trailing number of an issue key (REL-52061 → 52061); 0 if none.
    Fallback train ordering when the parent's display order isn't available."""
    m = re.search(r"(\d+)\s*$", key or "")
    return int(m.group(1)) if m else 0


def _display_order_index(rel: str, token: str) -> Optional[dict]:
    """{subtask_key: position} in the parent's DISPLAY order (what the Jira UI
    shows), from its `fields.subtasks` array. None on any error / no subtasks.

    Display order is the real train order: a subtask added to the release-steps
    template later (e.g. "Bump *-services") sorts LAST by key number but is shown
    FIRST - so key-number ordering would wrongly treat the pre-bump as coming
    after the stg bump and never close it. Best-effort; absorbs SystemExit
    (jira_client._get exits on non-200)."""
    try:
        issue = jira_client.fetch_issue(rel, token, fields=["subtasks"])
    except (Exception, SystemExit):
        return None
    keys = [s.get("key") for s in ((issue.get("fields") or {}).get("subtasks") or [])
            if s.get("key")]
    return {k: i for i, k in enumerate(keys)} if keys else None


def _train_position(rel: str, token: str, subtasks: list) -> Callable[[Optional[str]], int]:
    """A key→position function ordering release-train subtasks. Prefers the
    parent's display order (robust to out-of-line key numbers), but only when it
    covers EVERY subtask we're ordering - otherwise a missing key could misplace
    the close-fence, so we fall back to key-number ordering (the historic
    behavior)."""
    idx = _display_order_index(rel, token)
    if idx and all(st.get("key") in idx for st in subtasks):
        return lambda k: idx.get(k, len(idx))
    return _key_num


def _release_ticket(mf: M.Manifest) -> Optional[str]:
    for t in mf.tickets:
        s = t.summary or ""
        if "Release Steps for Release Train" in s or s.lstrip().startswith("[Release]"):
            return t.key
    return None


def _match_subtask(subtasks: list, stage_key: str) -> Tuple[Optional[str], str, str]:
    """(issue_key, current_status_name, summary) for the subtask matching
    `stage_key`. The summary is returned so action lines can read as Jira does -
    `<key> <summary>` - rather than exposing the internal stage key."""
    matcher = _MATCHER[stage_key]
    for st in subtasks:
        fields = st.get("fields") or {}
        summary = (fields.get("summary") or "")
        if matcher(summary.lower()):
            status = ((fields.get("status") or {}).get("name") or "")
            return st.get("key"), status, summary
    return None, "", ""


# When a transition (e.g. Closed) requires a resolution, prefer a "done"-ish one
# from its allowed values. The REL subtask Closed transition only allows
# Completed/Discarded/Inactive/Duplicate - NOT Fixed/Done - so a hardcoded
# resolution would 400. Pick by preference, else fall back to the first allowed.
_RESOLUTION_PREFERENCE = ("Completed", "Done", "Fixed", "Resolved")


def _required_resolution(transition: dict) -> Optional[str]:
    """If `transition` requires a resolution, choose a sensible allowed value; else None."""
    field = (transition.get("fields") or {}).get("resolution")
    if not field or not field.get("required"):
        return None
    allowed = [v.get("name") for v in (field.get("allowedValues") or []) if v.get("name")]
    for pref in _RESOLUTION_PREFERENCE:
        if pref in allowed:
            return pref
    return allowed[0] if allowed else "Fixed"


def _transition_to(key: str, target_status: str, token: str) -> bool:
    """Move issue `key` to the status named `target_status` (case-insensitive).

    Picks the transition whose target IS that status, supplying a `resolution`
    when the transition screen requires one (e.g. Closed). Without that, the
    close silently 400s - which is exactly why prior steps weren't getting
    closed. Retries without the resolution if Jira rejects the field. Returns
    True on success, False if there's no such transition or it errors."""
    try:
        transitions = jira_client.get_issue_transitions(key, token, expand_fields=True)
    except Exception:
        return False
    target = next(
        (t for t in transitions
         if ((t.get("to") or {}).get("name") or "").strip().lower() == target_status.lower()),
        None,
    )
    if not target:
        return False
    resolution = _required_resolution(target)
    try:
        jira_client.transition_issue(key, target["id"], token, resolution=resolution)
        return True
    except Exception:
        if resolution is None:
            return False
        # Resolution rejected by this screen - retry without it.
        try:
            jira_client.transition_issue(key, target["id"], token)
            return True
        except Exception:
            return False


def _current_account(token: str) -> Tuple[Optional[str], Optional[str]]:
    """(accountId, displayName) of the operator running conductor (from Jira /myself).
    Best-effort; (None, None) if it can't be resolved. The accountId assigns the
    step's ticket; the displayName lets the recorded/printed lines name the actual
    operator ("assigned to Alex Operator") instead of a generic "operator".
    Absorbs SystemExit too - jira_client._get exits on a non-200, but this layer
    must never abort an otherwise-successful release command."""
    try:
        me = jira_client.get_myself(token)
        return (me.get("accountId") or None), (me.get("displayName") or None)
    except (Exception, SystemExit):
        return None, None


def _current_account_id(token: str) -> Optional[str]:
    """The accountId alone (thin wrapper over `_current_account`)."""
    return _current_account(token)[0]


def _label(key: Optional[str], summary: str) -> str:
    """`<key> <summary>` - how Jira itself names an issue. Used as the subject of
    every ↪ action line so the output reads like the ticket, not the internal
    stage key. Falls back to the bare key when no summary is known."""
    return f"{key} {summary}".rstrip() if summary else (key or "")


def _assign(key: str, account_id: Optional[str], token: str, manifest_path: Path,
            what: str, summary: str = "", operator_name: Optional[str] = None) -> None:
    """Assign issue `key` to `account_id` (the operator). Best-effort and chatty;
    a failure prints a note but never raises. No-op if account_id is None. `what`
    is the recorded-action context; `summary` is the Jira summary for the line
    label (`<key> <summary>`); `operator_name` names the actual operator in the
    output (falls back to "you"/"operator" when unresolved)."""
    if not account_id:
        return
    label = _label(key, summary)
    try:
        jira_client.assign_issue(key, account_id, token)
        print(f"  ↪ Jira {label}: assigned to {operator_name or 'you'}")
        M.record_action(manifest_path, "jira",
                        f"{key} assigned to {operator_name or 'operator'} ({what})")
    except (Exception, SystemExit):
        print(f"  ↪ Jira {label}: couldn't assign to {operator_name or 'you'} "
              f"(perms?) - assign manually")


def open_release_subtasks(mf: M.Manifest, token: str) -> List[Tuple[str, str, str]]:
    """(key, summary, status) for the release ticket's subtasks that aren't yet
    Closed, in train order (by key number). Empty on any error / no release
    ticket. Used by `finalize` to wrap up whatever's still open at the end."""
    rel = _release_ticket(mf)
    if not rel:
        return []
    try:
        subtasks = jira_client.fetch_subtasks(rel, token)
    except Exception:
        return []
    out: List[Tuple[str, str, str]] = []
    for st in sorted(subtasks, key=lambda s: _key_num(s.get("key"))):
        fields = st.get("fields") or {}
        status = (fields.get("status") or {}).get("name") or ""
        if status.lower() == CLOSED.lower():
            continue
        out.append((st.get("key") or "", fields.get("summary") or "", status))
    return out


def close_subtask(key: str, token: str) -> bool:
    """Close a single subtask, supplying a resolution when required. Best-effort;
    returns True on success, False if there's no Closed transition or it errors."""
    return _transition_to(key, CLOSED, token)


# The first stages that touch tickets - reaching either means real release work
# has begun, so the parent release-train ticket should read In Progress too.
_PARENT_START_STAGES = ("pre", "stg")

# Jira status categories: "new" (to-do), "indeterminate" (In Progress), "done".
_IN_PROGRESS_CATEGORY = "indeterminate"


def _transition_to_category(key: str, category_key: str, token: str) -> bool:
    """Transition `key` into ANY status whose statusCategory is `category_key`
    ('new' | 'indeterminate' | 'done').

    Matching by CATEGORY, not status name, is what makes this work for the
    release parent: it's a Task whose in-progress status is named "Selected for
    Development" (not "In Progress"), so a name match finds nothing. Its category
    is still 'indeterminate'. Best-effort; returns True/False."""
    try:
        transitions = jira_client.get_issue_transitions(key, token, expand_fields=True)
    except Exception:
        return False
    target = next(
        (t for t in transitions
         if (((t.get("to") or {}).get("statusCategory") or {}).get("key") or "").lower()
         == category_key.lower()),
        None,
    )
    if not target:
        return False
    resolution = _required_resolution(target)
    try:
        jira_client.transition_issue(key, target["id"], token, resolution=resolution)
        return True
    except Exception:
        if resolution is None:
            return False
        try:
            jira_client.transition_issue(key, target["id"], token)
            return True
        except Exception:
            return False


def _start_parent_ticket(rel: str, token: str, manifest_path: Path,
                         account_id: Optional[str] = None,
                         operator_name: Optional[str] = None) -> None:
    """Move the parent release-train ticket into the In Progress category when
    release work begins, and assign it to the operator. Idempotent: skips the
    transition if it's already In Progress or Done (but still assigns, so the
    parent reflects whoever is driving the train).

    The parent workflow has BOTH "Selected for Development" and "In Progress" in
    the 'indeterminate' category. The release-steps clone starts the parent at
    "Selected for Development", so a CATEGORY match alone (a) can't tell the two
    apart and (b) treats "Selected for Development" as already-started and never
    advances it - leaving the parent stuck there for the whole train. So we target
    the status NAMED "In Progress" specifically (via `_transition_to`), and only
    skip when it's already there or Done. Falls back to a category move for any
    workflow that has no literal "In Progress" status. Best-effort - a
    fetch/transition hiccup prints a note but never raises."""
    try:
        issue = jira_client.fetch_issue(rel, token, fields=["status", "assignee", "summary"])
        fields = issue.get("fields") or {}
        status_name = ((fields.get("status") or {}).get("name") or "").strip()
        cat = (((fields.get("status") or {}).get("statusCategory") or {}).get("key") or "").lower()
        already_assigned = ((fields.get("assignee") or {}).get("accountId") or "") == (account_id or "")
        summary = fields.get("summary") or ""
    except Exception:
        return
    already_in_progress = status_name.lower() == IN_PROGRESS.lower()
    if not already_in_progress and cat != "done":
        # By NAME first ("In Progress"), so we advance PAST "Selected for
        # Development"; category fallback for workflows without that status.
        if _transition_to(rel, IN_PROGRESS, token) or \
                _transition_to_category(rel, _IN_PROGRESS_CATEGORY, token):
            print(f"  ↪ Jira {_label(rel, summary)}: → {IN_PROGRESS}")
            M.record_action(manifest_path, "jira", f"{rel} → In Progress (release train)")
        else:
            print(f"  ↪ Jira {_label(rel, summary)}: couldn't move release-train ticket to In Progress")
    if not already_assigned:
        _assign(rel, account_id, token, manifest_path, "release train",
                summary=summary, operator_name=operator_name)


def _close_earlier_than(subtasks: list, boundary_pos, pos: Callable,
                        token: str, manifest_path: Path,
                        account_id: Optional[str] = None,
                        operator_name: Optional[str] = None) -> None:
    """Close EVERY release-train subtask ordered before `boundary_pos` (per the
    `pos` train-position function) that isn't already closed - tracked stages AND
    the manual ones in between. Chatty + best-effort; each close is recorded in
    the manifest activity log.

    A subtask conductor auto-closes here is one the operator (implicitly) just completed
    by reaching a later step, so attribute it to them: assign the operator to a
    freshly-closed prior - but ONLY when it's currently Unassigned, so a subtask
    genuinely done by someone else keeps its real assignee."""
    for st in sorted(subtasks, key=lambda s: pos(s.get("key"))):
        if pos(st.get("key")) >= boundary_pos:
            break
        fields = st.get("fields") or {}
        status = (fields.get("status") or {}).get("name") or ""
        if status.lower() == CLOSED.lower():
            continue
        skey = st.get("key")
        summ = (fields.get("summary") or "")
        already_assigned = bool((fields.get("assignee") or {}).get("accountId"))
        # Assign BEFORE closing (assignment on an open issue is always allowed);
        # skip when someone's already on it.
        if account_id and not already_assigned:
            _assign(skey, account_id, token, manifest_path, f"closed: {summ}",
                    summary=summ, operator_name=operator_name)
        if _transition_to(skey, CLOSED, token):
            print(f"  ↪ Jira {_label(skey, summ)}: {status or 'Open'} → Closed")
            M.record_action(manifest_path, "jira", f"{skey} → Closed ({summ})")
        else:
            print(f"  ↪ Jira {skey}: couldn't auto-close (no transition / perms) - close manually")


def _priors_to_close(subtasks: list, pos: Callable, boundary_pos) -> List[Tuple[str, str, str, bool]]:
    """The subtasks `advance`/`close_prefix_before` WOULD close: everything
    ordered before `boundary_pos` that isn't already Closed. Returns
    (key, summary, status, already_assigned) in train order - the same set the
    close loop walks, so a preview can't drift from the action."""
    out: List[Tuple[str, str, str, bool]] = []
    for st in sorted(subtasks, key=lambda s: pos(s.get("key"))):
        if pos(st.get("key")) >= boundary_pos:
            break
        fields = st.get("fields") or {}
        status = (fields.get("status") or {}).get("name") or ""
        if status.lower() == CLOSED.lower():
            continue
        out.append((
            st.get("key"),
            (fields.get("summary") or "")[:40],
            status or "Open",
            bool((fields.get("assignee") or {}).get("accountId")),
        ))
    return out


def _print_jira_plan(
    priors: List[Tuple[str, str, str, bool]],
    *,
    parent_start: Optional[str] = None,
    current: Optional[Tuple[str, str]] = None,  # (key, status) for the step's own subtask
) -> None:
    """Compact heads-up of the Jira writes about to happen - these are the only
    conductor writes with no dry-run, and they touch shared team state, so name the full
    scope BEFORE doing it. Informational (not a gate); --no-transition skips it."""
    lines: List[str] = []
    if parent_start:
        lines.append(f"start parent {parent_start} (release train) → In Progress")
    if priors:
        keys = ", ".join(k for k, _s, _st, _a in priors)
        n_assign = sum(1 for _k, _s, _st, assigned in priors if not assigned)
        assign = f"; assign {n_assign} unassigned to you" if n_assign else ""
        lines.append(f"close {len(priors)} prior subtask(s): {keys}{assign}")
    if current:
        ckey, cstatus = current
        if cstatus.lower() in (IN_PROGRESS.lower(), CLOSED.lower()):
            lines.append(f"{ckey} already {cstatus} - left as is")
        else:
            lines.append(f"{ckey} → In Progress (assigned to you)")
    if not lines:
        return
    print("  Jira plan (this push):")
    for line in lines:
        print(f"    • {line}")
    print("    (pass --no-transition to skip all Jira writes)")


def close_prefix_before(manifest_path: Path, boundary_stage_key: str, *,
                        no_transition: bool = False) -> None:
    """Close every release-train subtask ordered BEFORE the boundary stage's
    subtask (exclusive), marking nothing In Progress.

    For steps that legitimately finish a prefix of the train but have no subtask
    of their own - namely cutting release tags, which (in the operator's flow)
    happens right after the stg bump but BEFORE the functional-test triggers.
    Passing boundary_stage_key="test_platform" closes everything up to but NOT
    including "Run platform tests …", so the two test-trigger subtasks stay open
    for the tester to drive.

    Best-effort and never raises. No-op on --no-transition, an unknown boundary
    stage, missing creds/ticket, or if the boundary subtask can't be located -
    we refuse to close the whole train when we can't find the fence.
    """
    if no_transition or boundary_stage_key not in _ORDER:
        return
    if not config.jira_configured():
        print("  (skipping Jira transition - Jira creds not set; --no-transition to silence)")
        return

    manifest_path = Path(manifest_path)
    try:
        token = config.get_jira_token()
        mf = M.read_manifest(manifest_path)
    except Exception:
        return
    rel = _release_ticket(mf)
    if not rel:
        return
    try:
        subtasks = jira_client.fetch_subtasks(rel, token)
    except Exception as e:
        print(f"  (Jira transition skipped - couldn't load subtasks: {e})")
        return

    boundary_key, _, _ = _match_subtask(subtasks, boundary_stage_key)
    if not boundary_key:
        print(f"  (no REL subtask matched boundary '{boundary_stage_key}' "
              f"- skipping tag-time closes)")
        return
    account_id, operator_name = _current_account(token)
    pos = _train_position(rel, token, subtasks)
    _print_jira_plan(_priors_to_close(subtasks, pos, pos(boundary_key)))
    # Cutting tags is release work in progress - ensure the parent reads In
    # Progress too (idempotent self-heal), same as the numbered steps do.
    _start_parent_ticket(rel, token, manifest_path, account_id, operator_name)
    _close_earlier_than(subtasks, pos(boundary_key), pos, token, manifest_path,
                        account_id, operator_name)


def advance(manifest_path: Path, stage_key: str, *, no_transition: bool = False) -> None:
    """Mark `stage_key`'s subtask In Progress and close every prior train subtask.

    Best-effort and never raises. No-op when --no-transition, the stage is
    unknown, or Jira creds aren't configured.
    """
    if no_transition or stage_key not in _ORDER:
        return
    if not config.jira_configured():
        print("  (skipping Jira transition - Jira creds not set; --no-transition to silence)")
        return

    manifest_path = Path(manifest_path)
    try:
        token = config.get_jira_token()
        mf = M.read_manifest(manifest_path)
    except Exception:
        return
    rel = _release_ticket(mf)
    if not rel:
        return
    try:
        subtasks = jira_client.fetch_subtasks(rel, token)
    except Exception as e:
        print(f"  (Jira transition skipped - couldn't load subtasks: {e})")
        return

    # Who's running this step - so we can assign the step's ticket to them, and
    # name them in the action lines.
    account_id, operator_name = _current_account(token)

    # Locate this step's subtask; everything ordered before it is "prior".
    cur_key, cur_status, cur_summary = _match_subtask(subtasks, stage_key)
    # The plan flags "start parent" only on the nominal first steps (pre/stg); the
    # actual _start_parent_ticket call below is idempotent and runs on EVERY step
    # (self-heal), so a later step still surfaces its transition line if needed.
    parent_start = rel if stage_key in _PARENT_START_STAGES else None

    # Heads-up plan BEFORE any writes - these Jira transitions are the only conductor
    # writes with no dry-run and they touch shared team state, so name the full
    # scope first. Needs cur_key (and its position) to know what's "prior".
    if cur_key:
        pos = _train_position(rel, token, subtasks)
        _print_jira_plan(
            _priors_to_close(subtasks, pos, pos(cur_key)),
            parent_start=parent_start,
            current=(cur_key, cur_status),
        )

    # Flip the parent release-train ticket to In Progress whenever ANY step runs -
    # reaching a step means work has begun. Idempotent (skips if already In
    # Progress or Done), so it self-heals a parent left in "Selected for
    # Development" because an earlier step wasn't driven by conductor. Before the
    # subtask-match gate so it happens even if the match falls through.
    _start_parent_ticket(rel, token, manifest_path, account_id, operator_name)

    if not cur_key:
        print(f"  (no REL subtask matched stage '{stage_key}' - skipping its transition)")
        return

    # 1. Close EVERY release-train subtask ordered before this step that isn't
    #    already closed - tracked stages AND the manual ones in between. Reaching
    #    this step means the whole prefix is done. Ordered by the parent's DISPLAY
    #    order (falls back to key number) so an out-of-line key (e.g. the
    #    "Bump *-services" pre-bump shown first but keyed last) still counts as
    #    prior to the stg bump.
    _close_earlier_than(subtasks, pos(cur_key), pos, token, manifest_path,
                        account_id, operator_name)

    # 2. Assign THIS step's subtask to the operator running it, then mark it In
    #    Progress (only from Open - don't reopen Closed). Assignment reflects who
    #    is driving this step; each auto-closed prior is likewise assigned to the
    #    operator when it was Unassigned (see _close_earlier_than).
    if cur_status.lower() in (IN_PROGRESS.lower(), CLOSED.lower()):
        return  # already in flight or done - don't reassign/reopen
    # Label the action lines with the Jira summary (`<key> <summary>`), not the
    # internal stage key, so the output reads like the ticket does in Jira.
    _assign(cur_key, account_id, token, manifest_path, cur_summary or stage_key,
            summary=cur_summary, operator_name=operator_name)
    if _transition_to(cur_key, IN_PROGRESS, token):
        print(f"  ↪ Jira {_label(cur_key, cur_summary)}: {cur_status or 'Open'} → In Progress")
        M.record_action(manifest_path, "jira", f"{cur_key} → In Progress ({cur_summary or stage_key})")
    else:
        print(f"  ↪ Jira {cur_key}: couldn't move to In Progress (no transition / perms)")
