"""find_current_version must resolve the ACTIVE current train, never a stale
'(CURRENT)' left on an archived/released version.

Real incident (2026-07-30): an archived `2026-06-25 (CURRENT)` lingered in Jira
and `conductor announce` resolved it instead of the live train."""
from conductor import jira_client as J


def _wire(monkeypatch, versions):
    monkeypatch.setattr(J, "fetch_project_versions", lambda proj, tok: versions)


def test_ignores_archived_current(monkeypatch):
    _wire(monkeypatch, [
        {"id": "29316", "name": "2026-06-25 (CURRENT)", "archived": True, "released": False},
        {"id": "31306", "name": "2026-08-04 (CURRENT)", "archived": False, "released": False},
    ])
    v = J.find_current_version("REL", "tok")
    assert v["id"] == "31306"


def test_ignores_released_current(monkeypatch):
    _wire(monkeypatch, [
        {"id": "1", "name": "2026-06-25 (CURRENT)", "archived": False, "released": True},
        {"id": "2", "name": "2026-08-04 (CURRENT)", "archived": False, "released": False},
    ])
    assert J.find_current_version("REL", "tok")["id"] == "2"


def test_none_when_only_archived_current(monkeypatch):
    # The exact stuck state: the only '(CURRENT)' is archived → return None so the
    # caller errors clearly instead of announcing a month-old train.
    _wire(monkeypatch, [
        {"id": "29316", "name": "2026-06-25 (CURRENT)", "archived": True, "released": False},
        {"id": "31306", "name": "2026-08-04 (NEXT)", "archived": False, "released": False},
    ])
    assert J.find_current_version("REL", "tok") is None


def test_multiple_active_current_prefers_highest_id(monkeypatch):
    _wire(monkeypatch, [
        {"id": "10", "name": "2026-08-04 (CURRENT)", "archived": False, "released": False},
        {"id": "20", "name": "2026-08-06 (CURRENT)", "archived": False, "released": False},
    ])
    assert J.find_current_version("REL", "tok")["id"] == "20"
