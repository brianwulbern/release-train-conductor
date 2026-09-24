"""`conductor recap` - the one-page end-of-cycle summary built purely from the manifest
(shipped artifacts, tickets by type, the activity timeline, follow-ups)."""
from conductor import manifest as M
from conductor.commands import recap as R


def _mf(actions=None):
    return M.Manifest(
        release_version_id="1", release_label="2026-08-04", generated_at="2026-08-03T12:00:00",
        tickets=[
            M.TicketEntry(key="REL-1", issue_id="1", summary="Fix a crash",
                          status="Closed", issue_type="Bug"),
            M.TicketEntry(key="REL-2", issue_id="2", summary="Bump a dep",
                          status="Ready for Release", issue_type="Task"),
            M.TicketEntry(key="REL-3", issue_id="3", summary="Add a feature",
                          status="Closed", issue_type="Story"),
        ],
        repos=[
            M.RepoEntry(repo="api", classification="platform", release_tag="5.221.1"),
            M.RepoEntry(repo="service-backup", classification="service",
                        release_tag="5.37.0", needs_pre_bump=True),
            M.RepoEntry(repo="charts", classification="meta"),  # in scope, not tagged
        ],
        actions=actions or [],
    )


def _joined(lines):
    return "\n".join(lines)


def test_shipped_lists_tagged_platform_and_service():
    out = _joined(R.build_recap(_mf()))
    assert "Shipped (2 repos)" in out
    assert "api" in out and "5.221.1" in out
    assert "service-backup" in out and "5.37.0" in out
    # meta repo isn't "shipped" but is surfaced as in-scope-not-tagged
    assert "not tagged: charts" in out


def test_ticket_type_counts_in_display_order():
    out = _joined(R.build_recap(_mf()))
    # Bug before Task before Story (the _TYPE_ORDER), all counted
    assert "Tickets (3) - 1 Bug, 1 Task, 1 Story" in out


def test_timeline_renders_steps_and_urls():
    actions = [
        {"at": "2026-08-04T00:27:00", "stage": "release", "summary": "cut api 5.221.1",
         "url": "https://github.com/example-org/api/releases/tag/5.221.1", "repo": "api"},
        {"at": "2026-08-04T00:25:00", "stage": "bump_stg", "summary": "stg bump PR",
         "url": None, "repo": None},
    ]
    out = _joined(R.build_recap(_mf(actions)))
    assert "Timeline (2 steps)" in out
    assert "release tag" in out  # friendly label for the "release" stage
    assert "stg bump" in out
    assert "releases/tag/5.221.1" in out


def test_followups_service_article_and_open_bug():
    # REL-1 is a Closed bug; add an open one to trigger the follow-up.
    mf = _mf()
    mf.tickets.append(M.TicketEntry(key="REL-9", issue_id="9", summary="Still open",
                                    status="Ready for Release", issue_type="Bug"))
    out = _joined(R.build_recap(mf))
    assert "service article required" in out and "service-backup 5.37.0" in out
    assert "Bug(s) not yet Closed" in out and "REL-9" in out


def test_finalized_status_flips_on_finalize_action():
    assert "in flight" in _joined(R.build_recap(_mf()))
    fin = [{"at": "2026-08-04T09:00:00", "stage": "finalize", "summary": "done",
            "url": None, "repo": None}]
    assert "finalized ✓" in _joined(R.build_recap(_mf(fin)))


def test_markdown_mode_uses_headers_and_bullets():
    out = _joined(R.build_recap(_mf(), markdown=True))
    assert out.startswith("# Release recap")
    assert "## Shipped" in out
    assert "- **platform** api - `5.221.1`" in out


def test_empty_manifest_does_not_crash():
    mf = M.Manifest(release_version_id="1", release_label="2026-08-04",
                    generated_at="2026-08-03T12:00:00")
    out = _joined(R.build_recap(mf))
    assert "no platform/service repos tagged yet" in out
    assert "no tickets recorded" in out
    assert "no steps recorded yet" in out
