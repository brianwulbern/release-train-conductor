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
    pipeline step. `quiet` suppresses even the one-line skip notice."""
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
        root = mf.slack_thread() or slack_roots.get_root(mf.release_label)
        if not root:
            return None
        channel, thread_ts = root
        ok, err, ts = slack_client.post_message(
            text, channel=channel, thread_ts=thread_ts, broadcast=broadcast)
        if not ok:
            if not quiet:
                print(f"  · Slack thread update skipped: {err}")
            return None
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
        return (mf.slack_thread() or slack_roots.get_root(mf.release_label)) is not None
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
