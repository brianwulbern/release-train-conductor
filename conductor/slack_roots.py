"""Cycle-keyed store of Slack announcement thread roots: `label → (channel, ts)`.

Why this exists (the cross-manifest gap): a train's announcement is posted at the
END of the PRIOR cycle (finalize → announce), when the *previous* cycle's manifest
is still the default - so the announce action (which carries the thread root) gets
recorded on the WRONG manifest. Days later the new cycle's pipeline runs against
its OWN manifest, which has no announce action, so `Manifest.slack_thread()` finds
no root and every threaded update silently no-ops.

`announce` also records the root here, keyed by the LABEL it announced (which it
always knows). `slack_thread.post_update` falls back to this store by the running
manifest's `release_label`, so the thread root follows the cycle regardless of
which manifest the announcement happened to land on. Best-effort - every function
swallows errors so thread bookkeeping never fails a command.
"""
from __future__ import annotations

import json
from typing import Optional, Tuple

from . import config

_FILENAME = "slack_thread_roots.json"


def _path():
    return config.STATE_DIR / _FILENAME


def _load() -> dict:
    try:
        return json.loads(_path().read_text())
    except Exception:
        return {}


def get_root(label: str) -> Optional[Tuple[Optional[str], str]]:
    """(channel, ts) of the announcement thread root for cycle `label`, or None."""
    entry = _load().get(label) if label else None
    if not entry:
        return None
    ts = entry.get("ts")
    return (entry.get("channel"), ts) if ts else None


def set_root(label: str, channel: Optional[str], ts: str) -> None:
    """Record (channel, ts) as cycle `label`'s announcement thread root."""
    if not (label and ts):
        return
    try:
        config.ensure_state_dir()
        data = _load()
        data[label] = {"channel": channel, "ts": ts}
        _path().write_text(json.dumps(data, indent=2, sort_keys=True))
    except Exception:
        pass
