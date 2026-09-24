"""Tests for jira_progress - advance REL subtasks as pipeline steps run.

Policy under test: a step marks its own subtask Open→In Progress and closes
EVERY release-train subtask ordered before it (by key number) that isn't already
closed - tracked stages AND the manual ones in between (Open or In Progress)."""
from types import SimpleNamespace

import pytest

from conductor import jira_progress


# Transition catalogue every issue offers (from Open: In Progress or Closed).
# Closed requires a resolution (mirrors the real REL subtask workflow, whose
# Closed transition only allows Completed/Discarded/Inactive/Duplicate).
_TRANSITIONS = [
    {"id": "11", "name": "In Progress",
     "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}},
    {"id": "21", "name": "Closed",
     "to": {"name": "Closed", "statusCategory": {"key": "done"}},
     "fields": {"resolution": {"required": True,
                               "allowedValues": [{"name": "Completed"}, {"name": "Discarded"}]}}},
]


def _subtask(key, summary, status):
    return {"key": key, "fields": {"summary": summary, "status": {"name": status}}}


@pytest.fixture
def wired(monkeypatch):
    """Wire jira_progress to in-memory Jira; returns a list of (key, transition_id)."""
    calls = []
    monkeypatch.setattr(jira_progress.config, "jira_configured", lambda: True)
    monkeypatch.setattr(jira_progress.config, "get_jira_token", lambda: "tok")
    monkeypatch.setattr(jira_progress.M, "read_manifest", lambda p: SimpleNamespace(
        tickets=[SimpleNamespace(key="REL-REL",
                                 summary="[Release] Release Steps for Release Train 2026-06-16")]))
    monkeypatch.setattr(jira_progress.M, "record_action", lambda *a, **k: None)
    monkeypatch.setattr(jira_progress.jira_client, "get_issue_transitions",
                        lambda k, t, **kw: _TRANSITIONS)
    monkeypatch.setattr(jira_progress.jira_client, "transition_issue",
                        lambda key, tid, token, **kw: calls.append((key, tid, kw.get("resolution"))))
    # Parent release ticket defaults to Open (so pre/stg move it to In Progress).
    monkeypatch.setattr(jira_progress.jira_client, "fetch_issue",
                        lambda key, token, fields=None: {"fields": {"status": {"name": "Open"}}})
    # Operator identity + assignment: get_myself returns a fixed accountId; assign
    # is a no-op by default (assignment tests override it to record).
    monkeypatch.setattr(jira_progress.jira_client, "get_myself", lambda t: {"accountId": "ME"})
    monkeypatch.setattr(jira_progress.jira_client, "assign_issue", lambda *a, **k: None)
    return calls


def test_closes_all_prior_subtasks_by_order(wired, monkeypatch):
    # Mirrors the real train: tracked stages AND untracked manual subtasks before
    # the current step. Running prd (REL-52064) closes EVERYTHING with a lower key
    # that isn't already closed - including the manual "Validate API migrations".
    subs = [
        _subtask("REL-52058", "Update stg.yaml for Release train testing", "In Progress"),
        _subtask("REL-52059", "Validate API migrations", "Open"),          # manual, untracked
        _subtask("REL-52061", "Run platform tests and validate results", "Closed"),  # already done
        _subtask("REL-52064", "Update prd.yaml files", "Open"),            # current
        _subtask("REL-52065", "PR to merge develop into production", "Open"),  # AFTER - untouched
    ]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "prd")
    closed = {c[0] for c in wired if c[1] == "21"}
    assert closed == {"REL-52058", "REL-52059"}        # In Progress AND Open priors closed
    assert all(c[2] == "Completed" for c in wired if c[1] == "21")  # with resolution
    assert ("REL-52064", "11", None) in wired          # current → In Progress
    assert not any(c[0] == "REL-52065" for c in wired)  # later subtask not touched
    assert not any(c[0] == "REL-52061" for c in wired)  # already-closed prior skipped


