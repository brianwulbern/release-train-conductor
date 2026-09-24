"""Foundation for threaded Slack status updates: the manifest thread-root reader
and the strictly-best-effort post_update helper (must never fail a command for an
operator who lacks the token)."""
import json
from pathlib import Path

from conductor import manifest as M, slack_client, slack_thread


# ─── Manifest.slack_thread() reader ─────────────────────────────────────────────

def _mf(actions):
    return M.Manifest(
        release_version_id="1", release_label="2026-08-20",
        generated_at="now", actions=actions,
    )


def test_slack_thread_none_when_no_announce():
    assert _mf([]).slack_thread() is None
    assert _mf([{"stage": "bump_stg", "summary": "x"}]).slack_thread() is None


def test_slack_thread_none_when_announce_has_no_ts():
    # A webhook/copy-paste announce records the action but no ts → nothing to thread.
    assert _mf([{"stage": "announce", "summary": "posted"}]).slack_thread() is None


def test_slack_thread_reads_channel_and_ts():
    mf = _mf([{"stage": "announce", "summary": "posted",
               "slack_channel": "#releases", "slack_thread_ts": "111.22"}])
    assert mf.slack_thread() == ("#releases", "111.22")


def test_slack_thread_prefers_most_recent_announce():
    mf = _mf([
        {"stage": "announce", "slack_channel": "#a", "slack_thread_ts": "1.0"},
        {"stage": "bump_stg", "summary": "x"},
        {"stage": "announce", "slack_channel": "#b", "slack_thread_ts": "2.0"},
    ])
    assert mf.slack_thread() == ("#b", "2.0")


# ─── post_update: best-effort / silent-skip invariant ───────────────────────────

def _root_manifest(tmp_path) -> Path:
    p = tmp_path / "release-2026-08-20.json"
    mf = _mf([{"stage": "announce", "summary": "posted",
               "slack_channel": "#releases", "slack_thread_ts": "111.22"}])
    M.write_manifest(mf, p)
    return p


def test_post_update_noop_without_token(tmp_path, monkeypatch):
    """No user token (every other operator) → silent no-op, and crucially it never
    even reads a would-be root or calls Slack."""
    monkeypatch.setattr(slack_thread.slack_client, "method", lambda: None)
    called = []
    monkeypatch.setattr(slack_thread.slack_client, "post_message",
                        lambda *a, **k: called.append(1) or (True, None, "x"))
    assert slack_thread.post_update(_root_manifest(tmp_path), "hi") is None
    assert called == []  # never attempted to post


def test_post_update_noop_without_recorded_root(tmp_path, monkeypatch):
    """Token present but no announce-root recorded → skip (nothing to thread under)."""
    monkeypatch.setattr(slack_thread.slack_client, "method", lambda: "token")
    called = []
    monkeypatch.setattr(slack_thread.slack_client, "post_message",
                        lambda *a, **k: called.append(1) or (True, None, "x"))
    p = tmp_path / "release-2026-08-20.json"
    M.write_manifest(_mf([]), p)  # no announce action
    assert slack_thread.post_update(p, "hi") is None
    assert called == []


def test_post_update_threads_under_root(tmp_path, monkeypatch):
    monkeypatch.setattr(slack_thread.slack_client, "method", lambda: "token")
    sent = {}
    def fake_post(text, channel=None, thread_ts=None, broadcast=False):
        sent.update(text=text, channel=channel, thread_ts=thread_ts, broadcast=broadcast)
        return True, None, "999.9"
    monkeypatch.setattr(slack_thread.slack_client, "post_message", fake_post)
    ts = slack_thread.post_update(_root_manifest(tmp_path), "tags cut", broadcast=True)
    assert ts == "999.9"
    assert sent == {"text": "tags cut", "channel": "#releases",
                    "thread_ts": "111.22", "broadcast": True}


def test_post_update_swallows_errors(tmp_path, monkeypatch):
    """A Slack failure (even a SystemExit, as a client raises on 401/403) is
    swallowed - post_update returns None, never propagates."""
    monkeypatch.setattr(slack_thread.slack_client, "method", lambda: "token")
    def boom(*a, **k):
        raise SystemExit("401")
    monkeypatch.setattr(slack_thread.slack_client, "post_message", boom)
    assert slack_thread.post_update(_root_manifest(tmp_path), "hi", quiet=True) is None


def test_post_update_returns_none_on_api_error(tmp_path, monkeypatch):
    monkeypatch.setattr(slack_thread.slack_client, "method", lambda: "token")
    monkeypatch.setattr(slack_thread.slack_client, "post_message",
                        lambda *a, **k: (False, "Slack API error: not_in_channel", None))
    assert slack_thread.post_update(_root_manifest(tmp_path), "hi", quiet=True) is None


def test_post_update_none_manifest_path():
    assert slack_thread.post_update(None, "hi") is None


# ─── cycle-keyed root fallback (announce lands on the PRIOR cycle's manifest) ────

def test_slack_roots_store_roundtrip(tmp_path, monkeypatch):
    from conductor import slack_roots, config as cfg
    monkeypatch.setattr(cfg, "STATE_DIR", tmp_path)
    assert slack_roots.get_root("2026-08-25") is None
    slack_roots.set_root("2026-08-25", "#releases", "111.22")
    assert slack_roots.get_root("2026-08-25") == ("#releases", "111.22")
    # a different label is independent
    assert slack_roots.get_root("2026-08-27") is None
    slack_roots.set_root("2026-08-27", "#releases", "333.44")
    assert slack_roots.get_root("2026-08-25") == ("#releases", "111.22")


