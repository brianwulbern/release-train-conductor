"""The interactive menu's model: which pipeline step is available right now, and why not.

Running `conductor` with no arguments opens a phase-aware menu. It reads the release manifest,
works out where the train is (nothing in scope yet, stg testing, tagging, ready for
production), and lists every step, always in the same place: available (plain) or disabled
(`·`, not selectable, with the reason in place of the hint). Steps are never hidden.

`PipelineState`, `_detect_state`, `MenuAction`, `_build_actions`, `_print_actions` and
`_resolve_choice` below are the real implementation, extracted as-is. The production menu also
runs each step in-process; here the `_act_*` step runners are stand-ins that just name the step,
since the steps themselves (charts PRs, tag cutting, test triggers, cluster checks) talk to
private infrastructure.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from . import config, manifest as M


def _stub(step: str) -> Callable[..., int]:
    """A stand-in step runner: names the step it would run and succeeds."""
    def run(*_args, **_kwargs) -> int:
        print(f"  (showcase) would run: {step}")
        return 0
    run.__name__ = f"_act_{step.replace(' ', '_')}"
    return run


_act_manifest_build = _stub("manifest build")
_act_manifest_show = _stub("manifest show")
_act_bump_pre = _stub("bump pre")
_act_bump_stg_push = _stub("bump stg")
_act_release_tag_apply = _stub("release tag")
_act_trigger_tests_platform = _stub("test platform")
_act_trigger_tests_services = _stub("test services")
_act_bump_prd_push = _stub("bump prd")
_act_promote = _stub("promote production")
_act_cluster_sync_check = _stub("cluster sync")
_act_cluster_sync_wait = _stub("cluster sync --wait")
_act_open_argocd_out_of_sync = _stub("open deploy view")
_act_finished_syncing = _stub("finished syncing")
_act_finalize_apply = _stub("finalize")
_act_announce = _stub("announce")


def _release_images_offered(mf) -> bool:
    """Stand-in: the production menu offers a deploy-only test run on the release images
    once its CI job has recorded a full run. That check reads the CI server, so the
    showcase never offers it."""
    return False


@dataclass
class PipelineState:
    manifest_path: Optional[Path] = None
    manifest: Optional[M.Manifest] = None
    release_label: Optional[str] = None
    phase: str = "no-manifest"           # no-manifest | stg-testing | tagging | prd-ready | empty
    needs_pre_bump_count: int = 0        # pre-bumped repos still PENDING (no release_tag yet)
    pre_bump_total: int = 0              # pre-bumped repos in this manifest (pending + done)
    tagged_count: int = 0
    untagged_count: int = 0
    scope_count: int = 0                 # platform+service repos in scope (0 ⇒ empty/early)
    active_pinned: bool = False          # True if the manifest came from an `conductor use` pin
    in_flight_count: int = 0             # unfinished manifests (genuine cycle overlap)
    finalized: bool = False              # finalize --apply recorded: the train has shipped


def _detect_state() -> PipelineState:
    s = PipelineState()
    p = M.default_manifest_path()
    s.active_pinned = M.get_active_path() is not None
    s.in_flight_count = len(M.manifests_in_flight())
    if not p:
        return s
    s.manifest_path = p
    try:
        mf = M.read_manifest(p)
    except Exception:
        return s
    s.manifest = mf
    s.release_label = mf.release_label
    s.finalized = bool(mf.actions_for("finalize"))

    # Tagging/phase gating must mirror what `release tag` actually acts on -
    # platform AND service repos (see release_tag._classification_needs_tag).
    # Using platform-only here hid an untagged SERVICE repo (e.g. a service-backup
    # catch-up), so the menu wrongly read "all tags cut" and disabled the tag step.
    taggable = [r for r in mf.repos if r.classification in ("platform", "service")]
    tagged = [r for r in taggable if r.release_tag]
    untagged = [r for r in taggable if not r.release_tag]
    s.tagged_count = len(tagged)
    s.untagged_count = len(untagged)
    s.scope_count = len(taggable)
    pre_bump_repos = [r for r in mf.repos if r.needs_pre_bump]
    s.pre_bump_total = len(pre_bump_repos)
    # `needs_pre_bump` is a static repo-config flag (always True). Count only the
    # repos whose pre-bump isn't done yet - done = tagged this cycle OR VERSION.txt
    # already bumped ahead of the last release (see RepoEntry.prebump_done).
    s.needs_pre_bump_count = sum(1 for r in pre_bump_repos if not r.prebump_done())

    if not taggable:
        s.phase = "empty"
    elif not tagged:
        s.phase = "stg-testing"
    elif tagged and not untagged:
        s.phase = "prd-ready"
    else:
        s.phase = "tagging"

    return s


@dataclass
class MenuAction:
    label: str
    hint: str = ""
    disabled_reason: str = ""  # non-empty ⇒ dimmed with `·` AND non-selectable
    soft_reason: str = ""      # non-empty ⇒ dimmed with `·` but STILL selectable (advisory)
    run_fn: Optional[Callable[[], int]] = None
    key: Optional[str] = None  # set on lettered utility actions (u/c/…); None ⇒ numbered pipeline step


def _build_actions(state: PipelineState, prefs: dict) -> List[MenuAction]:
    has_manifest = state.manifest_path is not None
    has_release_tags = state.tagged_count > 0
    has_pre_bump = state.needs_pre_bump_count > 0

    # An "empty" manifest (no platform/service repos yet - typical at cycle
    # start, before any PRs merge) is a distinct state from "done": pipeline
    # actions aren't applicable YET, not completed. Without this distinction the
    # menu wrongly shows "all tags already cut" / "release tags not cut yet" for
    # a brand-new cycle that has nothing in scope.
    EMPTY_MSG = "nothing in scope yet - refresh (1) after PRs merge"
    empty = has_manifest and state.scope_count == 0

    # Stage gating: an action is available only when there's scope and (for the
    # later stages) the prerequisite has been met.
    pipeline_ok = has_manifest and not empty
    scope_reason = EMPTY_MSG if empty else ("" if has_manifest else "no manifest yet")

    has_untagged = state.untagged_count > 0
    late_repos = state.manifest.repos_with_commits_after_tag() if state.manifest else []
    can_tag = (has_untagged or bool(late_repos)) and not empty
    tag_reason = EMPTY_MSG if empty else ("" if can_tag else "all tags already cut")
    if not empty and late_repos and not has_untagged:
        tag_hint = "re-cut to include later commits: " + ", ".join(
            f"{r.repo} +{len(r.commits_after_tag)}" for r in late_repos)
    else:
        tag_hint = "preview → create GitHub pre-releases (auto notes)"

    can_prd = has_release_tags and not empty
    prd_reason = EMPTY_MSG if empty else ("" if has_release_tags else "release tags not cut yet")
    prd_hint = (
        f"{state.tagged_count}/{state.tagged_count + state.untagged_count} repos tagged"
        if can_prd else ""
    )

    # Pre-bump: pending / all-done / none / empty.
    if empty:
        pre_bump_hint, pre_bump_reason, pre_bump_fn = EMPTY_MSG, EMPTY_MSG, None
    elif has_pre_bump:
        pre_bump_hint, pre_bump_reason = f"{state.needs_pre_bump_count} repo(s) need it", ""
        pre_bump_fn = lambda: _act_bump_pre(prefs)
    elif state.pre_bump_total > 0:
        pre_bump_hint, pre_bump_reason, pre_bump_fn = "needed bumps done", "all needed pre-bumps done", None
    else:
        pre_bump_hint, pre_bump_reason, pre_bump_fn = (
            "no repos need VERSION.txt pre-bump", "no repos need pre-bump", None)


    # Ordered by pipeline flow:
    #   manifest → pre-bump → stg bump → release tags → tests → prd bump → cluster sync
    # (tags precede tests so security scans start as early as possible.)
    return [
        MenuAction(
            label="Refresh manifest",
            hint=(f"conductor manifest build {state.manifest.release_version_id} (pinned) "
                  f"- `conductor use --clear` to unpin"
                  if state.active_pinned and state.manifest
                  else "conductor manifest build current"),
            run_fn=_act_manifest_build,
        ),
        MenuAction(
            label="Show manifest details",
            hint="conductor manifest show" if has_manifest else "",
            disabled_reason="" if has_manifest else "no manifest yet",
            run_fn=_act_manifest_show if has_manifest else None,
        ),
        MenuAction(
            label="Pre-bump VERSION.txt PRs",
            hint=pre_bump_hint,
            disabled_reason=pre_bump_reason,
            run_fn=pre_bump_fn,
        ),
        MenuAction(
            label="Bump stg",
            hint="preview → apply + push PR on charts" if pipeline_ok else "",
            disabled_reason=scope_reason,
            run_fn=(lambda: _act_bump_stg_push(prefs)) if pipeline_ok else None,
        ),
        # Release tags are cut BEFORE the functional-test triggers: tagging as
        # early as possible (right after the stg bump) gives the security image
        # scans the most lead time and avoids scan-timeout failures downstream.
        MenuAction(
            label="Cut release tags",
            hint=tag_hint if can_tag else tag_reason,
            disabled_reason="" if can_tag else tag_reason,
            run_fn=_act_release_tag_apply if can_tag else None,
        ),
        MenuAction(
            label="Trigger platform tests",
            hint="opens Jenkins build form in browser" if pipeline_ok else "",
            disabled_reason=scope_reason,
            run_fn=_act_trigger_tests_platform if pipeline_ok else None,
        ),
        MenuAction(
            label="Trigger services tests",
            hint=(("full run done; next: deploy run on the release tags (QA projects → released images)"
                   if _release_images_offered(state.manifest) else
                   "opens Jenkins form with image params pre-filled") if pipeline_ok else ""),
            disabled_reason=scope_reason,
            run_fn=_act_trigger_tests_services if pipeline_ok else None,
        ),
        MenuAction(
            label="Bump prd",
            hint=(f"preview → apply + push PR on charts  ({prd_hint})"
                  if can_prd and prd_hint else
                  ("preview → apply + push PR on charts" if can_prd else "")),
            disabled_reason="" if can_prd else prd_reason,
            run_fn=(lambda: _act_bump_prd_push(prefs)) if can_prd else None,
        ),
        MenuAction(
            label="Promote develop → production",
            hint="opens PR on charts, base=production" if can_prd else "",
            disabled_reason="" if can_prd else prd_reason,
            run_fn=(lambda: _act_promote(prefs)) if can_prd else None,
        ),
        MenuAction(
            label="Cluster sync check",
            hint="conductor cluster sync" if has_manifest else "",
            disabled_reason="" if has_manifest else "no manifest yet",
            run_fn=_act_cluster_sync_check if has_manifest else None,
        ),
        MenuAction(
            label="Cluster sync (--wait pods)",
            hint="conductor cluster sync --wait" if has_manifest else "",
            disabled_reason="" if has_manifest else "no manifest yet",
            run_fn=_act_cluster_sync_wait if has_manifest else None,
        ),
        MenuAction(
            label="Open ArgoCD view",
            hint="browser → OutOfSync apps; starts Release Execution in Jira",
            run_fn=_act_open_argocd_out_of_sync,
        ),
        MenuAction(
            label="Post cycle-empty note" if empty else "Finished syncing",
            hint=("nothing shipped - post the close-out, then Finalize (14)" if empty
                  else ("broadcast 'train complete' + article reminder"
                        if has_manifest else "")),
            disabled_reason="" if has_manifest else "no manifest yet",
            run_fn=_act_finished_syncing if has_manifest else None,
        ),
        MenuAction(
            label="Finalize Jira",
            hint="preview → rename + release + close stuck Bugs" if has_manifest else "",
            disabled_reason="" if has_manifest else "no manifest yet",
            run_fn=_act_finalize_apply if has_manifest else None,
        ),
        MenuAction(
            label="Announce next train",
            hint=("preview + post call-for-features to #releases"
                  if config.slack_configured()
                  else "preview message (set CONDUCTOR_SLACK_TOKEN or _WEBHOOK to post)"),
            run_fn=_act_announce,
        ),
    ]


def _print_actions(actions: List[MenuAction], utilities: List[MenuAction]) -> None:
    # Right-align the selector in a 2-wide slot (" 1"…"14", " u", " q") and pad
    # the label to a fixed width so EVERY description starts in the same column,
    # whether the row is single- or double-digit, a lettered utility, or quit.
    def _row(marker: str, sel: str, label: str, tail: str) -> str:
        line = f"  {marker} {sel:>2}. {label:<32s}"
        return (line + f" - {tail}").rstrip() if tail else line.rstrip()

    print("Actions:")
    for i, a in enumerate(actions, 1):
        # `·` for either a hard-disable OR an advisory soft note; only
        # disabled_reason actually blocks selection (see _resolve_choice).
        marker = "·" if (a.disabled_reason or a.soft_reason) else " "
        print(_row(marker, str(i), a.label, a.disabled_reason or a.soft_reason or a.hint))
    if utilities:
        print("  Utilities:")
        for u in utilities:
            # Same `·` convention as the numbered steps: a greyed-out utility shows
            # why it can't run in place of its hint.
            marker = "·" if (u.disabled_reason or u.soft_reason) else " "
            print(_row(marker, u.key or "", u.label, u.disabled_reason or u.soft_reason or u.hint))
    print(_row(" ", "q", "Quit", ""))


def _resolve_choice(choice: str, actions: List[MenuAction],
                    utilities: Optional[List[MenuAction]] = None):
    """Map a raw menu input to an outcome. Returns (kind, value):

        ("quit", None)        - q / quit / exit
        ("menu", None)        - empty input (return to / redraw the menu)
        ("run", MenuAction)   - a valid, enabled action to run
        ("error", message)    - unparseable, out of range, or disabled
    """
    c = (choice or "").strip().lower()
    if c in ("q", "quit", "exit"):
        return ("quit", None)
    if not c:
        return ("menu", None)
    # Lettered utility keys (u/c/…) resolve BEFORE the numeric parse so a single
    # letter never falls through to "invalid input".
    for u in (utilities or []):
        if u.key and c == u.key:
            if u.disabled_reason or not u.run_fn:
                return ("error", f"'{u.label}' is unavailable: {u.disabled_reason}")
            return ("run", u)
    try:
        idx = int(c) - 1
    except ValueError:
        return ("error", f"invalid input: {choice!r}")
    if idx < 0 or idx >= len(actions):
        return ("error", f"choice out of range (1-{len(actions)})")
    action = actions[idx]
    if action.disabled_reason or not action.run_fn:
        return ("error", f"'{action.label}' is unavailable: {action.disabled_reason}")
    return ("run", action)
