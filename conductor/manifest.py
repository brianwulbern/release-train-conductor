"""Release manifest - the core data structure that flows through every stage.

Stage 1 (manifest build) produces it.
Stages 2–10 consume it.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config, repos_config


# Leading X.Y.Z, optionally followed by a `-<suffix>` (e.g. vpn's VERSION.txt is
# `6.1.0-ipsec` / `6.2.0-openvpn` - version first, variant appended). The suffix
# is ignored for comparison; only the numeric version orders a pre-bump.
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-.*)?$")


def _semver_tuple(s):
    """Parse an X.Y.Z string (optionally with a `-suffix`) into a comparable
    (int, int, int) tuple, or None. The suffix (e.g. `-ipsec`) is ignored."""
    if not s:
        return None
    m = _SEMVER_RE.match(s.strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class TicketEntry:
    key: str                                # "REL-12345"
    issue_id: str                           # internal numeric id, used for dev-status
    summary: str
    status: str
    issue_type: str = ""                    # Jira issuetype name ("Bug","Task","Story",…) - drives auto bump kind
    fix_versions: List[str] = field(default_factory=list)
    pr_urls: List[str] = field(default_factory=list)
    # Open/draft PRs (NOT yet merged) whose title references this ticket or a
    # subtask - surfaced by `manifest show` so the operator can chase what's
    # blocking the release. Each: {"url", "state" ("OPEN"|"DRAFT"), "repo"}.
    unmerged_prs: List[dict] = field(default_factory=list)


@dataclass
class RepoEntry:
    repo: str
    classification: str                     # "platform" | "service" | "meta" | "other"
    target_sha: Optional[str] = None
    target_tag: Optional[str] = None        # e.g. "master_413ee0" (always, used for stg bump)
    release_tag: Optional[str] = None       # e.g. "client-2.30.0" or "5.10.0" - for prd bump
    version_txt: Optional[str] = None       # VERSION.txt content if the repo uses version-prefixed tags
    ci_status: Optional[str] = None         # "success" | "failure" | "pending" | "unknown"
    ci_build: str = ""                       # identifying default-branch build, e.g. "master #158"
    ci_url: Optional[str] = None             # Jenkins URL of that build (from the GitHub status target_url)
    docker_image_status: str = "unchecked"  # "confirmed" | "missing" | "unverified" | "error" | "unchecked"
    # Existence of the RELEASE image (acme/<image>:<release_tag>) on
    # Docker Hub - the exact artifact production pulls after promote. Checked at
    # build time when a release_tag is set and Docker creds are present; "unknown"
    # for a private image without readable creds. Distinct from docker_image_status
    # above, which tracks the master_<sha> (stg) image.
    release_image_status: Optional[str] = None  # "confirmed" | "absent" | "unknown" | None
    scan_block: Optional[Dict[str, Any]] = None
    needs_pre_bump: bool = False
    pre_bump_files: List[str] = field(default_factory=list)
    contributing_tickets: List[str] = field(default_factory=list)
    pr_urls: List[str] = field(default_factory=list)
    # How release_tag was decided - for transparency in `manifest show` and to
    # drive reconcile. One of: "override" (operator --release-tag, pinned),
    # "pr_match" | "ticket_match" | "head_rescue" (auto-detected), or None.
    tag_source: Optional[str] = None
    # When True, the release_tag decision is operator-confirmed and survives a
    # rebuild - `manifest build` re-applies it instead of re-deriving from live
    # GitHub (which can flip-flop as master advances). Set by --release-tag.
    pinned: bool = False
    # Latest published bare-semver release tag at build time (pre-bump repos
    # only). Lets prebump_done() recognize a manual VERSION.txt bump that hasn't
    # been tagged yet - version_txt ahead of this ⇒ already bumped.
    latest_release: Optional[str] = None
    # Human-readable trail of how this repo's release_tag / target_sha was decided
    # at build time - the branch outcomes that aren't reconstructable from the
    # other fields (auto-accept, operator override/pin, head-rescue, "previous
    # release" rejection, master-HEAD fallback). Surfaced by `manifest explain`.
    # Schema-tolerant: old manifests default to [].
    resolution_notes: List[str] = field(default_factory=list)
    # The ACTUAL full stg image tag published to Docker Hub for target_sha (the
    # registry is the source of truth for the name). Captured at build time for
    # repos that feed the stack test. Carries base-image prefixes conductor can't
    # reconstruct from version_txt - e.g. nginx's `1.31.2-6.10.0-master_e43186`
    # (the `1.31.2-` base version). services_jenkins_params() prefers it over the
    # reconstructed `{version_txt}-{target_tag}`. None if not resolved (no creds).
    published_image_tag: Optional[str] = None
    # Commits on the default branch AFTER this train's release tag, oldest first,
    # as [{sha, title}] - work merged past the cut that the tag (and so the stg
    # bump and tests) doesn't include. Empty when the tag is at HEAD or untagged.
    # `release tag --recut` pulls them in on purpose (option 5 offers it).
    commits_after_tag: List[dict] = field(default_factory=list)

    def release_image_tag(self) -> Optional[str]:
        """The Docker Hub tag of the release image, keeping any base prefix the
        published master tag carries (nginx `1.31.3-6.13.0`, not `6.13.0`)."""
        tag, pub = self.release_tag, self.published_image_tag
        if not tag or not pub or not self.target_tag or not pub.endswith("-" + self.target_tag):
            return tag
        stem = pub[: -len(self.target_tag) - 1]
        if stem == tag or stem.endswith("-" + tag):
            return stem
        if self.version_txt and stem.endswith(self.version_txt):
            return stem[: -len(self.version_txt)] + tag
        return tag

    def prebump_done(self) -> bool:
        """True if this repo's VERSION.txt pre-bump is already done for the cycle.

        Two signals:
          - a release_tag is set (version locked + tagged this cycle), or
          - version_txt is already ahead of the latest published release
            (manually bumped, tag still pending).
        Used by the menu and `bump pre` so both agree on what still needs doing.
        """
        if self.release_tag:
            return True
        cur = _semver_tuple(self.version_txt)
        last = _semver_tuple(self.latest_release)
        return bool(cur and last and cur > last)


@dataclass
class Manifest:
    release_version_id: str
    release_label: str                      # e.g. "2026-04-28"
    generated_at: str                       # ISO timestamp
    tickets: List[TicketEntry] = field(default_factory=list)
    repos: List[RepoEntry] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    # Append-only log of side-effecting steps taken against this release: PRs
    # opened, tags cut, tests triggered, promotion, finalize. Each entry is
    # {"at": iso, "stage": str, "summary": str, "url": str|None, "repo": str|None}.
    # Survives `manifest build` rebuilds (reconcile carries it forward) so the
    # menu can show real progress rather than re-inferring it.
    actions: List[dict] = field(default_factory=list)

    def actions_for(self, stage: str) -> List[dict]:
        """All recorded actions for a pipeline stage, oldest first."""
        return [a for a in self.actions if a.get("stage") == stage]

    def slack_thread(self) -> Optional[Tuple[Optional[str], str]]:
        """(channel, ts) of the pinned announcement that roots this cycle's Slack
        thread, or None if it wasn't posted via a token this cycle (so there's
        nothing to thread status updates under).

        Stored on the most recent `announce` action's `extra` (keys
        `slack_channel` / `slack_thread_ts`), so it rides the action log through
        `manifest build` rebuilds like every other recorded step - no separate
        top-level field to carry forward."""
        for a in reversed(self.actions):
            if a.get("stage") == "announce" and a.get("slack_thread_ts"):
                return a.get("slack_channel"), a["slack_thread_ts"]
        return None

    def door_closed(self, now: Optional[dt.datetime] = None) -> bool:
        """True once the scope-freeze ("closing the door") has been posted this
        cycle - recorded as a `close_door` action - OR once the door post that
        Slack was scheduled to make (by the `a` utility) is due: the latest
        `door_warning` carries a `door_scheduled_id` and its cutoff has passed.
        The `d` utility and the auto close-on-first-bump both guard on this so the
        freeze posts at most once."""
        if any(a.get("stage") == "close_door" for a in self.actions):
            return True
        latest = self._latest_door_warning()
        if latest and latest.get("door_scheduled_id"):
            cutoff = self.door_cutoff()
            now = now or dt.datetime.now(dt.timezone.utc)
            return cutoff is not None and cutoff <= now
        return False

    def _latest_door_warning(self) -> Optional[dict]:
        for a in reversed(self.actions):
            if a.get("stage") == "door_warning" and a.get("door_at"):
                return a
        return None

    def pending_door_post(
        self, now: Optional[dt.datetime] = None,
    ) -> Optional[Tuple[str, str]]:
        """(channel, scheduled_message_id) of the door post Slack still has
        queued (the latest `door_warning`'s schedule, cutoff not yet reached), or
        None. Callers cancel it when the door is closed early or re-announced."""
        if any(a.get("stage") == "close_door" for a in self.actions):
            return None
        latest = self._latest_door_warning()
        if not latest or not latest.get("door_scheduled_id"):
            return None
        cutoff = self.door_cutoff()
        now = now or dt.datetime.now(dt.timezone.utc)
        if cutoff is None or cutoff <= now:
            return None
        return latest.get("door_channel"), latest["door_scheduled_id"]

    def door_cutoff(self) -> Optional[dt.datetime]:
        """The announced door-closing time (UTC) from the latest `door_warning`
        action (the `a` "All aboard" utility), or None if none was posted. The
        latest wins, so re-posting with a later time moves the cutoff."""
        latest = self._latest_door_warning()
        if not latest:
            return None
        try:
            return dt.datetime.fromisoformat(latest["door_at"])
        except (TypeError, ValueError):
            return None

    def partly_merged_tickets(self) -> List["TicketEntry"]:
        """Tickets with at least one MERGED PR and at least one still-open PR:
        shipping the train now would ship part of the ticket (e.g. the api half
        of a change whose console half is still in review)."""
        return [t for t in self.tickets if t.pr_urls and t.unmerged_prs]

    def partial_override_keys(self) -> set:
        """Ticket keys the release manager chose to ship partly (recorded
        `partial_override` actions), so a later step doesn't block on them again."""
        keys: set = set()
        for a in self.actions:
            if a.get("stage") == "partial_override":
                keys.update(a.get("tickets") or [])
        return keys

    def test_scope_keys(self) -> Optional[List[str]]:
        """The tested-scope baseline: the ticket keys considered already-tested for
        this release. Drift = current tickets minus this set.

        Two sources, in priority order:
          1. An explicit re-baseline - a `scope_baseline` action recorded by
             `conductor scope integrate` after the operator re-folded a late add-in and
             re-ran tests. The LATEST such action wins (you can integrate more than
             once). This is the ONLY thing that moves the baseline.
          2. Otherwise the AUTO baseline - the FIRST `test` action that recorded a
             `scope` (test_trigger freezes `[t.key for t in mf.tickets]` at trigger).
             Re-triggering tests records more `test` actions but never moves the
             baseline (only #1 does) - a flaky re-run can't silently bless a late add.
          3. A re-cut resets the auto baseline: the first `test` after the latest
             `release_recut` tested the re-cut images, which carry everything then
             in scope, so it becomes the cutoff. Whichever of (1) and (3) is later
             wins.

        Survives Refresh because the action log is carried forward across rebuilds.
        Returns None when neither source exists - callers treat that as 'no
        baseline, no drift' (nothing triggered yet, or a run predating this capture).
        """
        rebaselined = [
            a for a in self.actions
            if a.get("stage") == "scope_baseline" and a.get("scope") is not None
        ]
        recuts = [a.get("at") or "" for a in self.actions_for("release_recut")]
        after_recut = next(
            (a for a in self.actions
             if a.get("stage") == "test" and a.get("scope") is not None
             and recuts and (a.get("at") or "") > max(recuts)), None)
        if after_recut and (not rebaselined
                            or (after_recut.get("at") or "") > (rebaselined[-1].get("at") or "")):
            return list(after_recut.get("scope") or [])
        if rebaselined:
            return list(rebaselined[-1].get("scope") or [])  # latest re-baseline wins
        for a in self.actions:  # oldest first - the earliest trigger is the cutoff
            if a.get("stage") == "test" and a.get("scope") is not None:
                return list(a.get("scope") or [])
        return None

    def sole_ticket(self) -> Optional[str]:
        """The train's only ticket, when it has no release-steps ticket and just one
        ticket (a hotfix version), so steps can file their PRs under it."""
        if any("Release Steps for Release Train" in (t.summary or "")
               or (t.summary or "").lstrip().startswith("[Release]") for t in self.tickets):
            return None
        return self.tickets[0].key if len(self.tickets) == 1 else None

    def tickets_added_after_test(self) -> List["TicketEntry"]:
        """Tickets now in the manifest that were NOT in scope when testing started
        - i.e. added to the (CURRENT) fix-version after the functional-test trigger,
        so they rode in untested. Empty when no test baseline exists (see
        `test_scope_keys`). The risky direction to surface: untested work going out.
        """
        scope = self.test_scope_keys()
        if scope is None:
            return []
        frozen = set(scope)
        return [t for t in self.tickets if t.key not in frozen]

    def repos_with_commits_after_tag(self) -> List["RepoEntry"]:
        """Tagged repos whose default branch has moved past the release tag."""
        return [r for r in self.repos if r.release_tag and r.commits_after_tag]

    def tests_predate_recut(self) -> bool:
        """True when a release tag was re-cut after the latest functional-test
        trigger, so the tests ran on images the train no longer ships."""
        recuts = [a.get("at") or "" for a in self.actions_for("release_recut")]
        tests = [a.get("at") or "" for a in self.actions_for("test")]
        return bool(recuts and tests and max(recuts) > max(tests))

    def charts_pr_actions(
        self, stages=("bump_stg", "bump_prd", "promote"),
    ) -> List[dict]:
        """Recorded actions that opened a charts PR (have a `url`), oldest first,
        deduped by URL (a re-record of the same PR keeps only the latest entry).

        Pure - no network. Callers resolve each PR's live merge state via
        `github_client.pr_state_from_url` to answer "did the bump actually land
        before we promote?". `stages` defaults to the charts-touching bumps +
        promote; pass a subset (e.g. the two bumps) for the promote pre-check.
        """
        by_url: Dict[str, dict] = {}
        for a in self.actions:
            if a.get("stage") in stages and a.get("url"):
                by_url[a["url"]] = a  # last record for a URL wins
        return list(by_url.values())

    # ─── Derived helpers ────────────────────────────────────────────────────

    def classify_test_path(self) -> str:
        """Decide which Jenkins job to trigger:
            "platform"  - any Platform repo touched (covers both suites)
            "services"  - only Services repos touched
            "none"      - no Platform or Services repos (e.g. charts-only release)
        """
        repo_names = {r.repo for r in self.repos}
        if repo_names & repos_config.platform_repos():
            return "platform"
        if repo_names & repos_config.service_repos():
            return "services"
        return "none"

    def services_jenkins_params(self, release: bool = False) -> Dict[str, str]:
        """Build {param_name: docker_tag} for runStackFunctionalTestsForRelease.

        Returns just the docker tag (post-colon), not the full image string -
        the Jenkins form fields are labeled "Set the version of X image" and
        accept tag values like `4.2.0-master_abc123` or `master_def456`.

        Source of truth is `published_image_tag` (the actual Docker Hub tag,
        captured at build time) when present - it carries base-image prefixes we
        can't reconstruct (nginx's `1.31.2-6.10.0-master_…`). Falls back to
        `{version_txt}-{target_tag}` for pre-versioned repos, else `target_tag`.

        Scope: only services that are being bumped in this release get a value.
        Services NOT in the current manifest leave their Jenkins field blank,
        which is the desired behavior - Jenkins uses its defaults for those
        (production-current images), avoiding the need to maintain a separate
        "what's currently in prod for each service" snapshot.
        """
        jenkins_params = repos_config.jenkins_param_map()
        params: Dict[str, str] = {}
        for r in self.repos:
            param = jenkins_params.get(r.repo)
            if not param or not r.target_tag:
                continue
            # `release=True`: the release tag itself (what prd will run), for the
            # deploy-only run that leaves the QA projects on the released images.
            if release and r.release_tag:
                params[param] = r.release_image_tag()
                continue
            # Prefer the real published Docker Hub tag (source of truth); else
            # reconstruct - version_txt prefix for pre-versioned repos, else the
            # bare master tag.
            if r.published_image_tag:
                tag = r.published_image_tag
            elif r.version_txt:
                tag = f"{r.version_txt}-{r.target_tag}"
            else:
                tag = r.target_tag
            params[param] = tag
        return params

    def release_images_ready(self) -> bool:
        """True when every service repo the services run takes an image for has a
        release tag whose image is confirmed on Docker Hub, so a deploy-only run
        can move the QA projects onto the released images."""
        jenkins_params = repos_config.jenkins_param_map()
        svc = [r for r in self.repos if jenkins_params.get(r.repo) and r.target_tag]
        return bool(svc) and all(
            r.release_tag and r.release_image_status == "confirmed" for r in svc)

    def judge_bump_kind(self, repo_entry: "RepoEntry") -> str:
        """Decide 'patch' vs 'minor' for a repo from its contributing tickets'
        Jira issue types this cycle: **patch** only when EVERY known contributing
        ticket is a Bug; **minor** if any is a Task/Story/other non-bug - or if no
        ticket types are known (conservative default, matches the historic
        always-minor). Shared by `release tag` (non-pre-versioned repos) and
        `bump pre` (VERSION.txt repos) so both stages agree on the bump.
        """
        by_key = {t.key: t for t in self.tickets}
        types = [by_key[k].issue_type for k in (repo_entry.contributing_tickets or [])
                 if k in by_key and by_key[k].issue_type]
        if types and all(t.strip().lower() == "bug" for t in types):
            return "patch"
        return "minor"

    def argocd_sync_order(self) -> List[str]:
        """Return repo names in the runbook-mandated sync order:
        workloads-controller, api, then everything else.
        """
        repo_names = [r.repo for r in self.repos]
        prefix = [r for r in config.ARGOCD_SYNC_ORDER_PREFIX if r in repo_names]
        rest = [r for r in repo_names if r not in config.ARGOCD_SYNC_ORDER_PREFIX]
        return prefix + sorted(rest)


# ─────────────────────────────────────────────────────────────────────────────
# I/O
# ─────────────────────────────────────────────────────────────────────────────


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


_LABEL_RE = re.compile(r"release-(.+)\.json$")


def _manifest_sort_key(p: Path):
    """Sort key for release manifests: prefer a parseable YYYY-MM-DD in the name
    (real cycle date), falling back to mtime for non-date labels so they don't
    sort lexically ahead of dated ones.
    """
    m = _LABEL_RE.search(p.name)
    label = m.group(1) if m else p.stem
    d = _label_date(label)
    if d is not None:
        # (1, date) - dated manifests sort by date, ahead of undated.
        return (1, d.isoformat())
    # (0, mtime-iso) - undated manifests fall back to mtime, behind any dated one.
    try:
        mtime = dt.datetime.fromtimestamp(p.stat().st_mtime, dt.timezone.utc)
    except OSError:
        mtime = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    return (0, mtime.isoformat())


def _label_date(label: str) -> Optional[dt.date]:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", label or "")
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def all_manifests() -> List[Path]:
    """All release-*.json in the state dir, newest first (by cycle date)."""
    if not config.STATE_DIR.exists():
        return []
    candidates = list(config.STATE_DIR.glob("release-*.json"))
    candidates.sort(key=_manifest_sort_key, reverse=True)
    return candidates


def shipped_by(repo: str, tag: str, label: str) -> Optional[str]:
    """Label of an earlier train whose manifest already carries `repo` at release
    `tag`, or None."""
    for p in all_manifests():
        try:
            mf = read_manifest(p)
        except Exception:
            continue
        if mf.release_label < label and any(
                r.repo == repo and r.release_tag == tag for r in mf.repos):
            return mf.release_label
    return None


def find_latest_manifest() -> Optional[Path]:
    """Return the most-recent release-*.json in ~/.conductor/state/, or None.

    'Most recent' is the latest parseable YYYY-MM-DD cycle date in the filename
    (preferred over mtime, since you may build a future manifest before its
    cycle runs); non-date labels fall back to mtime and sort behind dated ones.
    """
    manifests = all_manifests()
    return manifests[0] if manifests else None


def recent_manifests(within_days: int = 5) -> List[Path]:
    """Dated manifests whose cycle date is within `within_days` of the newest
    dated manifest - the candidate set for cycle-overlap detection. The Tue/Thu
    cadence puts an adjacent cycle 2-3 days out, so a small window catches
    genuine overlap without flagging a month of finalized history.
    """
    dated = [(p, _label_date(_LABEL_RE.search(p.name).group(1)))
             for p in all_manifests() if _LABEL_RE.search(p.name)]
    dated = [(p, d) for p, d in dated if d is not None]
    if not dated:
        return []
    newest = dated[0][1]
    return [p for p, d in dated if abs((newest - d).days) <= within_days]


def _is_finished(mf: "Manifest", is_newest: bool) -> bool:
    """Heuristic for 'this release is done, not in flight':
    it has been finalized (Jira housekeeping recorded), OR it isn't the newest
    cycle and all its taggable (platform/service) repos already have release tags
    (tags cut + a newer cycle started ⇒ effectively shipped).
    """
    if any(a.get("stage") == "finalize" for a in mf.actions):
        return True
    taggable = [r for r in mf.repos if r.classification in ("platform", "service")]
    if not is_newest and taggable and all(r.release_tag for r in taggable):
        return True
    return False


def manifests_in_flight() -> List[Path]:
    """Recent manifests that are still in progress - excludes finished cycles so
    the overlap signal means 'genuinely juggling two releases', not 'a finished
    one happens to be within a few days'. Always keeps the newest.
    """
    recent = recent_manifests()
    if not recent:
        return []
    newest = recent[0]
    out: List[Path] = []
    for p in recent:
        try:
            mf = read_manifest(p)
        except Exception:
            out.append(p)
            continue
        if not _is_finished(mf, is_newest=(p == newest)):
            out.append(p)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Active-release pointer (cycle-overlap support)
#
# During finalize-while-next-starts there are two manifests in flight. Rather
# than silently defaulting every command to the newest by date, the operator
# can pin an "active" release with `conductor use`. default_manifest_path() honours it.
# ─────────────────────────────────────────────────────────────────────────────

def _active_file() -> Path:
    return config.STATE_DIR / "active.json"


def get_active_path() -> Optional[Path]:
    """Return the pinned active manifest path if set and the file still exists."""
    f = _active_file()
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text())
    except Exception:
        return None
    p = data.get("path")
    if not p:
        return None
    path = Path(p)
    return path if path.exists() else None


