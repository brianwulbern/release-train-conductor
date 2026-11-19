"""A repository's bump is a patch only for bug fixes, and a security fix is always a minor."""
from conductor import manifest as M


def _mf(*tickets):
    return M.Manifest(release_version_id="1", release_label="2030-04-02", generated_at="x",
                      tickets=[M.TicketEntry(key=k, issue_id=k, summary=k, status="Closed", issue_type=t, security=s)
                               for k, t, s in tickets])


def _repo(*keys):
    return M.RepoEntry(repo="api", classification="platform", contributing_tickets=list(keys))


def test_bug_only_is_a_patch():
    assert _mf(("REL-1", "Bug", False)).bump_reason(_repo("REL-1")) == ("patch", "bug-only")


def test_a_security_bug_is_a_minor_and_says_why():
    mf = _mf(("REL-1", "Bug", False), ("REL-2", "Bug", True))
    assert mf.bump_reason(_repo("REL-1", "REL-2")) == ("minor", "security fix REL-2")
    assert mf.judge_bump_kind(_repo("REL-1", "REL-2")) == "minor"


def test_any_non_bug_is_a_minor():
    assert _mf(("REL-1", "Task", False)).bump_reason(_repo("REL-1"))[0] == "minor"