def test_post_update_falls_back_to_cycle_root_store(tmp_path, monkeypatch):
    # The manifest has NO announce action (its root landed on the prior cycle's
    # manifest), but the cycle-keyed store has it → post_update still threads.
    from conductor import slack_roots, config as cfg
    monkeypatch.setattr(cfg, "STATE_DIR", tmp_path)
    monkeypatch.setattr(slack_thread.slack_client, "method", lambda: "token")
    slack_roots.set_root("2026-08-25", "#releases", "111.22")
    sent = {}
    def fake_post(text, channel=None, thread_ts=None, broadcast=False):
        sent.update(channel=channel, thread_ts=thread_ts)
        return True, None, "999.9"
    monkeypatch.setattr(slack_thread.slack_client, "post_message", fake_post)
    p = tmp_path / "release-2026-08-25.json"
    M.write_manifest(M.Manifest(release_version_id="1", release_label="2026-08-25",
                                generated_at="now", actions=[]), p)  # no announce action
    ts = slack_thread.post_update(p, "stg bumped")
    assert ts == "999.9"
    assert sent == {"channel": "#releases", "thread_ts": "111.22"}


def test_thread_available_uses_cycle_root_store(tmp_path, monkeypatch):
    from conductor import slack_roots, config as cfg
    monkeypatch.setattr(cfg, "STATE_DIR", tmp_path)
    monkeypatch.setattr(slack_thread.slack_client, "method", lambda: "token")
    p = tmp_path / "release-2026-08-25.json"
    M.write_manifest(M.Manifest(release_version_id="1", release_label="2026-08-25",
                                generated_at="now", actions=[]), p)
    assert slack_thread.thread_available(p) is False   # nothing yet
    slack_roots.set_root("2026-08-25", "#releases", "111.22")
    assert slack_thread.thread_available(p) is True    # store supplies the root


# ─── slack_client broadcast wiring ──────────────────────────────────────────────

class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._p = payload

    def json(self):
        return self._p


def test_broadcast_sets_reply_broadcast(monkeypatch):
    sent = {}
    monkeypatch.setattr(slack_client.requests, "post",
                        lambda url, headers=None, json=None, timeout=None:
                        sent.update(json=json) or _Resp({"ok": True, "ts": "1.1"}))
    slack_client.post_message("hi", token="xoxp-a", channel="#c",
                              thread_ts="9.9", broadcast=True)
    assert sent["json"]["reply_broadcast"] is True
    assert sent["json"]["thread_ts"] == "9.9"


def test_broadcast_ignored_without_thread_ts(monkeypatch):
    """reply_broadcast only makes sense on a threaded reply - never sent on a
    top-level post."""
    sent = {}
    monkeypatch.setattr(slack_client.requests, "post",
                        lambda url, headers=None, json=None, timeout=None:
                        sent.update(json=json) or _Resp({"ok": True, "ts": "1.1"}))
    slack_client.post_message("hi", token="xoxp-a", channel="#c", broadcast=True)
    assert "reply_broadcast" not in sent["json"]


# ─── thread_available (pre-poll guard) ──────────────────────────────────────────

def test_thread_available_true_with_token_and_root(tmp_path, monkeypatch):
    monkeypatch.setattr(slack_thread.slack_client, "method", lambda: "token")
    assert slack_thread.thread_available(_root_manifest(tmp_path)) is True


def test_thread_available_false_without_token(tmp_path, monkeypatch):
    monkeypatch.setattr(slack_thread.slack_client, "method", lambda: None)
    assert slack_thread.thread_available(_root_manifest(tmp_path)) is False


def test_thread_available_false_without_root(tmp_path, monkeypatch):
    monkeypatch.setattr(slack_thread.slack_client, "method", lambda: "token")
    p = tmp_path / "release-2026-08-20.json"
    M.write_manifest(_mf([]), p)
    assert slack_thread.thread_available(p) is False


def test_thread_available_none_path():
    assert slack_thread.thread_available(None) is False


# ─── post_once (idempotent milestone broadcast) ─────────────────────────────────

def test_post_once_posts_and_records_then_noops(tmp_path, monkeypatch):
    p = _root_manifest(tmp_path)
    monkeypatch.setattr(slack_thread.slack_client, "method", lambda: "token")
    posts = []
    monkeypatch.setattr(slack_thread.slack_client, "post_message",
                        lambda *a, **k: posts.append(k) or (True, None, "5.5"))
    assert slack_thread.post_once(p, "argocd_sync", "📢 Syncing", broadcast=True) is True
    assert len(posts) == 1 and posts[0]["broadcast"] is True
    assert M.read_manifest(p).actions_for("argocd_sync")  # recorded

    # Second call: already recorded → returns True, no re-post.
    assert slack_thread.post_once(p, "argocd_sync", "📢 Syncing", broadcast=True) is True
    assert len(posts) == 1


def test_post_once_no_token_does_not_record(tmp_path, monkeypatch):
    p = _root_manifest(tmp_path)
    monkeypatch.setattr(slack_thread.slack_client, "method", lambda: None)
    assert slack_thread.post_once(p, "finished_sync", "🏁 done", quiet=True) is False
    assert not M.read_manifest(p).actions_for("finished_sync")


def test_post_once_records_explicit_summary(tmp_path, monkeypatch):
    p = _root_manifest(tmp_path)
    monkeypatch.setattr(slack_thread.slack_client, "method", lambda: "token")
    monkeypatch.setattr(slack_thread.slack_client, "post_message",
                        lambda *a, **k: (True, None, "6.6"))
    slack_thread.post_once(p, "finished_sync", "🏁 line1\nline2", summary="train done")
    rec = M.read_manifest(p).actions_for("finished_sync")
    assert rec and rec[0]["summary"] == "train done"
