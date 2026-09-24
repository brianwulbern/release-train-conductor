"""Finalize keeps the CURRENT→NEXT→OVERNEXT→SUBSEQUENT version ladder four deep,
creating missing slots on the release train's Tue/Thu cadence. A new version's
release date is its slot date; its start date is the day the prior release ships
(= the previous slot's release date)."""
from datetime import date

from conductor import manifest as M
from conductor.commands import finalize as F


# ── cadence helpers ─────────────────────────────────────────────────────────

def test_next_train_date_tue_to_thu_and_thu_to_tue():
    assert F._next_train_date(date(2026, 7, 7)) == date(2026, 7, 9)    # Tue → Thu
    assert F._next_train_date(date(2026, 7, 9)) == date(2026, 7, 14)   # Thu → next Tue


def test_prev_train_date_is_the_prior_ship_day():
    assert F._prev_train_date(date(2026, 7, 9)) == date(2026, 7, 7)    # Thu ← Tue
    assert F._prev_train_date(date(2026, 7, 7)) == date(2026, 7, 2)    # Tue ← prior Thu


def test_parse_release_date_strips_suffix():
    assert F._parse_release_date("2026-07-09 (NEXT)") == date(2026, 7, 9)
    assert F._parse_release_date("2026-07-07") == date(2026, 7, 7)
    assert F._parse_release_date("Backlog") is None


# ── ladder planning ─────────────────────────────────────────────────────────

def _shift(vid, old, new):
    return (vid, old, new)


def test_fills_the_vacated_subsequent_slot():
    # Typical steady state: NEXT/OVERNEXT/SUBSEQUENT all exist and shift forward,
    # leaving SUBSEQUENT empty → create exactly one, chained off OVERNEXT's date.
    shifts = [
        _shift("1", "2026-07-09 (NEXT)", "2026-07-09 (CURRENT)"),
        _shift("2", "2026-07-14 (OVERNEXT)", "2026-07-14 (NEXT)"),
        _shift("3", "2026-07-16 (SUBSEQUENT)", "2026-07-16 (OVERNEXT)"),
    ]
    versions = [
        {"name": "2026-07-09 (NEXT)"}, {"name": "2026-07-14 (OVERNEXT)"},
        {"name": "2026-07-16 (SUBSEQUENT)"}, {"name": "2026-07-07 (CURRENT)"},
    ]
    creations = F._plan_ladder_creations(versions, shifts, date(2026, 7, 7))
    # OVERNEXT after shift = 2026-07-16 → new SUBSEQUENT = next train = 2026-07-21,
    # start = the prior ship day 2026-07-16.
    assert creations == [("2026-07-21 (SUBSEQUENT)", "2026-07-16", "2026-07-21")]


def test_fills_multiple_gaps_when_ladder_is_short():
    # Only NEXT exists → after shift, CURRENT is filled, but NEXT/OVERNEXT/
    # SUBSEQUENT are all empty and get chained + created.
    shifts = [_shift("1", "2026-07-09 (NEXT)", "2026-07-09 (CURRENT)")]
    versions = [{"name": "2026-07-09 (NEXT)"}, {"name": "2026-07-07 (CURRENT)"}]
    creations = F._plan_ladder_creations(versions, shifts, date(2026, 7, 7))
    assert creations == [
        ("2026-07-14 (NEXT)", "2026-07-09", "2026-07-14"),
        ("2026-07-16 (OVERNEXT)", "2026-07-14", "2026-07-16"),
        ("2026-07-21 (SUBSEQUENT)", "2026-07-16", "2026-07-21"),
    ]


def test_no_creations_when_ladder_already_full():
    # All four slots occupied after the shift → nothing to create.
    shifts = [
        _shift("1", "2026-07-09 (NEXT)", "2026-07-09 (CURRENT)"),
        _shift("2", "2026-07-14 (OVERNEXT)", "2026-07-14 (NEXT)"),
        _shift("3", "2026-07-16 (SUBSEQUENT)", "2026-07-16 (OVERNEXT)"),
    ]
    # A version already occupies the would-be new SUBSEQUENT date → dedupe skips it.
    versions = [{"name": "2026-07-21 (SUBSEQUENT)"}]
    creations = F._plan_ladder_creations(versions, shifts, date(2026, 7, 7))
    assert creations == []