def set_active(path: Path) -> None:
    config.ensure_state_dir()
    mf = read_manifest(path)
    _active_file().write_text(json.dumps(
        {"label": mf.release_label, "path": str(path)}, indent=2,
    ))


def clear_active() -> None:
    f = _active_file()
    if f.exists():
        f.unlink()


def default_manifest_path() -> Optional[Path]:
    """The manifest commands should act on when none is passed: the pinned
    active release if set, else the newest by cycle date.
    """
    return get_active_path() or find_latest_manifest()


# ─────────────────────────────────────────────────────────────────────────────
# Action log
# ─────────────────────────────────────────────────────────────────────────────

def update_ticket_statuses(
    manifest_path: Path, status_by_key: Dict[str, str],
) -> None:
    """Patch `TicketEntry.status` for the given {key: status} on the on-disk
    manifest (read-modify-write). Keeps downstream reads (recap, menu, `manifest
    show`) truthful after a step transitions tickets in Jira - e.g. finalize
    closing a Bug - instead of showing the stale build-time snapshot.

    Best-effort: never raises into the caller (a status refresh must not fail an
    otherwise-successful command). Only keys already present in the manifest are
    touched; unknown keys are ignored.
    """
    try:
        mf = read_manifest(manifest_path)
        changed = False
        for t in mf.tickets:
            new = status_by_key.get(t.key)
            if new and new != t.status:
                t.status = new
                changed = True
        if changed:
            write_manifest(mf, manifest_path)
    except Exception:
        pass


