"""Release-ladder health assessment - detect a degraded CURRENT/NEXT/OVERNEXT/
SUBSEQUENT Jira ladder and plan its date-anchored repair.

The motivating state is the live 2026-07-30 one: an active ladder missing its
(CURRENT) (the earliest future train was mislabeled (NEXT)) plus a stale,
archived `2026-06-25 (CURRENT)`."""
from datetime import date

from conductor import ladder as L


def _v(vid, name, archived=False, released=False, project=10881):
    return {"id": vid, "name": name, "archived": archived, "released": released,
            "projectId": project}


TODAY = date(2026, 7, 30)


def test_healthy_ladder_is_ok_no_repair():
    versions = [
        _v("1", "2026-08-04 (CURRENT)"),
        _v("2", "2026-08-06 (NEXT)"),
        _v("3", "2026-08-11 (OVERNEXT)"),
        _v("4", "2026-08-13 (SUBSEQUENT)"),
    ]
    h = L.assess(versions, TODAY)
    assert h.ok and not h.needs_repair and not h.issues
    assert h.renames == [] and h.creations == []


def test_degraded_missing_current_is_blocking_and_planned():
    # The real stuck ladder: no (NEXT) rung → nothing is (CURRENT).
    versions = [
        _v("1", "2026-08-04 (NEXT)"),
        _v("2", "2026-08-06 (OVERNEXT)"),
        _v("3", "2026-08-11 (SUBSEQUENT)"),
    ]
    h = L.assess(versions, TODAY)
    assert not h.ok
    assert any("no active (CURRENT)" in i for i in h.issues)
    assert h.renames == [
        ("1", "2026-08-04 (NEXT)", "2026-08-04 (CURRENT)"),
        ("2", "2026-08-06 (OVERNEXT)", "2026-08-06 (NEXT)"),
        ("3", "2026-08-11 (SUBSEQUENT)", "2026-08-11 (OVERNEXT)"),
    ]
    # short by one → creates the vacated SUBSEQUENT (Thu after 08-11 Tue = 08-13)
    assert h.creations == [("2026-08-13 (SUBSEQUENT)", "2026-08-11", "2026-08-13")]


def test_stale_archived_current_is_advisory_not_blocking():
    versions = [
        _v("old", "2026-06-25 (CURRENT)", archived=True),
        _v("1", "2026-08-04 (CURRENT)"),
        _v("2", "2026-08-06 (NEXT)"),
        _v("3", "2026-08-11 (OVERNEXT)"),
        _v("4", "2026-08-13 (SUBSEQUENT)"),
    ]
    h = L.assess(versions, TODAY)
    assert h.ok                      # archived stale doesn't block (announce ignores it)
    assert h.stale_current and h.stale_current[0]["id"] == "old"
    assert any("stale (CURRENT)" in a for a in h.advisories)


def test_multiple_active_current_flagged():
    versions = [
        _v("1", "2026-08-04 (CURRENT)"),
        _v("2", "2026-08-06 (CURRENT)"),
    ]
    h = L.assess(versions, TODAY)
    assert not h.ok
    assert any("2 active versions are labeled (CURRENT)" in i for i in h.issues)


def test_past_dated_and_released_versions_excluded():
    versions = [
        _v("past", "2026-07-28 (CURRENT)"),          # before today → not the ladder
        _v("done", "2026-08-04 (CURRENT)", released=True),  # released → excluded
        _v("1", "2026-08-06 (CURRENT)"),
    ]
    h = L.assess(versions, TODAY)
    # only the active future one counts; it's earliest → correctly (CURRENT)
    assert [t[1] for t in h.active] == ["1"]
    assert h.ok


def test_short_ladder_creates_up_to_four():
    versions = [_v("1", "2026-08-04 (CURRENT)")]
    h = L.assess(versions, TODAY)
    assert h.ok  # has an active CURRENT, correctly labeled → not blocking
    names = [c[0] for c in h.creations]
    assert names == ["2026-08-06 (NEXT)", "2026-08-11 (OVERNEXT)", "2026-08-13 (SUBSEQUENT)"]


def test_empty_ladder_anchors_from_today():
    h = L.assess([], TODAY)  # 2026-07-30 is a Thursday → next train Tue 08-04
    assert [c[0] for c in h.creations] == [
        "2026-08-04 (CURRENT)", "2026-08-06 (NEXT)",
        "2026-08-11 (OVERNEXT)", "2026-08-13 (SUBSEQUENT)",
    ]
