"""A repo taken out of a train stays out across rebuilds until it is brought back."""
from conductor import manifest as M


def _mf(*actions):
    return M.Manifest(release_version_id="1", release_label="2030-04-02", generated_at="x",
                      actions=[{"stage": s, "repo": r} for s, r in actions])


def test_drop_then_undrop_replays_in_order():
    mf = _mf(("drop_repo", "service-backup"), ("drop_repo", "api"),
             ("drop_repo", "service-backup"), ("undrop_repo", "api"))
    assert mf.dropped_repos() == ["service-backup"]


def test_undrop_without_a_drop_is_ignored():
    assert _mf(("undrop_repo", "api"), ("bump_stg", "api")).dropped_repos() == []