def test_advance_prints_jira_plan_before_writing(wired, monkeypatch, capsys):
    # The heads-up plan names the full scope BEFORE the ↪ action lines - parent
    # start (pre/stg only), the priors it'll close, and the current subtask.
    subs = [
        _subtask("REL-52058", "Update stg.yaml for Release train testing", "Open"),
        _subtask("REL-52059", "Validate API migrations", "Open"),
        _subtask("REL-52064", "Update prd.yaml files", "Open"),  # current
    ]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "prd")
    out = capsys.readouterr().out
    assert "Jira plan (this push):" in out
    assert "close 2 prior subtask(s): REL-52058, REL-52059" in out
    assert "REL-52064 → In Progress" in out
    assert "--no-transition to skip" in out
    # plan appears BEFORE the first action line
    assert out.index("Jira plan") < out.index("→ Closed")
    # action lines read as Jira does: `<key> <summary>`, not the stage key
    assert "REL-52058 Update stg.yaml for Release train testing: Open → Closed" in out


def test_plan_notes_parent_start_and_unassigned_count(wired, monkeypatch, capsys):
    subs = [
        _subtask("REL-1", "Bump *-services in their repos", "Open"),
        _subtask("REL-2", "Update stg.yaml for Release train testing", "Open"),  # current (stg)
    ]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "stg")
    out = capsys.readouterr().out
    assert "start parent REL-REL (release train) → In Progress" in out
    assert "assign 1 unassigned to you" in out   # REL-1 has no assignee


def test_plan_skipped_under_no_transition(wired, monkeypatch, capsys):
    subs = [_subtask("REL-1", "Update prd.yaml files", "Open")]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "prd", no_transition=True)
    assert "Jira plan" not in capsys.readouterr().out


def test_priors_to_close_helper():
    pos = jira_progress._key_num  # position by key number for this test
    subs = [
        _subtask("REL-1", "a", "Open"),
        _subtask("REL-2", "b", "Closed"),   # already closed → excluded
        _subtask("REL-3", "c", "In Progress"),
        _subtask("REL-5", "d", "Open"),     # at/after boundary → excluded
    ]
    priors = jira_progress._priors_to_close(subs, pos, pos("REL-5"))
    assert [p[0] for p in priors] == ["REL-1", "REL-3"]
    assert priors[0][3] is False  # unassigned


def test_open_prior_is_now_closed_too(wired, monkeypatch):
    # Behavior change (0.11.3): an Open prior is closed (reaching this step means
    # it's done), not left alone.
    subs = [
        _subtask("REL-52058", "Update stg.yaml for Release train testing", "Open"),
        _subtask("REL-52064", "Update prd.yaml files", "Open"),
    ]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "prd")
    assert ("REL-52058", "21", "Completed") in wired
    assert ("REL-52064", "11", None) in wired


def test_closed_current_not_reopened(wired, monkeypatch):
    subs = [_subtask("REL-52064", "Update prd.yaml files", "Closed")]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "prd")
    # The already-Closed current subtask is never re-transitioned…
    assert not any(c[0] == "REL-52064" for c in wired)
    # …though the parent still self-heals to In Progress (it was Open here).
    assert ("REL-REL", "11", None) in wired


def test_no_transition_flag_is_a_noop(wired, monkeypatch):
    subs = [_subtask("REL-52064", "Update prd.yaml files", "Open")]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "prd", no_transition=True)
    assert wired == []


def test_execution_stage_closes_promote_and_starts_release_execution(wired, monkeypatch):
    # Opening the ArgoCD view advances the "Release Execution" subtask: the prior
    # promote subtask (In Progress) closes, Release Execution → In Progress +
    # assigned, and the later "Services Published" subtask is left alone.
    subs = [
        _subtask("REL-52497", "Update prd.yaml files", "Closed"),
        _subtask("REL-52498", "PR to merge develop into production", "In Progress"),
        _subtask("REL-52499", "Release Execution", "Open"),           # current
        _subtask("REL-52500", "Services Published", "Open"),          # AFTER - untouched
    ]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "execution")
    assert ("REL-52498", "21", "Completed") in wired    # promote PR closed
    assert ("REL-52499", "11", None) in wired           # Release Execution → In Progress
    assert not any(c[0] == "REL-52500" for c in wired)  # later subtask untouched
    assert not any(c[0] == "REL-52497" for c in wired)  # already-closed prior skipped


