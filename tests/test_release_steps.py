"""`conductor release steps` - rebuild the release-steps template (REL-47502) for a
new train: a parent Task (date-swapped summary, [TEMPLATE] dropped) plus its
Technical-Task subtasks, via direct REST creates (no async clone)."""
from types import SimpleNamespace

import pytest

from conductor.commands import release_steps as RS


_TMPL = {
    "key": "REL-47502",
    "fields": {
        "summary": "[Release] Release Steps for Release Train for YYYY-MM-DD [TEMPLATE]",
        "issuetype": {"id": "10002", "name": "Task"},
        "components": [{"id": "11564", "name": "Releases"}],
        "labels": ["release-saas-squad"],
        "priority": {"id": "4", "name": "Low"},
        "description": "## Requirements \n\n- [ ] \n\n",
    },
}
_SUBS = [
    {"key": "REL-47505", "fields": {"summary": "Update stg.yaml for Release train testing",
                                    "issuetype": {"id": "10153"}, "priority": {"id": "4"},
                                    "description": "do the thing"}},
    {"key": "REL-47506", "fields": {"summary": "Validate API migrations",
                                    "issuetype": {"id": "10153"}, "priority": {"id": "4"}}},
]


# ── pure helpers ────────────────────────────────────────────────────────────

def test_new_summary_swaps_date_and_drops_template():
    assert RS._new_summary(_TMPL["fields"]["summary"], "2026-07-16") == \
        "[Release] Release Steps for Release Train for 2026-07-16"


def test_parent_fields_copies_type_labels_components_priority():
    f = RS._parent_fields(_TMPL, "[Release] ... 2026-07-16")
    assert f["project"] == {"key": "REL"}
    assert f["issuetype"] == {"id": "10002"}
    assert f["labels"] == ["release-saas-squad"]
    assert f["components"] == [{"id": "11564"}]
    assert f["priority"] == {"id": "4"}
    assert f["description"].startswith("## Requirements")


def test_subtask_fields_sets_parent_and_type():
    f = RS._subtask_fields(_SUBS[0], "REL-99999")
    assert f["parent"] == {"key": "REL-99999"}
    assert f["issuetype"] == {"id": "10153"}
    assert f["summary"] == "Update stg.yaml for Release train testing"
    assert f["description"] == "do the thing"


def test_subtask_without_description_omits_it():
    f = RS._subtask_fields(_SUBS[1], "REL-99999")
    assert "description" not in f


# ── run() ───────────────────────────────────────────────────────────────────

def test_fetch_template_orders_by_display_not_key(monkeypatch):
    # The template's display order puts pre-bump ("Bump *-services") FIRST, but
    # its key (REL-48284) sorts AFTER the others - a key-ordered fetch would put
    # it LAST, which makes advancing the `pre` stage close the whole train above
    # it. fetch_template must take order from the parent's fields.subtasks.
    parent = {"fields": {"subtasks": [
        {"key": "REL-48284"},   # Bump *-services - first in display, HIGHEST key
        {"key": "REL-47505"},   # Update stg.yaml
        {"key": "REL-47506"},   # Validate API migrations
    ]}}
    # search_issues returns them key-ordered (the wrong order for the train).
    detailed = [
        {"key": "REL-47505", "fields": {"summary": "Update stg.yaml for Release train testing"}},
        {"key": "REL-47506", "fields": {"summary": "Validate API migrations"}},
        {"key": "REL-48284", "fields": {"summary": "Bump *-services in their repos (if necessary)"}},
    ]
    monkeypatch.setattr(RS.jira_client, "fetch_issue", lambda key, tok, fields=None: parent)
    monkeypatch.setattr(RS.jira_client, "search_issues",
                        lambda jql, tok, fields=None, **kw: detailed)
    _tmpl, subs = RS.fetch_template("tok")
    assert [s["key"] for s in subs] == ["REL-48284", "REL-47505", "REL-47506"]
    assert subs[0]["fields"]["summary"].startswith("Bump *-services")


