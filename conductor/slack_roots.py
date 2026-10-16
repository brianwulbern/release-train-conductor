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

Two more lookups close the gaps the label-keyed file leaves:
- **Renamed versions.** A train that misses its window can have its Jira version
  renamed to the next date; the label changes but the version id doesn't. Roots are
  also stored under `v<version_id>`, and looked up that way.
- **Other machines.** The file only exists where the train was announced. With the
  channels:history scope, `get_root` scans #releases for the newest top-level
  post linking the train's Jira version (an announcement links
  `/versions/<id>`), so a hand-posted or repeated announcement is found too. For a
  token without that scope, `announce` and `conductor thread` also write the root into the
  Jira version's description (`publish_root`), and `get_root` reads it from there.
  Lookups only cache locally: they run while the menu draws, which never writes Jira.
"""
from __future__ import annotations

import json
import re
import time
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


_MARKER = "conductor Slack thread:"
_MISS_TTL = 300  # seconds; the menu header asks on every redraw
_misses: dict = {}
_MARKER_RE = re.compile(re.escape(_MARKER) + r"\s*(\S+)\s+(\d+\.\d+)")


def _entry(data: dict, key: str) -> Optional[Tuple[Optional[str], str]]:
    entry = data.get(key) if key else None
    if not entry or not entry.get("ts"):
        return None
    return entry.get("channel"), entry["ts"]


def get_root(label: str, version_id: Optional[str] = None) -> Optional[Tuple[Optional[str], str]]:
    """(channel, ts) of the announcement thread root for cycle `label`, or None.
    Tries the label, then the Jira version id, then the version's description in
    Jira (caching a Jira hit locally)."""
    return find_root(label, version_id)[0]


def find_root(label: str, version_id: Optional[str] = None):
    """`get_root` plus where it came from: ((channel, ts) or None, source), source
    being "local file (date)", "local file (version id)", "Jira version" or None."""
    data = _load()
    root = _entry(data, label)
    if root:
        return root, "local file (date)"
    root = _entry(data, f"v{version_id}") if version_id else None
    if root:
        return root, "local file (version id)"
    if not version_id:
        return None, None
    vid = str(version_id)
    if time.time() - _misses.get(vid, 0) < _MISS_TTL:
        return None, None
    for lookup, source in ((_slack_lookup, "#releases scan"), (_jira_lookup, "Jira version")):
        root = lookup(vid)
        if root:
            set_root(label, root[0], root[1], version_id=vid)
            return root, source
    _misses[vid] = time.time()
    return None, None


_SCAN_DAYS = 21


def _slack_lookup(version_id: str) -> Optional[Tuple[str, str]]:
    """The newest top-level #releases post that links Jira version
    `version_id`, as (channel, ts). None without the channels:history scope."""
    try:
        from . import slack_client
        msgs = slack_client.channel_history(config.SLACK_CHANNEL_ID,
                                            oldest=time.time() - _SCAN_DAYS * 86400)
        return pick_announcement(msgs or [], version_id)
    except Exception:
        return None


def pick_announcement(messages: list, version_id: str) -> Optional[Tuple[str, str]]:
    needle = f"/versions/{version_id}"
    hits = [m for m in messages
            if needle in (m.get("text") or "")
            and m.get("thread_ts") in (None, m.get("ts"))
            and m.get("subtype") is None]
    if not hits:
        return None
    newest = max(hits, key=lambda m: float(m["ts"]))
    return config.SLACK_CHANNEL_ID, newest["ts"]


_LINK_RE = re.compile(r"/archives/([A-Z0-9]+)/p(\d{10})(\d{6})")


def parse_link(link: str) -> Optional[Tuple[str, str]]:
    """(channel, ts) from a Slack message link
    (https://<workspace>.slack.com/archives/C0EXAMPLE01/p1900000000000100).
    A reply's link names the thread in `thread_ts=`; the root is what we want."""
    m = _LINK_RE.search(link or "")
    if not m:
        return None
    t = re.search(r"thread_ts=(\d+\.\d+)", link)
    return m.group(1), (t.group(1) if t else f"{m.group(2)}.{m.group(3)}")


def link_for(channel: Optional[str], ts: str) -> str:
    """A clickable link when `channel` is an id; otherwise "<channel> <ts>"."""
    if channel and re.fullmatch(r"[A-Z0-9]+", channel):
        return f"{config.SLACK_WORKSPACE_URL}/archives/{channel}/p{ts.replace('.', '')}"
    return f"{channel or '?'} {ts}"


def set_root(label: str, channel: Optional[str], ts: str,
             version_id: Optional[str] = None) -> None:
    """Record (channel, ts) as cycle `label`'s announcement thread root, and under
    the version id when known."""
    if not (label and ts):
        return
    try:
        config.ensure_state_dir()
        data = _load()
        data[label] = {"channel": channel, "ts": ts}
        if version_id:
            data[f"v{version_id}"] = {"channel": channel, "ts": ts}
        _path().write_text(json.dumps(data, indent=2, sort_keys=True))
    except Exception:
        pass


def parse_description(text: Optional[str]) -> Optional[Tuple[str, str]]:
    m = _MARKER_RE.search(text or "")
    return (m.group(1), m.group(2)) if m else None


def with_marker(text: Optional[str], channel: str, ts: str) -> str:
    """`text` with its thread line replaced (or appended) by one for (channel, ts)."""
    kept = [ln for ln in (text or "").splitlines() if not ln.strip().startswith(_MARKER)]
    return "\n".join(kept + [f"{_MARKER} {channel} {ts}"]).strip()


def _jira_lookup(version_id: str) -> Optional[Tuple[str, str]]:
    try:
        from . import jira_client
        token = config.get_jira_token()
        if not token:
            return None
        return parse_description(jira_client.fetch_version(version_id, token).get("description"))
    except (Exception, SystemExit):
        return None


def publish_root(version_id: str, channel: str, ts: str) -> Optional[str]:
    """Write the thread root into the Jira version's description so other operators'
    conductor can find it. Keeps any other description text. Returns an error string, or
    None on success."""
    try:
        from . import jira_client
        token = config.get_jira_token()
        if not token:
            return "no Jira token"
        current = jira_client.fetch_version(version_id, token).get("description")
        jira_client.update_version(version_id, token,
                                   description=with_marker(current, channel, ts))
        return None
    except (Exception, SystemExit) as e:
        return str(e) or e.__class__.__name__
