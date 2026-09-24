"""Scope-drift detection - tickets added to the (CURRENT) fix-version AFTER
functional testing started (frozen scope on the `test` action vs current tickets)."""
from types import SimpleNamespace

from conductor import manifest as M
from conductor.commands import recap as R


def _mf(tickets, actions=None):
    return M.Manifest(
        release_version_id="1", release_label="2026-08-06", generated_at="now",
        tickets=[M.TicketEntry(key=k, issue_id=k[-1], summary=f"t {k}", status="Open",
                               issue_type="Task") for k in tickets],
        actions=actions or [],
    )


def _test_action(scope):
    return {"at": "2026-08-06T02:33:00", "stage": "test",
            "summary": "triggered platform tests", "url": "https://ci/q/1/", "scope": scope}


# ── the data methods ─────────────────────────────────────────────────────────

def test_no_baseline_when_no_test_action():
    mf = _mf(["REL-1", "REL-2"])
    assert mf.test_scope_keys() is None
    assert mf.tickets_added_after_test() == []


def test_no_baseline_when_test_action_has_no_scope():
    # A run predating this feature (or a bare action) → no baseline, no drift.
    mf = _mf(["REL-1"], actions=[{"at": "x", "stage": "test", "summary": "old run"}])
    assert mf.test_scope_keys() is None
    assert mf.tickets_added_after_test() == []


def test_scope_frozen_from_earliest_test_action():
    mf = _mf(["REL-1", "REL-2"], actions=[
        _test_action(["REL-1"]),          # earliest - the cutoff
        _test_action(["REL-1", "REL-2"]),  # a later re-trigger; must NOT widen baseline
    ])
    assert mf.test_scope_keys() == ["REL-1"]


def test_added_after_test_lists_new_tickets():
    mf = _mf(["REL-1", "REL-2", "REL-3"], actions=[_test_action(["REL-1", "REL-2"])])
    added = mf.tickets_added_after_test()
    assert [t.key for t in added] == ["REL-3"]


def test_explicit_rebaseline_supersedes_and_retest_does_not():
    # First trigger froze {REL-1}. A later re-trigger recording {REL-1,REL-2} must
    # NOT move the baseline - only an explicit scope_baseline does.
    base = [_test_action(["REL-1"]), _test_action(["REL-1", "REL-2"])]
    mf = _mf(["REL-1", "REL-2"], actions=base)
    assert mf.test_scope_keys() == ["REL-1"]                 # re-test alone: unmoved
    assert [t.key for t in mf.tickets_added_after_test()] == ["REL-2"]
    # Now an explicit integrate re-baselines to the current scope.
    base.append({"at": "2026-08-06T05:00:00", "stage": "scope_baseline",
                 "summary": "re-baselined", "scope": ["REL-1", "REL-2"]})
    mf2 = _mf(["REL-1", "REL-2"], actions=base)
    assert mf2.test_scope_keys() == ["REL-1", "REL-2"]
    assert mf2.tickets_added_after_test() == []             # drift cleared


def test_no_drift_when_scope_unchanged():
    mf = _mf(["REL-1", "REL-2"], actions=[_test_action(["REL-1", "REL-2"])])
    assert mf.tickets_added_after_test() == []


# ── record_action(extra=) roundtrips the scope through disk ──────────────────

def test_record_action_extra_persists_scope(tmp_path):
    mf = _mf(["REL-1", "REL-9"])
    p = tmp_path / "release-2026-08-06.json"
    M.write_manifest(mf, p)
    M.record_action(p, "test", "triggered", url="https://ci/q/1/",
                    extra={"scope": ["REL-1"]})
    reloaded = M.read_manifest(p)
    assert reloaded.test_scope_keys() == ["REL-1"]
    assert [t.key for t in reloaded.tickets_added_after_test()] == ["REL-9"]


# ── the trigger captures scope for both paths ────────────────────────────────



# ── recap surfaces the drift as a follow-up ──────────────────────────────────

def test_recap_lists_added_after_test_as_followup():
    mf = _mf(["REL-1", "REL-2", "REL-7"], actions=[_test_action(["REL-1", "REL-2"])])
    out = "\n".join(R.build_recap(mf))
    assert "added AFTER testing started" in out
    assert "REL-7" in out