def record_action(
    manifest_path: Path,
    stage: str,
    summary: str,
    url: Optional[str] = None,
    repo: Optional[str] = None,
    extra: Optional[Dict] = None,
) -> None:
    """Append a side-effect record to the manifest on disk (read-append-write).

    `extra`, if given, merges additional keys into the entry (e.g. the `test`
    action's `scope` - the in-scope ticket keys frozen at trigger time). Extra
    keys survive read/write since actions are stored as raw dicts.

    Best-effort: never raises into the caller - recording progress must not
    fail an otherwise-successful release action.
    """
    try:
        mf = read_manifest(manifest_path)
        entry = {
            "at": now_iso(),
            "stage": stage,
            "summary": summary,
            "url": url,
            "repo": repo,
        }
        if extra:
            entry.update(extra)
        mf.actions.append(entry)
        write_manifest(mf, manifest_path)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Reconcile (sticky manifest)
# ─────────────────────────────────────────────────────────────────────────────

def pinned_overrides(prior: "Manifest") -> Dict[str, str]:
    """{repo: release_tag} for repos the operator pinned in a prior manifest, so
    a rebuild can re-apply them without the operator re-passing --release-tag.
    """
    return {
        r.repo: r.release_tag
        for r in prior.repos
        if r.pinned and r.release_tag
    }