def test_required_resolution_picks_preferred_allowed_value():
    # Closed requires resolution; allowed values exclude Fixed/Done → pick Completed.
    t = {"fields": {"resolution": {"required": True,
                                   "allowedValues": [{"name": "Completed"}, {"name": "Discarded"}]}}}
    assert jira_progress._required_resolution(t) == "Completed"
    # No resolution field / not required → None (e.g. the In Progress transition).
    assert jira_progress._required_resolution({"fields": {}}) is None
    assert jira_progress._required_resolution(
        {"fields": {"resolution": {"required": False}}}) is None
    # Required but none of our preferences allowed → fall back to first allowed.
    t2 = {"fields": {"resolution": {"required": True,
                                    "allowedValues": [{"name": "Inactive"}, {"name": "Duplicate"}]}}}
    assert jira_progress._required_resolution(t2) == "Inactive"


def test_unconfigured_jira_is_a_noop(monkeypatch):
    monkeypatch.setattr(jira_progress.config, "jira_configured", lambda: False)
    # Should not even attempt to read the manifest / hit Jira.
    jira_progress.advance("/tmp/m.json", "prd")  # no exception = pass


# ── close_prefix_before - release-tag cutting, boundary at the test triggers ──

def _train_subs():
    """The real cloned-train order (key number == train order)."""
    return [
        _subtask("REL-52055", "Bump *-services in their repos (if necessary)", "Open"),
        _subtask("REL-52056", "Update stg.yaml for Release train testing", "In Progress"),
        _subtask("REL-52057", "Validate API migrations", "Open"),
        _subtask("REL-52058", "Merge functional test PRs", "Open"),
        _subtask("REL-52059", "Run platform tests and validate results", "Open"),  # test trigger
        _subtask("REL-52060", "Run stack tests and validate results", "Open"),  # test trigger
        _subtask("REL-52061", "Update prd.yaml files", "Open"),
    ]


def test_close_prefix_before_stops_at_test_triggers(wired, monkeypatch):
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: _train_subs())
    jira_progress.close_prefix_before("/tmp/m.json", "test_platform")
    closed = {c[0] for c in wired if c[1] == "21"}
    # Everything before "Run platform tests …" closes: pre, stg, and the two
    # manual subtasks in between.
    assert closed == {"REL-52055", "REL-52056", "REL-52057", "REL-52058"}
    # The test-trigger subtasks and everything after stay untouched.
    assert not any(c[0] in ("REL-52059", "REL-52060", "REL-52061") for c in wired)
    # No SUBTASK is marked In Progress - release-tag has no subtask of its own…
    assert not any(c[1] == "11" and c[0] != "REL-REL" for c in wired)
    # …but the parent release ticket self-heals to In Progress (work is underway).
    assert ("REL-REL", "11", None) in wired


def test_close_prefix_before_no_transition_is_noop(wired, monkeypatch):
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: _train_subs())
    jira_progress.close_prefix_before("/tmp/m.json", "test_platform", no_transition=True)
    assert wired == []


# ── parent release-train ticket → In Progress on pre/stg bump ────────────────

def test_pre_bump_moves_parent_to_in_progress(wired, monkeypatch):
    subs = [_subtask("REL-52055", "Bump *-services in their repos (if necessary)", "Open")]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "pre")
    # Parent REL-REL Open → In Progress (transition id 11, no resolution).
    assert ("REL-REL", "11", None) in wired
    # The pre subtask itself also goes In Progress.
    assert ("REL-52055", "11", None) in wired


def test_stg_bump_moves_parent_to_in_progress(wired, monkeypatch):
    subs = [_subtask("REL-52056", "Update stg.yaml for Release train testing", "Open")]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "stg")
    assert ("REL-REL", "11", None) in wired