def _wire(monkeypatch, *, existing=None, created_holder=None, versions=None):
    monkeypatch.setattr(RS.config, "get_jira_token", lambda: "tok")
    monkeypatch.setattr(RS.config, "get_jira_email", lambda: "e@x")
    monkeypatch.setattr(RS.jira_client, "fetch_issue", lambda key, tok, fields=None: _TMPL)
    monkeypatch.setattr(RS.jira_client, "fetch_project_versions",
                        lambda proj, tok: versions if versions is not None
                        else [{"id": "29910", "name": "2026-07-16 (SUBSEQUENT)"}])

    def _search(jql, tok, fields=None, **kw):
        if jql.startswith("parent ="):
            return _SUBS
        # dedupe search
        return existing or []
    monkeypatch.setattr(RS.jira_client, "search_issues", _search)

    counter = {"n": 0}
    def _create(fields, tok):
        counter["n"] += 1
        key = "REL-90000" if "parent" not in fields else f"REL-9000{counter['n']}"
        if created_holder is not None:
            created_holder.append((key, fields))
        return {"key": key}
    monkeypatch.setattr(RS.jira_client, "create_issue", _create)

    # New in v0.47.0: assign-to-me + link-to-template (both best-effort).
    monkeypatch.setattr(RS.jira_client, "get_myself", lambda tok: {"accountId": "acc-me"})
    monkeypatch.setattr(RS.jira_client, "get_issue_link_types",
                        lambda tok: [{"name": "Blocks"}, {"name": "Relates"}])
    assigns = []
    links = []
    monkeypatch.setattr(RS.jira_client, "assign_issue",
                        lambda key, acc, tok: assigns.append((key, acc)))
    monkeypatch.setattr(RS.jira_client, "link_issues",
                        lambda ink, out, typ, tok: links.append((ink, out, typ)))
    return {"assigns": assigns, "links": links}


def _args(**kw):
    base = dict(date="2026-07-16", apply=False, yes=True, force=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_dry_run_makes_no_creates(monkeypatch, capsys):
    created = []
    _wire(monkeypatch, created_holder=created)
    rc = RS.run(_args(apply=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert created == []                       # nothing created in dry-run
    assert "create Task" in out and "2 subtask" in out
    assert "Dry-run" in out


def test_apply_creates_parent_and_subtasks(monkeypatch, capsys):
    created = []
    sink = _wire(monkeypatch, created_holder=created)
    rc = RS.run(_args(apply=True))
    assert rc == 0
    # 1 parent + 2 subtasks
    assert len(created) == 3
    parent_key, parent_fields = created[0]
    assert parent_fields["summary"] == "[Release] Release Steps for Release Train for 2026-07-16"
    assert "parent" not in parent_fields
    # parent tagged with the resolved train version → announce can discover it
    assert parent_fields["fixVersions"] == [{"id": "29910"}]
    # subtasks reference the created parent
    for _key, f in created[1:]:
        assert f["parent"] == {"key": parent_key}
    # parent assigned to the caller, and linked to the template (Relates, not Blocks)
    assert sink["assigns"] == [(parent_key, "acc-me")]
    assert sink["links"] == [(parent_key, "REL-47502", "Relates")]


def test_no_version_found_creates_without_fixversion(monkeypatch, capsys):
    created = []
    _wire(monkeypatch, created_holder=created, versions=[])  # no matching version
    rc = RS.run(_args(apply=True))
    out = capsys.readouterr().out
    assert rc == 0
    _key, parent_fields = created[0]
    assert "fixVersions" not in parent_fields
    assert "none found" in out


def test_resolve_version_id_matches_date_prefix(monkeypatch):
    monkeypatch.setattr(RS.jira_client, "fetch_project_versions", lambda proj, tok: [
        {"id": "1", "name": "2026-07-14 (NEXT)"},
        {"id": "2", "name": "2026-07-16 (SUBSEQUENT)"},
        {"id": "3", "name": "2026-07-16"},  # bare, also matches
    ])
    assert RS.resolve_version_id("2026-07-16", "tok") == "2"


def test_dedupe_skips_when_existing_and_no_force(monkeypatch, capsys):
    created = []
    _wire(monkeypatch,
          existing=[{"key": "REL-52350",
                     "fields": {"summary": "[Release] Release Steps for Release Train for 2026-07-16"}}],
          created_holder=created)
    rc = RS.run(_args(apply=True, force=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert created == []                       # skipped - would duplicate
    assert "already exists" in out


def test_bad_date_exits(monkeypatch):
    _wire(monkeypatch)
    with pytest.raises(SystemExit):
        RS.run(_args(date="July 16"))