FORMAT = 1


class ManifestFormatError(ValueError):
    pass


def write_manifest(manifest: Manifest, out_path: Optional[Path] = None) -> Path:
    if out_path is None:
        config.ensure_state_dir()
        filename = f"release-{manifest.release_label}.json"
        out_path = config.STATE_DIR / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"format": FORMAT, **asdict(manifest)}, f, indent=2, sort_keys=False)
    return out_path


def read_manifest(path: Path) -> Manifest:
    with open(path) as f:
        data = json.load(f)
    fmt = data.get("format", 1)
    if not isinstance(fmt, int) or fmt > FORMAT:
        raise ManifestFormatError(
            f"{path} uses manifest format {fmt}, newer than this conductor reads ({FORMAT}); "
            "upgrade conductor (git pull, then pip install --user .)")

    # Tolerant of schema evolution - drop unknown keys, default missing ones.
    ticket_fields = {fld.name for fld in fields(TicketEntry)}
    repo_fields = {fld.name for fld in fields(RepoEntry)}

    return Manifest(
        release_version_id=data["release_version_id"],
        release_label=data["release_label"],
        generated_at=data["generated_at"],
        tickets=[
            TicketEntry(**{k: v for k, v in t.items() if k in ticket_fields})
            for t in data.get("tickets", [])
        ],
        repos=[
            RepoEntry(**{k: v for k, v in r.items() if k in repo_fields})
            for r in data.get("repos", [])
        ],
        flags=data.get("flags", []),
        actions=data.get("actions", []),
    )