def test_later_stage_self_heals_parent(wired, monkeypatch):
    # Any step now moves the parent to In Progress if it isn't already - so a
    # parent left in "Selected for Development" (because pre/stg weren't driven by
    # conductor) still gets advanced when a later stage runs. Here the parent is Open.
    subs = [_subtask("REL-52061", "Update prd.yaml files", "Open")]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "prd")
    assert ("REL-REL", "11", None) in wired


def test_parent_selected_for_development_advances_to_in_progress(wired, monkeypatch):
    # "Selected for Development" and "In Progress" share the 'indeterminate'
    # category; the release-steps clone starts the parent at the former. conductor must
    # advance it to the literal "In Progress" (operator ask, 2026-08-11) - not
    # treat "Selected for Development" as already-started and leave it stuck.
    monkeypatch.setattr(jira_progress.jira_client, "fetch_issue",
                        lambda key, token, fields=None: {"fields": {"status": {
                            "name": "Selected for Development",
                            "statusCategory": {"key": "indeterminate"}}}})
    subs = [_subtask("REL-52055", "Bump *-services in their repos (if necessary)", "Open")]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "pre")
    assert ("REL-REL", "11", None) in wired   # advanced to the "In Progress" status


def test_later_stage_advances_parent_from_selected_for_development(wired, monkeypatch):
    # The real-world case: pre/stg weren't driven by conductor, so the parent is stuck
    # at "Selected for Development"; running platform tests must advance it.
    monkeypatch.setattr(jira_progress.jira_client, "fetch_issue",
                        lambda key, token, fields=None: {"fields": {"status": {
                            "name": "Selected for Development",
                            "statusCategory": {"key": "indeterminate"}}}})
    subs = [_subtask("REL-52059", "Run platform tests and validate results", "Open")]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "test_platform")
    assert ("REL-REL", "11", None) in wired   # parent advanced past SfD


def test_action_line_reads_key_then_summary_not_stage_key(wired, monkeypatch, capsys):
    # The step's own ↪ lines name the subtask `<key> <summary>` - never the
    # internal stage key ("test_platform").
    subs = [_subtask("REL-52059", "Run platform tests and validate results", "Open")]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "test_platform")
    out = capsys.readouterr().out
    assert "REL-52059 Run platform tests and validate results: Open → In Progress" in out
    assert "REL-52059 Run platform tests and validate results: assigned to you" in out
    assert "test_platform" not in out   # internal stage key never surfaced


def test_parent_already_in_progress_is_left_alone(wired, monkeypatch):
    # Already at the literal "In Progress" → idempotent, no re-transition.
    monkeypatch.setattr(jira_progress.jira_client, "fetch_issue",
                        lambda key, token, fields=None: {"fields": {"status": {
                            "name": "In Progress",
                            "statusCategory": {"key": "indeterminate"}}}})
    subs = [_subtask("REL-52055", "Bump *-services in their repos (if necessary)", "Open")]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "pre")
    assert not any(c[0] == "REL-REL" for c in wired)   # parent left alone


def test_step_assigns_its_subtask_to_operator(wired, monkeypatch):
    assigns = []
    monkeypatch.setattr(jira_progress.jira_client, "assign_issue",
                        lambda key, acct, token: assigns.append((key, acct)))
    subs = [_subtask("REL-52064", "Update prd.yaml files", "Open")]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "prd")
    assert ("REL-52064", "ME") in assigns          # current step assigned to runner
    assert ("REL-52064", "11", None) in wired       # …and moved In Progress