def test_dedupes_against_existing_date_unlabeled():
    # The computed SUBSEQUENT date already exists as a plain (unlabeled) version
    # → don't create a duplicate.
    shifts = [
        _shift("1", "2026-07-09 (NEXT)", "2026-07-09 (CURRENT)"),
        _shift("2", "2026-07-14 (OVERNEXT)", "2026-07-14 (NEXT)"),
        _shift("3", "2026-07-16 (SUBSEQUENT)", "2026-07-16 (OVERNEXT)"),
    ]
    versions = [{"name": "2026-07-21"}]  # plain, no suffix, same date
    creations = F._plan_ladder_creations(versions, shifts, date(2026, 7, 7))
    assert creations == []


# ── label shifts are DATE-anchored (self-heal a degraded ladder) ────────────

def test_shift_healthy_ladder_promotes_each_rung():
    versions = [
        {"id": "1", "name": "2026-08-04 (NEXT)"},
        {"id": "2", "name": "2026-08-06 (OVERNEXT)"},
        {"id": "3", "name": "2026-08-11 (SUBSEQUENT)"},
        {"id": "9", "name": "2026-07-30 (CURRENT)"},
    ]
    shifts = F._plan_label_shifts(versions, skip_version_id="9", after_date=date(2026, 7, 30))
    assert shifts == [
        ("1", "2026-08-04 (NEXT)", "2026-08-04 (CURRENT)"),
        ("2", "2026-08-06 (OVERNEXT)", "2026-08-06 (NEXT)"),
        ("3", "2026-08-11 (SUBSEQUENT)", "2026-08-11 (OVERNEXT)"),
    ]


def test_shift_heals_missing_next_rung():
    # The real 2026-07-30 stuck state: no (NEXT) rung. Label-anchored logic left
    # NO (CURRENT); date-anchored makes the earliest future train CURRENT.
    versions = [
        {"id": "1", "name": "2026-08-04 (OVERNEXT)"},
        {"id": "2", "name": "2026-08-06 (SUBSEQUENT)"},
    ]
    shifts = F._plan_label_shifts(versions, skip_version_id="9", after_date=date(2026, 7, 30))
    assert shifts == [
        ("1", "2026-08-04 (OVERNEXT)", "2026-08-04 (CURRENT)"),
        ("2", "2026-08-06 (SUBSEQUENT)", "2026-08-06 (NEXT)"),
    ]


def test_shift_ignores_archived_and_past_and_unlabeled():
    versions = [
        {"id": "old", "name": "2026-06-25 (CURRENT)", "archived": True},   # archived → skip
        {"id": "past", "name": "2026-07-28 (NEXT)"},                       # date ≤ shipped → skip
        {"id": "stray", "name": "2026-08-05"},                            # no suffix → never touched
        {"id": "keep", "name": "2026-08-04 (OVERNEXT)"},
    ]
    shifts = F._plan_label_shifts(versions, skip_version_id="9", after_date=date(2026, 7, 30))
    assert shifts == [("keep", "2026-08-04 (OVERNEXT)", "2026-08-04 (CURRENT)")]


def test_shift_leaves_correct_labels_untouched():
    # If a version already carries the right suffix for its date position, no rename.
    versions = [
        {"id": "1", "name": "2026-08-04 (CURRENT)"},
        {"id": "2", "name": "2026-08-06 (OVERNEXT)"},  # should be NEXT → rename
    ]
    shifts = F._plan_label_shifts(versions, skip_version_id="9", after_date=date(2026, 7, 30))
    assert shifts == [("2", "2026-08-06 (OVERNEXT)", "2026-08-06 (NEXT)")]


# ── run() integration: apply path creates the vacated slot ──────────────────

