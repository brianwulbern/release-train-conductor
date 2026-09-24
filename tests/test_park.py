"""`conductor park` - park the current train (missed its release window) and advance the
CURRENT→NEXT→OVERNEXT→SUBSEQUENT ladder to the next train, WITHOUT releasing.

The motivating live case: the 2026-09-03 (Thu) train missed its window; we want it
to stay a distinct UNRELEASED version with its tickets, while 2026-09-08 (Tue)
becomes CURRENT and the ladder slides forward.
"""
from types import SimpleNamespace

import pytest

from conductor.commands import park as P


def _v(vid, name, archived=False, released=False, project=10881):
    return {"id": vid, "name": name, "archived": archived, "released": released,
            "projectId": project}


# The live ladder: 09-03 is the (CURRENT) that missed its window.
LIVE_VERSIONS = [
    _v("100", "2026-09-03 (CURRENT)"),
    _v("101", "2026-09-08 (NEXT)"),
    _v("102", "2026-09-10 (OVERNEXT)"),
    _v("103", "2026-09-15 (SUBSEQUENT)"),
]


def _args(**over):
    base = dict(version_id=None, apply=False, yes=True,
                no_extend_ladder=False, no_release_steps=True)
    base.update(over)
    return SimpleNamespace(**base)


def _wire(monkeypatch, versions=None, capture=None):
    """Common Jira stubs. `capture` (a dict) records writes when apply-testing."""
    versions = versions if versions is not None else [dict(v) for v in LIVE_VERSIONS]
    monkeypatch.setattr(P.config, "get_jira_token", lambda: "tok")
    monkeypatch.setattr(P.config, "get_jira_email", lambda: "e@x")
    monkeypatch.setattr(P.jira_client, "fetch_project_versions", lambda proj, tok: versions)
    monkeypatch.setattr(P.jira_client, "find_current_version",
                        lambda proj, tok: next(
                            (v for v in versions
                             if "(CURRENT)" in (v.get("name") or "")
                             and not v.get("archived") and not v.get("released")), None))
    if capture is not None:
        def _upd(vid, tok, *, name=None, **k):
            capture.setdefault("renamed", []).append((str(vid), name))
            for v in versions:                    # mutate in place → realistic re-reads
                if str(v.get("id")) == str(vid) and name is not None:
                    v["name"] = name
            return {}

        def _create(project_id, name, tok, *, start_date=None, release_date=None):
            capture.setdefault("created", []).append((name, start_date, release_date))
            versions.append(_v("999", name))
            return {"id": "999"}
        monkeypatch.setattr(P.jira_client, "update_version", _upd)
        monkeypatch.setattr(P.jira_client, "create_version", _create)
    return versions


# ── _resolve_new_current ────────────────────────────────────────────────────

def test_resolve_new_current_picks_earliest_future_excluding_parked():
    vid, name = P._resolve_new_current(
        [dict(v) for v in LIVE_VERSIONS], skip_id="100",
        after_date=P.finalize._parse_release_date("2026-09-03"))
    assert (vid, name) == ("101", "2026-09-08 (CURRENT)")


def test_resolve_new_current_none_when_no_future_ladder():
    # Only the parked train exists → the new CURRENT must be created (id unknown).
    vid, name = P._resolve_new_current(
        [_v("100", "2026-09-03 (CURRENT)")], skip_id="100",
        after_date=P.finalize._parse_release_date("2026-09-03"))
    assert vid is None and name is None


# ── dry-run (no writes) ─────────────────────────────────────────────────────

def test_dry_run_plans_but_writes_nothing(monkeypatch, capsys):
    _wire(monkeypatch)
    # Any write attempt in dry-run is a bug.
    monkeypatch.setattr(P.jira_client, "update_version",
                        lambda *a, **k: pytest.fail("dry-run must not write"))
    monkeypatch.setattr(P.jira_client, "create_version",
                        lambda *a, **k: pytest.fail("dry-run must not write"))
    rc = P.run(_args(apply=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "2026-09-03 (CURRENT)' → '2026-09-03'" in out         # strip, unreleased
    assert "2026-09-08 (NEXT)' → '2026-09-08 (CURRENT)'" in out  # promote
    assert "new CURRENT train: 2026-09-08 (CURRENT)" in out
    assert "conductor manifest build 101" in out                       # exact follow-up
    assert "Dry-run" in out


# ── apply ───────────────────────────────────────────────────────────────────

def test_apply_parks_shifts_and_extends(monkeypatch, capsys):
    cap = {}
    versions = _wire(monkeypatch, capture=cap)
    rc = P.run(_args(apply=True))
    out = capsys.readouterr().out
    assert rc == 0
    renamed = dict(cap["renamed"])
    # parked train demoted (label stripped), NOT released.
    assert renamed["100"] == "2026-09-03"
    # ladder slid forward.
    assert renamed["101"] == "2026-09-08 (CURRENT)"
    assert renamed["102"] == "2026-09-10 (NEXT)"
    assert renamed["103"] == "2026-09-15 (OVERNEXT)"
    # vacated SUBSEQUENT recreated on the Tue/Thu cadence (Thu after 09-15 = 09-17).
    assert cap["created"] == [("2026-09-17 (SUBSEQUENT)", "2026-09-15", "2026-09-17")]
    # exact follow-up build command for the resolved new CURRENT.
    assert "conductor manifest build 101" in out


def test_apply_never_marks_released(monkeypatch):
    """Park must leave the parked train UNRELEASED - no release write, ever."""
    _wire(monkeypatch, capture={})
    seen = {}

    def _upd(vid, tok, *, name=None, released=None, release_date=None, **k):
        seen[str(vid)] = {"name": name, "released": released}
        return {}
    monkeypatch.setattr(P.jira_client, "update_version", _upd)
    monkeypatch.setattr(P.jira_client, "create_version", lambda *a, **k: {"id": "999"})
    P.run(_args(apply=True))
    assert seen  # writes happened
    assert all(v["released"] in (None, False) for v in seen.values())


def test_no_extend_ladder_skips_creations(monkeypatch):
    cap = {}
    _wire(monkeypatch, capture=cap)
    rc = P.run(_args(apply=True, no_extend_ladder=True))
    assert rc == 0
    assert "created" not in cap        # no versions created


# ── guardrails ──────────────────────────────────────────────────────────────

def test_errors_when_no_current_version(monkeypatch):
    _wire(monkeypatch, versions=[_v("1", "2026-09-08 (NEXT)")])
    with pytest.raises(SystemExit) as e:
        P.run(_args())
    assert "no active '(CURRENT)'" in str(e.value)


def test_version_id_without_current_suffix_errors(monkeypatch):
    _wire(monkeypatch, versions=[_v("55", "2026-09-08 (NEXT)")])
    with pytest.raises(SystemExit) as e:
        P.run(_args(version_id="55"))
    assert "no ' (CURRENT)' suffix" in str(e.value)