def test_closing_unassigned_prior_assigns_operator_not_already_assigned(wired, monkeypatch):
    # An auto-closed prior that's Unassigned gets attributed to the operator; a
    # prior already assigned to someone else keeps its assignee. Both still close.
    assigns = []
    monkeypatch.setattr(jira_progress.jira_client, "assign_issue",
                        lambda key, acct, token: assigns.append((key, acct)))

    def _sub(key, summary, status, acct=None):
        fields = {"summary": summary, "status": {"name": status}}
        if acct:
            fields["assignee"] = {"accountId": acct}
        return {"key": key, "fields": fields}

    subs = [
        _sub("REL-52058", "Update stg.yaml for Release train testing", "Open"),        # unassigned prior
        _sub("REL-52059", "Validate API migrations", "Open", acct="SOMEONE-ELSE"),     # assigned prior
        _sub("REL-52064", "Update prd.yaml files", "Open"),                            # current
    ]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "prd")
    assert ("REL-52058", "ME") in assigns      # unassigned prior → operator
    assert ("REL-52064", "ME") in assigns      # current step → operator
    assert not any(k == "REL-52059" for k, _ in assigns)  # real assignee preserved
    closed = {c[0] for c in wired if c[1] == "21"}
    assert closed == {"REL-52058", "REL-52059"}  # both priors still closed


def test_pre_bump_assigns_parent_and_subtask(wired, monkeypatch):
    assigns = []
    monkeypatch.setattr(jira_progress.jira_client, "assign_issue",
                        lambda key, acct, token: assigns.append((key, acct)))
    subs = [_subtask("REL-52055", "Bump *-services in their repos (if necessary)", "Open")]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "pre")
    assert ("REL-REL", "ME") in assigns             # parent release ticket assigned
    assert ("REL-52055", "ME") in assigns           # pre subtask assigned


def test_action_lines_name_the_operator_when_displayname_known(wired, monkeypatch, capsys):
    # /myself carries a displayName → the assign line + recorded action name the
    # actual operator ("assigned to Alex Operator"), not a generic "operator".
    monkeypatch.setattr(jira_progress.jira_client, "get_myself",
                        lambda t: {"accountId": "ME", "displayName": "Alex Operator"})
    recorded = []
    monkeypatch.setattr(jira_progress.M, "record_action",
                        lambda mp, stage, summary, **k: recorded.append(summary))
    subs = [_subtask("REL-52064", "Update prd.yaml files", "Open")]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "prd")
    out = capsys.readouterr().out
    assert "assigned to Alex Operator" in out
    assert any("assigned to Alex Operator" in r for r in recorded)
    assert not any("assigned to operator" in r for r in recorded)


def test_action_lines_fall_back_to_operator_without_displayname(wired, monkeypatch):
    # No displayName (the default mock) → recorded action keeps "assigned to operator".
    recorded = []
    monkeypatch.setattr(jira_progress.M, "record_action",
                        lambda mp, stage, summary, **k: recorded.append(summary))
    subs = [_subtask("REL-52064", "Update prd.yaml files", "Open")]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "prd")
    assert any("assigned to operator" in r for r in recorded)


def test_closed_current_is_not_assigned(wired, monkeypatch):
    assigns = []
    monkeypatch.setattr(jira_progress.jira_client, "assign_issue",
                        lambda key, acct, token: assigns.append((key, acct)))
    subs = [_subtask("REL-52064", "Update prd.yaml files", "Closed")]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "prd")
    # The already-Closed current subtask isn't reassigned (parent may still be).
    assert not any(k == "REL-52064" for k, _ in assigns)


def test_assignment_failure_is_best_effort(wired, monkeypatch):
    # A SystemExit out of assign_issue (jira_client._put exits on non-200) must
    # NOT abort the step - the transition still happens.
    def _boom(*a, **k):
        raise SystemExit("boom")
    monkeypatch.setattr(jira_progress.jira_client, "assign_issue", _boom)
    subs = [_subtask("REL-52064", "Update prd.yaml files", "Open")]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "prd")     # must not raise
    assert ("REL-52064", "11", None) in wired       # transition still applied


def test_myself_failure_does_not_abort_step(wired, monkeypatch):
    # get_myself raising SystemExit (401) must be absorbed - account_id None,
    # no assignment attempted, but the step's transition still runs.
    def _boom(t):
        raise SystemExit("401")
    monkeypatch.setattr(jira_progress.jira_client, "get_myself", _boom)
    assigns = []
    monkeypatch.setattr(jira_progress.jira_client, "assign_issue",
                        lambda key, acct, token: assigns.append((key, acct)))
    subs = [_subtask("REL-52064", "Update prd.yaml files", "Open")]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.advance("/tmp/m.json", "prd")
    assert assigns == []                            # no accountId → no assign
    assert ("REL-52064", "11", None) in wired       # transition still applied


