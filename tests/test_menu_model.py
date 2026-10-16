"""The menu's gating: what is available, hard-disabled, or advisory for a given train state."""
from pathlib import Path

from conductor import manifest as M, menu
from conductor.menu import PipelineState


def _state(repos, **kw):
    mf = M.Manifest(release_version_id="1", release_label="2030-04-02", generated_at="x",
                    repos=repos)
    taggable = [r for r in repos if r.classification in ("platform", "service")]
    return PipelineState(manifest_path=Path("/x/release.json"), manifest=mf,
                         release_label=mf.release_label, scope_count=len(taggable),
                         tagged_count=sum(1 for r in taggable if r.release_tag),
                         untagged_count=sum(1 for r in taggable if not r.release_tag), **kw)


def test_empty_cycle_disables_the_pipeline_and_points_at_refresh():
    actions = menu._build_actions(_state([]), {})
    bump_stg = actions[3]
    assert bump_stg.disabled_reason == "nothing in scope yet - refresh (1) after PRs merge"
    assert menu._resolve_choice("4", actions)[0] == "error"


def test_empty_cycle_relabels_the_finished_step_to_a_close_out_note():
    row = menu._build_actions(_state([]), {})[12]
    assert row.label == "Post cycle-empty note"
    assert len(row.label) <= 32  # fits the label column so every hint lines up


def test_both_test_steps_are_regular_on_a_services_only_train():
    actions = menu._build_actions(
        _state([M.RepoEntry(repo="service-backup", classification="service")]), {})
    for i, label in ((5, "Trigger platform tests"), (6, "Trigger services tests")):
        row = actions[i]
        assert row.label == label and row.disabled_reason == "" and not row.soft_reason
        kind, picked = menu._resolve_choice(str(i + 1), actions)
        assert kind == "run" and picked is row


def test_prd_bump_waits_for_release_tags():
    untagged = [M.RepoEntry(repo="api", classification="platform")]
    assert menu._build_actions(_state(untagged), {})[7].disabled_reason == "release tags not cut yet"
    tagged = [M.RepoEntry(repo="api", classification="platform", release_tag="5.230.0")]
    assert menu._build_actions(_state(tagged), {})[7].disabled_reason == ""


def test_every_label_fits_the_column():
    for repos in ([], [M.RepoEntry(repo="api", classification="platform", release_tag="1.0.0")]):
        for a in menu._build_actions(_state(repos), {}):
            assert len(a.label) <= 32, a.label
