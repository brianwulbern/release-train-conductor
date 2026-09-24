"""Finalize wrap-up behavior:
  1. close any remaining release-ticket subtasks (operator ask 2026-07-02)
  2. a Bug that has no direct 'Closed' transition (e.g. 'Ready for Release' whose
     only move is 'Back to In Review') is a NOTE, not a failure - finalize must
     still exit 0 for the release-critical ops that succeeded."""
from types import SimpleNamespace

from conductor import jira_progress
from conductor.commands import finalize


def _sub(key, summary, status):
    return {"key": key, "fields": {"summary": summary, "status": {"name": status}}}


# ─── jira_progress.open_release_subtasks / close_subtask ──────────────────────


def test_open_release_subtasks_excludes_closed_and_orders(monkeypatch):
    mf = SimpleNamespace(tickets=[SimpleNamespace(
        key="REL-REL", summary="[Release] Release Steps for Release Train 2026-07-02")])
    subs = [
        _sub("REL-52340", "PR develop into production", "In Progress"),
        _sub("REL-52339", "Update prd.yaml files", "Closed"),   # already closed → excluded
        _sub("REL-52341", "Finalize the release", "Open"),
    ]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, tok: subs)
    out = jira_progress.open_release_subtasks(mf, "tok")
    assert [k for k, _, _ in out] == ["REL-52340", "REL-52341"]  # sorted, closed dropped


def test_close_subtask_delegates(monkeypatch):
    seen = {}
    monkeypatch.setattr(jira_progress, "_transition_to",
                        lambda key, status, tok: seen.update(key=key, status=status) or True)
    assert jira_progress.close_subtask("REL-52341", "tok") is True
    assert seen == {"key": "REL-52341", "status": jira_progress.CLOSED}


# ─── finalize execute ─────────────────────────────────────────────────────────


def _finalize_args(**kw):
    base = dict(manifest="/x.json", apply=True, yes=True, no_shift_labels=True,
                no_release_latest=True)
    base.update(kw)
    return SimpleNamespace(**base)


def _wire_finalize(monkeypatch, *, bug_transitions, open_subs, closed, bug_subtasks=None):
    """Wire finalize to in-memory Jira. `closed` collects keys closed via subtask path."""
    # A Bug's own subtasks (new close-before-parent path). Default: none.
    monkeypatch.setattr(finalize.jira_client, "fetch_subtasks",
                        lambda parent, tok: bug_subtasks or [])
    monkeypatch.setattr(finalize.M, "update_ticket_statuses", lambda *a, **k: None)
    monkeypatch.setattr(finalize.M, "default_manifest_path", lambda: "/x.json")
    # No active pin by default (hermetic - don't read the real ~/.conductor state).
    monkeypatch.setattr(finalize.M, "get_active_path", lambda: None)
    monkeypatch.setattr(finalize.M, "clear_active", lambda: None)
    monkeypatch.setattr(finalize.M, "read_manifest", lambda p: SimpleNamespace(
        release_version_id="29871", release_label="2026-07-02",
        tickets=[SimpleNamespace(key="REL-REL",
                                 summary="[Release] Release Steps for Release Train 2026-07-02")]))
    monkeypatch.setattr(finalize.M, "record_action", lambda *a, **k: None)
    monkeypatch.setattr(finalize.config, "get_jira_token", lambda: "tok")
    monkeypatch.setattr(finalize.config, "get_jira_email", lambda: "e@x")
    # Version already renamed + released → only bug-close + subtasks in play.
    monkeypatch.setattr(finalize.jira_client, "fetch_version",
                        lambda vid, tok: {"name": "2026-07-02", "released": True,
                                          "releaseDate": "2026-07-02"})
    monkeypatch.setattr(finalize.jira_client, "fetch_version_issues",
                        lambda vid, tok, fields=None: [
                            {"key": "REL-51827",
                             "fields": {"summary": "lcp backup hang", "issuetype": {"name": "Bug"},
                                        "status": {"name": "Ready for Release"}, "resolution": None}}])
    monkeypatch.setattr(finalize.jira_client, "get_issue_transitions",
                        lambda key, tok, **kw: bug_transitions)
    # fetch_issue serves both the can't-close blocker lookup and the release-ticket
    # status check. Report the release ticket already Closed here so the parent-
    # close path stays out of these subtask/bug tests (covered separately).
    monkeypatch.setattr(finalize.jira_client, "fetch_issue",
                        lambda key, tok, fields=None: {
                            "fields": {"subtasks": [], "issuelinks": [],
                                       "status": {"name": "Closed"}}})
    monkeypatch.setattr(finalize.jira_client, "transition_issue",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("bug close should not be attempted")))
    # Subtask closing goes through jira_progress.
    monkeypatch.setattr(finalize.jira_progress, "open_release_subtasks", lambda mf, tok: open_subs)
    monkeypatch.setattr(finalize.jira_progress, "close_subtask",
                        lambda key, tok: closed.append(key) or True)


