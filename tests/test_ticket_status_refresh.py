"""`manifest.update_ticket_statuses` - reconcile the manifest's cached ticket
statuses with live Jira (used by finalize so recap/menu don't show a Bug that was
just closed as still-open)."""
from conductor import manifest as M


def _mf():
    return M.Manifest(
        release_version_id="1", release_label="2026-08-06", generated_at="now",
        tickets=[
            M.TicketEntry(key="REL-52777", issue_id="1", summary="closed by finalize",
                          status="Ready for Release", issue_type="Bug"),
            M.TicketEntry(key="REL-51429", issue_id="2", summary="genuinely open",
                          status="Ready for Release", issue_type="Bug"),
            M.TicketEntry(key="REL-52727", issue_id="3", summary="release ticket",
                          status="Selected for Development", issue_type="Task"),
        ],
    )


def test_updates_matching_keys_and_persists(tmp_path):
    p = tmp_path / "release-2026-08-06.json"
    M.write_manifest(_mf(), p)
    # REL-52777 + release ticket closed; REL-51429 stayed open.
    M.update_ticket_statuses(p, {
        "REL-52777": "Closed", "REL-52727": "Closed", "REL-51429": "Ready for Release",
    })
    reloaded = M.read_manifest(p)
    by_key = {t.key: t.status for t in reloaded.tickets}
    assert by_key["REL-52777"] == "Closed"
    assert by_key["REL-52727"] == "Closed"
    assert by_key["REL-51429"] == "Ready for Release"


def test_recap_followup_reflects_refreshed_statuses(tmp_path):
    from conductor.commands import recap as R
    mf = _mf()
    mf.actions = [{"at": "2026-08-06T02:33:00", "stage": "test", "summary": "t",
                   "scope": ["REL-52777", "REL-51429", "REL-52727"]}]
    p = tmp_path / "release-2026-08-06.json"
    M.write_manifest(mf, p)
    M.update_ticket_statuses(p, {"REL-52777": "Closed", "REL-52727": "Closed"})
    out = "\n".join(R.build_recap(M.read_manifest(p)))
    # Only the genuinely-open bug remains in the "not yet Closed" follow-up.
    assert "1 Bug(s) not yet Closed: REL-51429" in out
    assert "REL-52777" not in out.split("Follow-ups")[1]


def test_unknown_keys_ignored_and_no_spurious_change(tmp_path):
    p = tmp_path / "release-2026-08-06.json"
    M.write_manifest(_mf(), p)
    M.update_ticket_statuses(p, {"REL-99999": "Closed"})  # not in manifest
    by_key = {t.key: t.status for t in M.read_manifest(p).tickets}
    assert by_key["REL-52777"] == "Ready for Release"  # untouched
