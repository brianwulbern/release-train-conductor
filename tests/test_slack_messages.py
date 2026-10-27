"""Wording/emoji of the threaded release messages + the idempotent close-the-door
helper."""
from pathlib import Path

from conductor import config, manifest as M, slack_messages as sm, slack_thread


def test_closing_the_door_wording():
    m = sm.closing_the_door()
    # the 🤝 carries the configured skin tone (light 🏻 by default)
    assert m == ("closing the door 🚪 on this release train 🚂. "
                 f"Please add any ongoing work to the NEXT cycle. Thanks 🤝{config.SLACK_SKIN_TONE}")
    assert m.endswith("🤝🏻")  # light tone applied, not default yellow


def test_bump_messages_lead_with_review_emoji():
    assert sm.stg_bump("URL").startswith(":review: pull to bump `stg.yaml` on `develop`")
    assert sm.prd_bump("URL").startswith(":review: pull to bump `prd.yaml` on `develop`")
    assert sm.stg_bump("URL").endswith("\nURL")


def test_pre_bump_lists_prs():
    m = sm.pre_bump(["u1", "u2"])
    assert m.splitlines() == [":review: service pre-bump PR(s):", "u1", "u2"]


def test_release_tags_cut_lists_tag_urls():
    m = sm.release_tags_cut([("api", "api_url"), ("web-console", "lc_url")])
    assert m.splitlines() == ["pre-release tags cut:", "api_url", "lc_url"]


def test_testing_links_build_number_when_url_known():
    assert sm.platform_testing(445, "http://ci/445") == "Platform testing (<http://ci/445|445>) :loading:"
    assert sm.services_testing(804, "http://ci/804") == "Services testing (<http://ci/804|804>) :loading:"
    # no url → bare number
    assert sm.platform_testing(445) == "Platform testing (445) :loading:"


def test_promote_renders_real_mentions():
    m = sm.promote("URL", ["UREVIEWER01", "UREVIEWER02"])
    assert m == (":review: pull to bump `prd.yaml` on `production`\nURL\n"
                 "<@UREVIEWER01> <@UREVIEWER02>")
    # no reviewers → no dangling mention line
    assert sm.promote("URL", []) == ":review: pull to bump `prd.yaml` on `production`\nURL"


def test_argocd_and_finished():
    assert sm.argocd_sync("2026-08-20") == "📢 Syncing ArgoCD for Release Train 2026-08-20"
    assert sm.finished_syncing("2026-08-20").startswith("🏁 Finished syncing.")
    assert "Thank you everyone involved!" in sm.finished_syncing("2026-08-20")


def test_empty_cycle_names_this_and_next_train():
    msg = sm.empty_cycle("2026-09-24", "2026-09-29")
    assert "2026-09-24" in msg and "nothing to test" in msg
    assert "next train (2026-09-29) begins momentarily" in msg


def test_empty_cycle_without_next_label_reads_cleanly():
    msg = sm.empty_cycle("2026-09-24")
    assert "The next train begins momentarily." in msg
    assert "()" not in msg  # no empty parenthetical when the date is unknown


# ─── door_closed + close_the_door (idempotent, best-effort) ──────────────────────

def _root_mf(tmp_path) -> Path:
    p = tmp_path / "release-2026-08-20.json"
    mf = M.Manifest(
        release_version_id="1", release_label="2026-08-20", generated_at="now",
        actions=[{"stage": "announce", "slack_channel": "#c", "slack_thread_ts": "1.0"}],
    )
    M.write_manifest(mf, p)
    return p


def test_door_closed_flag():
    mf = M.Manifest(release_version_id="1", release_label="x", generated_at="n", actions=[])
    assert not mf.door_closed()
    mf.actions.append({"stage": "close_door", "summary": "frozen"})
    assert mf.door_closed()


def test_close_the_door_posts_and_records_once(tmp_path, monkeypatch):
    p = _root_mf(tmp_path)
    monkeypatch.setattr(slack_thread.slack_client, "method", lambda: "token")
    posts = []
    monkeypatch.setattr(slack_thread.slack_client, "post_message",
                        lambda *a, **k: posts.append(k) or (True, None, "9.9"))
    assert slack_thread.close_the_door(p) is True
    assert len(posts) == 1 and posts[0]["broadcast"] is True  # broadcast to channel
    assert M.read_manifest(p).door_closed()  # recorded

    # Second call is a no-op - already closed, no re-post.
    assert slack_thread.close_the_door(p) is True
    assert len(posts) == 1


def test_close_the_door_no_token_does_not_record(tmp_path, monkeypatch):
    """Token-less operator: silent no-op, and it must NOT record close_door (so it
    isn't falsely marked done), leaving a token holder free to post it later."""
    p = _root_mf(tmp_path)
    monkeypatch.setattr(slack_thread.slack_client, "method", lambda: None)
    assert slack_thread.close_the_door(p, quiet=True) is False
    assert not M.read_manifest(p).door_closed()


def test_a_landed_thread_post_is_noted(monkeypatch, tmp_path, capsys):
    """Every Slack post that lands says where it went and what it said (operator
    ask 2026-10-01: option 12 broadcast 'Syncing ArgoCD' without a word)."""
    from conductor import manifest as M, slack_client, slack_thread
    path = tmp_path / "release-2026-10-01.json"
    M.write_manifest(M.Manifest(release_version_id="1", release_label="2026-10-01",
                                generated_at="x"), path)
    monkeypatch.setattr(slack_client, "method", lambda: "token")
    monkeypatch.setattr(slack_thread.slack_roots, "get_root", lambda label, vid=None: ("C1", "111.1"))
    monkeypatch.setattr(slack_client, "post_message", lambda *a, **k: (True, None, "222.2"))
    assert slack_thread.post_update(path, "📢 Syncing ArgoCD for Release Train 2026-10-01\nmore",
                                    broadcast=True, quiet=True) == "222.2"
    assert capsys.readouterr().out == ("  ↪ Slack: posted to the release thread + channel: "
                                       "📢 Syncing ArgoCD for Release Train 2026-10-01\n")
    slack_thread.post_update(path, "Platform testing (463)")
    assert "posted to the release thread: Platform testing (463)" in capsys.readouterr().out


def test_triage_result_links_the_build_and_each_ticket():
    from conductor import config
    from conductor.slack_messages import triage_result
    text = triage_result("platform", 473, "https://ci/473/", "315 passed, 3 failed: all known (REL-53425, REL-53426)")
    assert text == (f"Platform tests (<https://ci/473/|473>): 315 passed, 3 failed: all known "
                    f"(<{config.JIRA_HOST}/browse/REL-53425|REL-53425>, "
                    f"<{config.JIRA_HOST}/browse/REL-53426|REL-53426>)")