def test_unclosable_bug_is_not_a_failure(monkeypatch):
    """'Ready for Release' Bug with only a backward transition → note, exit 0."""
    closed = []
    _wire_finalize(
        monkeypatch,
        bug_transitions=[{"id": "9", "name": "Back to In Review", "to": {"name": "In Review"}}],
        open_subs=[],
        closed=closed,
    )
    rc = finalize.run(_finalize_args())
    assert rc == 0  # the workflow limitation must NOT taint the exit code


def test_bug_open_subtasks_closed_before_parent(monkeypatch):
    """The REL-51429 case: a Bug gated by open subtasks. Finalize closes the
    subtasks first (release-train sign-off), then the parent gets its Close."""
    closed_subs = []
    transitioned = []
    _wire_finalize(
        monkeypatch,
        # After subtasks close, Jira offers the parent a real Closed transition.
        bug_transitions=[{"id": "5", "name": "Close", "to": {"name": "Closed"}}],
        open_subs=[],
        closed=closed_subs,
        bug_subtasks=[_sub("REL-52587", "Technical plan", "Open"),
                      _sub("REL-52588", "Fix permissions", "Open")],
    )
    # Allow the parent bug close (override the helper's "should not attempt" guard).
    monkeypatch.setattr(finalize.jira_client, "transition_issue",
                        lambda key, tid, tok, **kw: transitioned.append(key))
    rc = finalize.run(_finalize_args())
    assert rc == 0
    assert closed_subs == ["REL-52587", "REL-52588"]  # subtasks closed first…
    assert transitioned == ["REL-51827"]              # …then the parent bug


def test_no_close_subtasks_flag_skips_subtask_closing(monkeypatch):
    """--no-close-subtasks: the Bug's subtasks are left alone (parent may stay open)."""
    closed_subs = []
    _wire_finalize(
        monkeypatch,
        bug_transitions=[{"id": "9", "name": "Back to In Review", "to": {"name": "In Review"}}],
        open_subs=[],
        closed=closed_subs,
        bug_subtasks=[_sub("REL-52587", "Technical plan", "Open")],
    )
    rc = finalize.run(_finalize_args(no_close_subtasks=True))
    assert rc == 0
    assert closed_subs == []  # opted out - no subtask writes


def test_finalize_releases_pin_on_the_finalized_manifest(monkeypatch):
    """After a clean finalize, the active pin on THIS manifest is released so the
    next Refresh / `manifest build current` advances to the new CURRENT cycle."""
    from pathlib import Path
    _wire_finalize(monkeypatch, bug_transitions=[], open_subs=[], closed=[])
    monkeypatch.setattr(finalize.M, "get_active_path", lambda: Path("/x.json"))
    cleared = {"n": 0}
    monkeypatch.setattr(finalize.M, "clear_active",
                        lambda: cleared.__setitem__("n", cleared["n"] + 1))
    rc = finalize.run(_finalize_args())
    assert rc == 0 and cleared["n"] == 1


def test_finalize_keeps_a_pin_on_a_different_manifest(monkeypatch):
    """A pin pointing at a DIFFERENT in-flight release is left alone."""
    from pathlib import Path
    _wire_finalize(monkeypatch, bug_transitions=[], open_subs=[], closed=[])
    monkeypatch.setattr(finalize.M, "get_active_path", lambda: Path("/other.json"))
    cleared = {"n": 0}
    monkeypatch.setattr(finalize.M, "clear_active",
                        lambda: cleared.__setitem__("n", cleared["n"] + 1))
    rc = finalize.run(_finalize_args())
    assert rc == 0 and cleared["n"] == 0


def test_already_finalized_apply_still_releases_pin(monkeypatch):
    """Re-running finalize --apply on an ALREADY-finalized release (the natural
    'how do I move on?' move) is a no-op that must STILL free the pin - otherwise
    `conductor use --clear` is the only escape."""
    from pathlib import Path
    _wire_finalize(monkeypatch, bug_transitions=[], open_subs=[], closed=[])
    monkeypatch.setattr(finalize.jira_client, "fetch_version_issues",
                        lambda vid, tok, fields=None: [])   # no bugs → nothing to do
    monkeypatch.setattr(finalize.M, "get_active_path", lambda: Path("/x.json"))
    cleared = {"n": 0}
    monkeypatch.setattr(finalize.M, "clear_active",
                        lambda: cleared.__setitem__("n", cleared["n"] + 1))
    rc = finalize.run(_finalize_args())
    assert rc == 0 and cleared["n"] == 1   # released even on the "Nothing to do" path


