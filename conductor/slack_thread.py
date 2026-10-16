"""Best-effort threaded status updates under the pinned release announcement.

The announcement (`conductor announce`) is the thread ROOT; each later pipeline step
posts a threaded reply beneath it via `post_update()`.

STRICTLY best-effort and non-fatal - this is the invariant that keeps the Slack
layer from ever affecting operators who don't have the token/app:

  - No Slack **user token** set  → silent no-op (the common case for every other
    operator; a webhook can't thread, so it's skipped too).
  - No recorded **thread root**  → silent no-op (announcement wasn't posted via a
    token this cycle, so there's nothing to reply under).
  - Any Slack **error** (network, 401/403 → the clients `sys.exit`, missing_scope,
    thread_not_found) → swallowed.

In every one of those cases the calling command's action and return code are
already decided and completely unaffected. Mirrors `jira_progress`'s contract
(never fails the command; absorbs even the `SystemExit` a client raises on
401/403 - hence the `except (Exception, SystemExit)`).
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Optional

from . import manifest as M, slack_client, slack_messages, slack_roots


def post_update(
    manifest_path: Optional[Path],
    text: str,
    broadcast: bool = False,
    quiet: bool = False,
) -> Optional[str]:
    """Post `text` as a threaded reply under this cycle's announcement root.

    `broadcast=True` also surfaces it to the channel (the milestone posts:
    closing-the-door, ArgoCD sync, finished). Returns the reply's `ts` on success,
    else None. NEVER raises - a Slack hiccup must not fail an otherwise-successful
    pipeline step. `quiet` suppresses even the one-line skip notice; a post that
    lands is always noted, so the operator knows what went to Slack and where."""
    try:
        if manifest_path is None:
            return None
        # Threading requires a user token; a webhook can't reply into a thread.
        # No token → this operator simply isn't the one driving Slack: skip.
        if slack_client.method() != "token":
            return None
        mf = M.read_manifest(manifest_path)
        # Prefer the root recorded on THIS manifest; fall back to the cycle-keyed
        # store (by release_label), since a train's announcement is often recorded
        # on the PRIOR cycle's manifest (announce runs at the end of that cycle).
        root = mf.slack_thread() or slack_roots.get_root(mf.release_label, mf.release_version_id)
        if not root:
            return None
        channel, thread_ts = root
        ok, err, ts = slack_client.post_message(
            text, channel=channel, thread_ts=thread_ts, broadcast=broadcast)
        if not ok:
            if not quiet:
                print(f"  · Slack thread update skipped: {err}")
            return None
        where = "the release thread + channel" if broadcast else "the release thread"
        first = (text.strip().splitlines() or [""])[0]
        print(f"  ↪ Slack: posted to {where}: {first[:90]}{'…' if len(first) > 90 else ''}")
        return ts
    except (Exception, SystemExit) as e:
        if not quiet:
            print(f"  · Slack thread update skipped: {e}")
        return None


def thread_available(manifest_path: Optional[Path]) -> bool:
    """True only when a threaded update would actually post: a Slack **user token**
    is set AND this cycle recorded an announcement thread root. Lets a caller skip
    expensive pre-work (e.g. polling Jenkins for a build number just to post it)
    when `post_update` would silently no-op anyway. Best-effort - never raises."""
    try:
        if manifest_path is None or slack_client.method() != "token":
            return False
        mf = M.read_manifest(manifest_path)
        return (mf.slack_thread() or slack_roots.get_root(mf.release_label, mf.release_version_id)) is not None
    except (Exception, SystemExit):
        return False


def post_once(
    manifest_path: Optional[Path],
    stage: str,
    text: str,
    summary: Optional[str] = None,
    broadcast: bool = False,
    quiet: bool = False,
) -> bool:
    """Post `text` as a threaded reply AT MOST once per cycle, guarded by a
    recorded `stage` action. Returns True if it's (now or already) posted, False
    if it couldn't (e.g. no token). Used for the milestone broadcasts (ArgoCD
    sync, finished syncing) so re-opening the menu step doesn't double-post.

    Only records the action when the post actually lands, so a token-less
    operator's attempts stay silent no-ops rather than falsely marking it done.
    Best-effort - never raises."""
    try:
        if manifest_path is None:
            return False
        mf = M.read_manifest(manifest_path)
        if mf.actions_for(stage):
            return True  # already posted this cycle - don't double-post
        ts = post_update(manifest_path, text, broadcast=broadcast, quiet=quiet)
        if ts:
            M.record_action(manifest_path, stage, summary or text.splitlines()[0][:80])
            return True
        return False
    except (Exception, SystemExit):
        return False


def close_the_door(manifest_path: Optional[Path], quiet: bool = False) -> bool:
    """Post the scope-freeze ("closing the door") broadcast - at most once per
    cycle. Idempotent via a recorded `close_door` action: if the door is already
    closed, this returns True without re-posting. Called explicitly by the `d`
    utility (quiet=False) and auto by the first bump step (quiet=True).

    Returns True if the door is (now or already) closed & recorded, False if it
    couldn't post (e.g. no token) - best-effort, never raises. Only records the
    action when the post actually lands, so a token-less operator's auto-attempts
    stay silent no-ops rather than falsely marking it done."""
    try:
        if manifest_path is None:
            return False
        mf = M.read_manifest(manifest_path)
        if mf.door_closed():
            return True  # already announced this cycle - don't double-post
        # A door post Slack is about to send can't be reliably cancelled, so let it
        # go out rather than posting a second one now.
        secs = _secs_until_door(mf)
        if mf.pending_door_post() and secs is not None and secs < CANCEL_SAFE_SECS:
            if not quiet:
                print(f"  · Slack posts the door close in {max(secs, 0)}s (already queued); "
                      f"letting that one go out instead of posting a second")
            return True
        # Closing early (by `d` or the first bump) supersedes a door post Slack has
        # queued for the announced cutoff, so cancel it first: one door post only.
        note = cancel_door_post(manifest_path)
        if note and not quiet:
            print(f"  · {note}")
        ts = post_update(
            manifest_path, slack_messages.closing_the_door(), broadcast=True, quiet=quiet)
        if ts:
            M.record_action(
                manifest_path, "close_door",
                "closing the door on the release train (scope frozen)")
            return True
        return False
    except (Exception, SystemExit):
        return False


# Slack reports a cancel of a post due within a few minutes as successful but
# sends it anyway (live-tested 2026-09-28: a cancel ~3 min out still posted; one
# 10 min out held). Inside this window a queued door post can't be relied on to
# cancel, so an early close lets it post instead of posting a second one.
CANCEL_SAFE_SECS = 300


def _secs_until_door(mf) -> Optional[int]:
    cutoff = mf.door_cutoff()
    if cutoff is None:
        return None
    return int((cutoff - dt.datetime.now(dt.timezone.utc)).total_seconds())


def schedule_door_post(manifest_path: Optional[Path], post_at: int):
    """Have Slack post the scope-freeze ("closing the door") under this cycle's
    announcement at `post_at` (epoch seconds), also broadcast to the channel, the
    same post `d` makes. Slack holds it and posts it on time, so no process has to
    be running at the cutoff. Returns (channel, scheduled_message_id) or an error
    string. Best-effort - never raises."""
    try:
        if manifest_path is None or slack_client.method() != "token":
            return "no Slack user token"
        mf = M.read_manifest(manifest_path)
        root = mf.slack_thread() or slack_roots.get_root(mf.release_label, mf.release_version_id)
        if not root:
            return "no release thread to reply under"
        channel, thread_ts = root
        ok, err, sid = slack_client.schedule_message(
            slack_messages.closing_the_door(), post_at, channel,
            thread_ts=thread_ts, broadcast=True)
        return (channel, sid) if ok and sid else (err or "Slack returned no id")
    except (Exception, SystemExit) as e:
        return str(e)


def cancel_door_post(manifest_path: Optional[Path]) -> Optional[str]:
    """Cancel the door post Slack still has queued for this cycle, if any.
    Returns a one-line note for the operator, or None when nothing was pending.
    Best-effort - never raises."""
    try:
        if manifest_path is None:
            return None
        pending = M.read_manifest(manifest_path).pending_door_post()
        if not pending:
            return None
        channel, sid = pending
        secs = _secs_until_door(M.read_manifest(manifest_path))
        ok, err = slack_client.delete_scheduled_message(channel, sid)
        if ok and secs is not None and secs < CANCEL_SAFE_SECS:
            return (f"cancel requested, but the old door post was due in {max(secs, 0)}s; "
                    f"Slack may still send it, so delete it in Slack if it appears")
        if ok:
            return "cancelled the scheduled door post"
        return (f"couldn't cancel the scheduled door post ({err}); it may still post "
                f"at the old time - delete it in Slack if so")
    except (Exception, SystemExit) as e:
        return f"couldn't cancel the scheduled door post ({e})"
