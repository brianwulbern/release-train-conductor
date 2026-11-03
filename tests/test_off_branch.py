import pytest

from conductor import manifest as M
from conductor.commands import off_branch as B

API = "https://github.com/example-org/api/pull/"


def _ticket(*nums, unmerged=()):
    return M.TicketEntry(key="REL-54172", issue_id="1", summary="s", status="x",
                         pr_urls=[API + str(n) for n in nums], unmerged_prs=list(unmerged))


def _gh(monkeypatch, bases, compare="diverged", branch_prs=()):
    merged = {9489: "2026-10-29T10:00:00Z", 9490: "2026-10-29T12:00:00Z", 9400: "2026-10-28T00:00:00Z"}
    monkeypatch.setattr(B.github_client, "pr_payload", lambda repo, n, token: {
        "number": n, "merged_at": merged[n], "base": {"ref": bases[n]}})
    monkeypatch.setattr(B.github_client, "get_default_branch", lambda repo, token: "master")
    monkeypatch.setattr(B.github_client, "compare_status", lambda repo, a, b, token: compare)
    monkeypatch.setattr(B.github_client, "branch_prs", lambda repo, head, base, token: list(branch_prs))


def test_prs_merged_into_a_feature_branch_are_not_on_master(monkeypatch):
    _gh(monkeypatch, {9489: "REL-54172", 9490: "REL-54172", 9400: "master"})
    t = _ticket(9400, 9489, 9490)
    assert B._off_branch_prs([t], "t") == 1
    assert t.pr_urls == [API + "9400"]
    [e] = t.unmerged_prs
    assert (e["state"], e["base"], e["prs"], e["url"]) == ("BRANCH", "REL-54172", [9489, 9490], API + "9490")
    assert e["next"] == "https://github.com/example-org/api/compare/master...REL-54172?expand=1"
    assert B.off_branch_lines(e) == [
        "api #9489, #9490 merged into REL-54172, not master",
        f"last merged: {API}9490",
        "open a PR to master: https://github.com/example-org/api/compare/master...REL-54172?expand=1"]


def test_an_open_pr_to_master_is_the_next_step_and_not_listed_twice(monkeypatch):
    _gh(monkeypatch, {9489: "REL-54172", 9490: "REL-54172"},
        branch_prs=[{"state": "open", "merged_at": None, "html_url": API + "9496"}])
    t = _ticket(9489, 9490, unmerged=[{"url": API + "9496", "state": "OPEN", "repo": "api"}])
    B._off_branch_prs([t], "t")
    [e] = t.unmerged_prs
    assert e["next"] == API + "9496" and t.pr_urls == []
    assert B.off_branch_lines(e)[-1] == f"PR to master is open: {API}9496"


@pytest.mark.parametrize("compare,branch_prs", [
    ("behind", ()),
    ("identical", ()),
    ("diverged", [{"state": "closed", "merged_at": "2026-10-30T00:00:00Z"}]),
])
def test_a_branch_that_reached_master_counts_as_merged(monkeypatch, compare, branch_prs):
    _gh(monkeypatch, {9489: "REL-54172", 9490: "REL-54172"}, compare, branch_prs)
    t = _ticket(9489, 9490)
    assert B._off_branch_prs([t], "t") == 0
    assert len(t.pr_urls) == 2 and t.unmerged_prs == []