def test_apply_creates_missing_version(monkeypatch):
    from types import SimpleNamespace

    created = []
    monkeypatch.setattr(F.M, "read_manifest", lambda p: SimpleNamespace(
        release_version_id="29904", release_label="2026-07-07", tickets=[]))
    monkeypatch.setattr(F.M, "record_action", lambda *a, **k: None)
    monkeypatch.setattr(F.config, "get_jira_token", lambda: "tok")
    monkeypatch.setattr(F.config, "get_jira_email", lambda: "e@x")
    # CURRENT release version: already renamed+released, carries a projectId.
    monkeypatch.setattr(F.jira_client, "fetch_version", lambda vid, tok: {
        "name": "2026-07-07 (CURRENT)", "released": False, "releaseDate": "",
        "projectId": 10001})
    monkeypatch.setattr(F.jira_client, "fetch_version_issues",
                        lambda vid, tok, fields=None: [])
    # Steady-state ladder: NEXT/OVERNEXT/SUBSEQUENT present → shift, then create
    # the new SUBSEQUENT.
    monkeypatch.setattr(F.jira_client, "fetch_project_versions", lambda proj, tok: [
        {"id": "1", "name": "2026-07-09 (NEXT)"},
        {"id": "2", "name": "2026-07-14 (OVERNEXT)"},
        {"id": "3", "name": "2026-07-16 (SUBSEQUENT)"},
    ])
    monkeypatch.setattr(F.jira_client, "update_version", lambda *a, **k: {})
    monkeypatch.setattr(F.jira_progress, "open_release_subtasks", lambda mf, tok: [])

    def _create(project_id, name, tok, *, start_date=None, release_date=None):
        created.append((project_id, name, start_date, release_date))
        return {"id": "99"}
    monkeypatch.setattr(F.jira_client, "create_version", _create)

    args = SimpleNamespace(manifest="/x.json", apply=True, yes=True,
                           no_shift_labels=False, no_extend_ladder=False,
                           no_release_steps=True, no_release_latest=True)  # steps/latest tested separately
    rc = F.run(args)
    assert rc == 0
    assert created == [(10001, "2026-07-21 (SUBSEQUENT)", "2026-07-16", "2026-07-21")]


def test_apply_clones_release_steps_for_new_version(monkeypatch):
    """When finalize creates a new SUBSEQUENT version, it also clones the
    release-steps ticket for that train (Task + subtasks)."""
    from types import SimpleNamespace

    monkeypatch.setattr(F.M, "read_manifest", lambda p: SimpleNamespace(
        release_version_id="29904", release_label="2026-07-07", tickets=[]))
    monkeypatch.setattr(F.M, "record_action", lambda *a, **k: None)
    monkeypatch.setattr(F.config, "get_jira_token", lambda: "tok")
    monkeypatch.setattr(F.config, "get_jira_email", lambda: "e@x")
    monkeypatch.setattr(F.jira_client, "fetch_version", lambda vid, tok: {
        "name": "2026-07-07 (CURRENT)", "released": False, "releaseDate": "",
        "projectId": 10001})
    monkeypatch.setattr(F.jira_client, "fetch_version_issues", lambda vid, tok, fields=None: [])
    monkeypatch.setattr(F.jira_client, "fetch_project_versions", lambda proj, tok: [
        {"id": "1", "name": "2026-07-09 (NEXT)"},
        {"id": "2", "name": "2026-07-14 (OVERNEXT)"},
        {"id": "3", "name": "2026-07-16 (SUBSEQUENT)"},
    ])
    monkeypatch.setattr(F.jira_client, "update_version", lambda *a, **k: {})
    # create_version returns the new version's id so the steps clone can tag it.
    monkeypatch.setattr(F.jira_client, "create_version", lambda *a, **k: {"id": "29911"})
    monkeypatch.setattr(F.jira_progress, "open_release_subtasks", lambda mf, tok: [])

    # Stub the release_steps building blocks finalize calls.
    tmpl = {"fields": {"summary": "[Release] Release Steps for Release Train for YYYY-MM-DD [TEMPLATE]"}}
    monkeypatch.setattr(F.release_steps, "fetch_template", lambda tok: (tmpl, [{"x": 1}, {"x": 2}]))
    monkeypatch.setattr(F.release_steps, "existing_for_date", lambda date, tok: [])
    cloned = []
    monkeypatch.setattr(F.release_steps, "create_ticket",
                        lambda date, tok, t, s, fix_version_id=None, indent="":
                        cloned.append((date, fix_version_id)) or ("REL-999", 2, 0))

    args = SimpleNamespace(manifest="/x.json", apply=True, yes=True,
                           no_shift_labels=False, no_extend_ladder=False,
                           no_release_steps=False, no_release_latest=True)
    rc = F.run(args)
    assert rc == 0
    # steps ticket cloned for the new train, tagged with the version just created
    assert cloned == [("2026-07-21", "29911")]


