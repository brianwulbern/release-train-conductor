"""GitHub: the two release calls `finalize` makes, and the PR lookups the off-branch check uses.

When a train ships, its release tags (cut earlier as pre-releases) are promoted to the repo's
full "latest" release. The production client is much larger (PR data, branch heads, combined
CI status, tag lookups, rate-limit routing); only these calls are needed here, and they are the
real implementation, extracted as-is, except `_get`: the production one routes a 401 or 403
to a diagnostic (bad token, SSO not authorized, rate limit) instead of raising. The production
session also mounts a retry adapter that retries transient 5xx errors on idempotent GETs only,
so a write can never double-fire.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import requests

from . import config

_SESSION = requests.Session()


def _get(path: str, token: str, params: Dict[str, Any] | None = None) -> Any:
    resp = _SESSION.get(f"{config.GITHUB_API}{path}", headers=_headers(token), params=params,
                        timeout=30)
    resp.raise_for_status()
    return resp.json()


_PR_URL_RE = re.compile(
    r"github\.com/(?P<org>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)


def parse_pr_url(url: str) -> Optional[Tuple[str, str, int]]:
    """Return (org, repo, pr_number) from a GitHub PR URL, or None."""
    m = _PR_URL_RE.search(url)
    if not m:
        return None
    return m.group("org"), m.group("repo"), int(m.group("number"))


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_default_branch(repo: str, token: str) -> Optional[str]:
    """Return the repo's default branch (e.g. 'master', 'main', 'develop')."""
    payload = _get(f"/repos/{config.GITHUB_ORG}/{repo}", token)
    return payload.get("default_branch") if payload else None


def pr_payload(repo: str, number: int, token: str) -> Dict[str, Any]:
    return _get(f"/repos/{config.GITHUB_ORG}/{repo}/pulls/{number}", token) or {}


def branch_prs(repo: str, head: str, base: str, token: str) -> List[Dict[str, Any]]:
    """PRs (any state) from branch `head` into `base`, newest first."""
    return _get(f"/repos/{config.GITHUB_ORG}/{repo}/pulls", token,
                {"head": f"{config.GITHUB_ORG}:{head}", "base": base, "state": "all"}) or []


def compare_status(repo: str, base: str, head: str, token: str) -> Optional[str]:
    """GitHub's comparison status of base…head: 'ahead' | 'behind' | 'identical'
    | 'diverged', or None on error. 'ahead' means `head` is ahead of `base`
    (i.e., base is an ancestor that head has commits beyond)."""
    try:
        payload = _get(
            f"/repos/{config.GITHUB_ORG}/{repo}/compare/{base}...{head}", token,
        )
    except SystemExit:
        raise
    except Exception:
        return None
    return payload.get("status") if isinstance(payload, dict) else None


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
