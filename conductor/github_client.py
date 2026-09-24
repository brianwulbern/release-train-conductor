"""GitHub: the two release calls `finalize` makes.

When a train ships, its release tags (cut earlier as pre-releases) are promoted to the repo's
full "latest" release. The production client is much larger (PR data, branch heads, combined
CI status, tag lookups, rate-limit routing); only these calls are needed here, and they are the
real implementation, extracted as-is. The production session also mounts a retry adapter that
retries transient 5xx errors on idempotent GETs only, so a write can never double-fire.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import requests

from . import config

_SESSION = requests.Session()


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_release_by_tag(repo: str, tag_name: str, token: str) -> "Optional[Dict[str, Any]]":
    """The GitHub Release object for a tag (id, prerelease, name, …), or None if
    the tag has no Release. GET /repos/{owner}/{repo}/releases/tags/{tag}."""
    try:
        resp = _SESSION.get(
            f"{config.GITHUB_API}/repos/{config.GITHUB_ORG}/{repo}/releases/tags/{tag_name}",
            headers=_headers(token), timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        return None
    return None


def mark_release_latest(repo: str, release_id: str, token: str) -> Dict[str, Any]:
    """Promote a release from pre-release to the repo's full/latest release:
    PATCH /repos/{owner}/{repo}/releases/{id} with prerelease=false and
    make_latest="true". Returns the updated release. Raises on non-2xx."""
    resp = requests.patch(
        f"{config.GITHUB_API}/repos/{config.GITHUB_ORG}/{repo}/releases/{release_id}",
        headers={
            **_headers(token),
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"prerelease": False, "make_latest": "true"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