def test_apply_promotes_prereleases_to_latest(monkeypatch):
    """Finalize flips this cycle's GitHub pre-release tags to full/latest - only
    the ones still marked prerelease, on platform/service repos."""
    from types import SimpleNamespace

    monkeypatch.setattr(F.M, "read_manifest", lambda p: SimpleNamespace(
        release_version_id="29904", release_label="2026-07-07", tickets=[],
        repos=[
            M.RepoEntry(repo="api", classification="platform", release_tag="5.214.0"),
            M.RepoEntry(repo="service-backup", classification="service", release_tag="5.36.0"),
            M.RepoEntry(repo="web-console", classification="platform", release_tag="6.105.0"),
            M.RepoEntry(repo="functional-tests", classification="other"),  # not taggable
        ]))
    monkeypatch.setattr(F.M, "record_action", lambda *a, **k: None)
    monkeypatch.setattr(F.config, "get_jira_token", lambda: "tok")
    monkeypatch.setattr(F.config, "get_jira_email", lambda: "e@x")
    monkeypatch.setattr(F.config, "get_github_token", lambda: "ghtok")
    # Already-finalized version (stripped + released): release-latest still runs.
    monkeypatch.setattr(F.jira_client, "fetch_version", lambda vid, tok: {
        "name": "2026-07-07", "released": True, "releaseDate": "2026-07-07", "projectId": 10001})
    monkeypatch.setattr(F.jira_client, "fetch_version_issues", lambda vid, tok, fields=None: [])
    monkeypatch.setattr(F.jira_progress, "open_release_subtasks", lambda mf, tok: [])

    # api: still a pre-release; service-backup: pre-release; console: already full.
    releases = {
        ("api", "5.214.0"): {"id": "1", "prerelease": True},
        ("service-backup", "5.36.0"): {"id": "2", "prerelease": True},
        ("web-console", "6.105.0"): {"id": "3", "prerelease": False},
    }
    monkeypatch.setattr(F.github_client, "get_release_by_tag",
                        lambda repo, tag, tok: releases.get((repo, tag)))
    promoted = []
    monkeypatch.setattr(F.github_client, "mark_release_latest",
                        lambda repo, rid, tok: promoted.append((repo, rid)) or {})

    args = SimpleNamespace(manifest="/x.json", apply=True, yes=True,
                           no_shift_labels=True,  # isolate: no ladder/steps this test
                           no_extend_ladder=True, no_release_steps=True,
                           no_release_latest=False)
    rc = F.run(args)
    assert rc == 0
    # only the two pre-releases flipped; console (already full) untouched.
    assert sorted(promoted) == [("api", "1"), ("service-backup", "2")]


def test_release_steps_skipped_when_date_exists(monkeypatch):
    """A train that already has a steps ticket isn't cloned again."""
    from types import SimpleNamespace

    monkeypatch.setattr(F.M, "read_manifest", lambda p: SimpleNamespace(
        release_version_id="29904", release_label="2026-07-07", tickets=[]))
    monkeypatch.setattr(F.M, "record_action", lambda *a, **k: None)
    monkeypatch.setattr(F.config, "get_jira_token", lambda: "tok")
    monkeypatch.setattr(F.config, "get_jira_email", lambda: "e@x")
    monkeypatch.setattr(F.jira_client, "fetch_version", lambda vid, tok: {
        "name": "2026-07-07 (CURRENT)", "released": False, "releaseDate": "", "projectId": 10001})
    monkeypatch.setattr(F.jira_client, "fetch_version_issues", lambda vid, tok, fields=None: [])
    monkeypatch.setattr(F.jira_client, "fetch_project_versions", lambda proj, tok: [
        {"id": "1", "name": "2026-07-09 (NEXT)"},
        {"id": "2", "name": "2026-07-14 (OVERNEXT)"},
        {"id": "3", "name": "2026-07-16 (SUBSEQUENT)"},
    ])
    monkeypatch.setattr(F.jira_client, "update_version", lambda *a, **k: {})
    monkeypatch.setattr(F.jira_client, "create_version", lambda *a, **k: {"id": "99"})
    monkeypatch.setattr(F.jira_progress, "open_release_subtasks", lambda mf, tok: [])
    tmpl = {"fields": {"summary": "[Release] Release Steps for Release Train for YYYY-MM-DD [TEMPLATE]"}}
    monkeypatch.setattr(F.release_steps, "fetch_template", lambda tok: (tmpl, []))
    monkeypatch.setattr(F.release_steps, "existing_for_date", lambda date, tok: ["REL-55555"])
    cloned = []
    monkeypatch.setattr(F.release_steps, "create_ticket",
                        lambda *a, **k: cloned.append(1) or ("REL-999", 0, 0))
    args = SimpleNamespace(manifest="/x.json", apply=True, yes=True,
                           no_shift_labels=False, no_extend_ladder=False, no_release_steps=False, no_release_latest=True)
    assert F.run(args) == 0
    assert cloned == []   # existing → skipped