def test_stg_closes_prebump_shown_first_but_keyed_last(wired, monkeypatch):
    # Real 2026-07-21 shape: the "Bump *-services" pre-bump subtask is displayed
    # FIRST but has the HIGHEST key (added to the template later). Key-number
    # ordering would leave it open at the stg step; display order closes it.
    subs = [
        _subtask("REL-52458", "Bump *-services in their repos (if necessary)", "Open"),  # pre, keyed last
        _subtask("REL-52448", "Update stg.yaml for Release train testing", "Open"),       # stg (current)
        _subtask("REL-52449", "Validate API migrations", "Open"),                         # after stg
    ]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    # Parent exposes the DISPLAY order via fields.subtasks; the status query
    # (fields without "subtasks") still returns Open so the parent moves.
    def fake_issue(key, token, fields=None):
        if fields and "subtasks" in fields:
            return {"fields": {"subtasks": [
                {"key": "REL-52458"}, {"key": "REL-52448"}, {"key": "REL-52449"}]}}
        return {"fields": {"status": {"name": "Open"}}}
    monkeypatch.setattr(jira_progress.jira_client, "fetch_issue", fake_issue)
    jira_progress.advance("/tmp/m.json", "stg")
    closed = {c[0] for c in wired if c[1] == "21"}
    assert closed == {"REL-52458"}                 # pre-bump closed despite higher key
    assert ("REL-52448", "11", None) in wired      # stg → In Progress
    assert not any(c[0] == "REL-52449" for c in wired)  # later subtask untouched


def test_display_order_falls_back_to_key_number_when_unavailable(wired, monkeypatch):
    # No parent subtasks field → key-number ordering (unchanged behavior).
    subs = [
        _subtask("REL-52448", "Update stg.yaml for Release train testing", "Open"),
        _subtask("REL-52458", "Bump *-services in their repos (if necessary)", "Open"),
    ]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    # wired's fetch_issue returns status-only (no subtasks) → fallback to key num.
    jira_progress.advance("/tmp/m.json", "stg")
    # By key number 52458 > 52448, so the pre-bump is NOT closed at stg (the old
    # behavior we fall back to when display order is unknown).
    closed = {c[0] for c in wired if c[1] == "21"}
    assert closed == set()
    assert ("REL-52448", "11", None) in wired


def test_transition_to_category_matches_by_category_not_name(monkeypatch):
    # The release parent's only in-progress transition targets "Selected for
    # Development" (category indeterminate), NOT a status named "In Progress" -
    # matching by category is what makes the move succeed.
    calls = []
    monkeypatch.setattr(jira_progress.jira_client, "get_issue_transitions",
        lambda k, t, **kw: [
            {"id": "31", "name": "Selected for Development",
             "to": {"name": "Selected for Development",
                    "statusCategory": {"key": "indeterminate"}}},
        ])
    monkeypatch.setattr(jira_progress.jira_client, "transition_issue",
                        lambda key, tid, token, **kw: calls.append((key, tid)))
    assert jira_progress._transition_to_category("REL-REL", "indeterminate", "tok") is True
    assert calls == [("REL-REL", "31")]
    # No status in the 'done' category available → no move.
    calls.clear()
    assert jira_progress._transition_to_category("REL-REL", "done", "tok") is False
    assert calls == []


def test_close_prefix_before_skips_when_boundary_absent(wired, monkeypatch):
    # No "Run platform tests …" subtask → refuse to close the whole train.
    subs = [
        _subtask("REL-52055", "Bump *-services in their repos (if necessary)", "Open"),
        _subtask("REL-52056", "Update stg.yaml for Release train testing", "Open"),
    ]
    monkeypatch.setattr(jira_progress.jira_client, "fetch_subtasks", lambda rel, t: subs)
    jira_progress.close_prefix_before("/tmp/m.json", "test_platform")
    assert wired == []
