"""Docker registry: picking the right image tag when one build publishes several.

Some images ship one variant per base track from the same commit (a search image built on two
major versions, an app server image per product line). The production client lists the
registry's tags for a build or a release and passes the matches here, newest first, with the
base the chart already uses; this rule decides which one the bump writes. The listing calls,
auth and private-image handling are not included; this is the selection rule, extracted as-is.
"""
from __future__ import annotations

import re
from typing import Optional


def _major(prefix: str) -> str:
    m = re.match(r"^(\d+)\.", prefix or "")
    return m.group(1) + "." if m else ""


def pick_on_track(matches: list, prefer_prefix: str = "") -> Optional[str]:
    """Pick one of several tags (newest first) for the same build or release.

    The chart's exact base wins (`7.4-` for app-server). When that base is gone
    because it moved within its major (elasticsearch `8.19.23-` to `8.19.26-`,
    nginx `1.31.2-` to `1.31.3-`), the newest tag on the same major wins, so a
    variant on another major (`9.5.7-`) is never picked over it. Otherwise the
    newest tag wins.
    """
    if not matches:
        return None
    if prefer_prefix:
        exact = next((m for m in matches if m.startswith(prefer_prefix)), None)
        if exact:
            return exact
        major = _major(prefer_prefix)
        if major:
            same = next((m for m in matches if m.startswith(major)), None)
            if same:
                return same
    return matches[0]