def test_already_finalized_dryrun_does_not_release_pin(monkeypatch):
    from pathlib import Path
    _wire_finalize(monkeypatch, bug_transitions=[], open_subs=[], closed=[])
    monkeypatch.setattr(finalize.jira_client, "fetch_version_issues",
                        lambda vid, tok, fields=None: [])
    monkeypatch.setattr(finalize.M, "get_active_path", lambda: Path("/x.json"))
    cleared = {"n": 0}
    monkeypatch.setattr(finalize.M, "clear_active",
                        lambda: cleared.__setitem__("n", cleared["n"] + 1))
    rc = finalize.run(_finalize_args(apply=False))
    assert rc == 0 and cleared["n"] == 0   # dry-run doesn't touch the pin


def test_release_pin_helper_matches_and_differs(monkeypatch):
    from pathlib import Path
    cleared = {"n": 0}
    monkeypatch.setattr(finalize.M, "clear_active",
                        lambda: cleared.__setitem__("n", cleared["n"] + 1))
    monkeypatch.setattr(finalize.M, "get_active_path", lambda: Path("/x.json"))
    finalize._release_pin_if_this_manifest(Path("/x.json"), "2026-07-21")
    assert cleared["n"] == 1
    finalize._release_pin_if_this_manifest(Path("/other.json"), "2026-07-21")
    assert cleared["n"] == 1                # unchanged - different path
    # no pin at all → no clear, no raise
    monkeypatch.setattr(finalize.M, "get_active_path", lambda: None)
    finalize._release_pin_if_this_manifest(Path("/x.json"), "2026-07-21")
    assert cleared["n"] == 1


def test_closing_blockers_surfaces_open_subtask_and_links(monkeypatch):
    """The can't-close note explains WHY: open subtask (the real gate) + linked
    items as context. Mirrors REL-51827 (subtask REL-52295 In Progress; another team's
    linked)."""
    issue = {"fields": {
        "subtasks": [
            {"key": "REL-52294", "fields": {"status": {"name": "Closed"}}},   # closed → not shown
            {"key": "REL-52295", "fields": {"status": {"name": "In Progress"}}},  # open → gate
        ],
        "issuelinks": [
            {"type": {"name": "Fix"}, "outwardIssue": {"key": "OPS-149159",
             "fields": {"status": {"name": "Solution Accepted"}}}},
            {"type": {"name": "Relationship"}, "inwardIssue": {"key": "LCH-13360",
             "fields": {"status": {"name": "Done"}}}},
        ],
    }}
    monkeypatch.setattr(finalize.jira_client, "fetch_issue", lambda key, tok, fields=None: issue)
    lines = finalize._closing_blockers("REL-51827", "tok")
    assert lines[0] == "Blocked by open subtask(s): REL-52295 [In Progress]"
    assert "OPS-149159 [Solution Accepted] (Fix)" in lines[1]
    assert "LCH-13360 [Done] (Relationship)" in lines[1]
    assert "REL-52294" not in " ".join(lines)  # closed subtask omitted


def test_closing_blockers_empty_on_lookup_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("jira down")
    monkeypatch.setattr(finalize.jira_client, "fetch_issue", boom)
    assert finalize._closing_blockers("REL-1", "tok") == []


def test_finalize_closes_remaining_subtasks(monkeypatch):
    closed = []
    _wire_finalize(
        monkeypatch,
        bug_transitions=[{"id": "9", "name": "Back to In Review", "to": {"name": "In Review"}}],
        open_subs=[("REL-52340", "PR develop into production", "In Progress"),
                   ("REL-52341", "Finalize the release", "Open")],
        closed=closed,
    )
    rc = finalize.run(_finalize_args())
    assert rc == 0
    assert closed == ["REL-52340", "REL-52341"]  # remaining subtasks closed


def test_finalize_closes_release_parent_ticket(monkeypatch):
    """The '[Release] Release Steps …' parent ticket itself is closed once its
    subtasks are done - after the subtasks, so Jira's parent gate is satisfied."""
    closed = []
    _wire_finalize(
        monkeypatch,
        bug_transitions=[{"id": "9", "name": "Back to In Review", "to": {"name": "In Review"}}],
        open_subs=[("REL-52340", "PR develop into production", "In Progress")],
        closed=closed,
    )
    # Override: the release ticket (REL-REL) is still In Progress → should close.
    monkeypatch.setattr(finalize.jira_client, "fetch_issue",
                        lambda key, tok, fields=None: {
                            "fields": {"subtasks": [], "issuelinks": [],
                                       "status": {"name": "In Progress"}}})
    rc = finalize.run(_finalize_args())
    assert rc == 0
    # subtask first, then the parent release ticket - order matters (Jira gate).
    assert closed == ["REL-52340", "REL-REL"]
