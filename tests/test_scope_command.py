"""`conductor scope {status,integrate,defer}` - the two late-add-in remedies."""
from types import SimpleNamespace

import pytest

from conductor import manifest as M
from conductor.commands import scope


def _mf(tickets, actions=None, version_id="31345"):
    return M.Manifest(
        release_version_id=version_id, release_label="2026-08-06", generated_at="now",
        tickets=[M.TicketEntry(key=k, issue_id=k.split("-")[1], summary=f"t {k}",
                               status="Open", issue_type="Task") for k in tickets],
        actions=actions or [],
    )


def _test_action(scope_keys):
    return {"at": "2026-08-06T02:33:00", "stage": "test", "summary": "triggered",
            "url": "https://ci/q/1/", "scope": scope_keys}


def _write(tmp_path, mf):
    p = tmp_path / f"release-{mf.release_label}.json"
    M.write_manifest(mf, p)
    return p


# ── integrate ────────────────────────────────────────────────────────────────

def test_integrate_dry_run_writes_nothing(tmp_path, capsys):
    mf = _mf(["REL-1", "REL-2"], actions=[_test_action(["REL-1"])])
    p = _write(tmp_path, mf)
    rc = scope.run(SimpleNamespace(scope_cmd="integrate", manifest=str(p), apply=False, yes=False))
    assert rc == 0
    assert "dry-run" in capsys.readouterr().out.lower()
    assert M.read_manifest(p).test_scope_keys() == ["REL-1"]  # unchanged


def test_integrate_apply_rebaselines_and_clears_drift(tmp_path):
    mf = _mf(["REL-1", "REL-2"], actions=[_test_action(["REL-1"])])
    p = _write(tmp_path, mf)
    assert [t.key for t in M.read_manifest(p).tickets_added_after_test()] == ["REL-2"]
    rc = scope.run(SimpleNamespace(scope_cmd="integrate", manifest=str(p), apply=True, yes=True))
    assert rc == 0
    reloaded = M.read_manifest(p)
    assert reloaded.test_scope_keys() == ["REL-1", "REL-2"]
    assert reloaded.tickets_added_after_test() == []


def test_integrate_noop_when_no_drift(tmp_path, capsys):
    mf = _mf(["REL-1"], actions=[_test_action(["REL-1"])])
    p = _write(tmp_path, mf)
    rc = scope.run(SimpleNamespace(scope_cmd="integrate", manifest=str(p), apply=True, yes=True))
    assert rc == 0
    assert "No scope drift" in capsys.readouterr().out


# ── defer ────────────────────────────────────────────────────────────────────

@pytest.fixture
def _jira(monkeypatch):
    monkeypatch.setattr(scope.config, "get_jira_token", lambda: "tok")
    monkeypatch.setattr(scope.config, "get_jira_email", lambda: "e@x")
    monkeypatch.setattr(scope.config, "JIRA_PROJECT", "REL")
    monkeypatch.setattr(scope.jira_client, "fetch_project_versions", lambda proj, tok: [
        {"id": "31345", "name": "2026-08-06 (CURRENT)"},
        {"id": "31346", "name": "2026-08-11 (NEXT)"},
    ])
    calls = {"updates": []}
    monkeypatch.setattr(scope.jira_client, "fetch_issue",
                        lambda key, tok, fields=None: {"fields": {
                            "fixVersions": [{"id": "31345", "name": "2026-08-06 (CURRENT)"}],
                            "summary": f"t {key}"}})
    monkeypatch.setattr(scope.jira_client, "update_issue_fix_versions",
                        lambda key, ids, tok: calls["updates"].append((key, ids)))
    return calls


def test_defer_dry_run_plans_move_without_writing(tmp_path, capsys, _jira):
    mf = _mf(["REL-1", "REL-9"], actions=[_test_action(["REL-1"])])  # REL-9 is the late add
    p = _write(tmp_path, mf)
    rc = scope.run(SimpleNamespace(scope_cmd="defer", manifest=str(p), tickets=None,
                                   apply=False, yes=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "2026-08-11 (NEXT)" in out and "REL-9" in out
    assert _jira["updates"] == []  # dry-run: no Jira write


def test_defer_apply_reassigns_fixversion_to_next(tmp_path, _jira):
    mf = _mf(["REL-1", "REL-9"], actions=[_test_action(["REL-1"])])
    p = _write(tmp_path, mf)
    rc = scope.run(SimpleNamespace(scope_cmd="defer", manifest=str(p), tickets=None,
                                   apply=True, yes=True))
    assert rc == 0
    # REL-9 moved: 31345 (CURRENT) dropped, 31346 (NEXT) added.
    assert _jira["updates"] == [("REL-9", ["31346"])]


def test_defer_errors_without_next_version(tmp_path, monkeypatch, _jira):
    monkeypatch.setattr(scope.jira_client, "fetch_project_versions",
                        lambda proj, tok: [{"id": "31345", "name": "2026-08-06 (CURRENT)"}])
    mf = _mf(["REL-1", "REL-9"], actions=[_test_action(["REL-1"])])
    p = _write(tmp_path, mf)
    with pytest.raises(SystemExit):
        scope.run(SimpleNamespace(scope_cmd="defer", manifest=str(p), tickets=None,
                                  apply=True, yes=True))
