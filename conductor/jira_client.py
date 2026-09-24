"""Jira API client.

Endpoints used:
    GET /rest/api/2/search/jql                      - list tickets in version
        (the classic /rest/api/2/search was removed by Atlassian in 2025;
        CHANGE-2046)
    GET /rest/api/2/version/{id}                    - version metadata
    GET /rest/dev-status/1.0/issue/detail?issueId=  - linked PRs per ticket

Auth: HTTP Basic with email + API token (the email goes in the username slot).
"""
from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

import requests
from requests.auth import HTTPBasicAuth

from . import config


# ─────────────────────────────────────────────────────────────────────────────
# Low-level HTTP
#
# Atlassian Cloud personal API tokens authenticate via HTTP Basic auth, with
# the user's account email as the username and the token as the password -
# NOT Bearer auth. (Bearer is for OAuth 2.0 access tokens, a separate beast.)
# Reference: https://developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis/
#
# A shared Session gives HTTP keep-alive so `manifest build`'s dozens of
# dev-status / subtask calls reuse one TLS connection instead of a fresh
# handshake each - the bulk of the "enumerate all the Jira tickets" time.
# requests.Session is safe for concurrent GETs from multiple threads (urllib3's
# connection pool is thread-safe), so a future parallel dev-status loop is fine.
# ─────────────────────────────────────────────────────────────────────────────

_SESSION = requests.Session()


def _auth(token: str) -> HTTPBasicAuth:
    email = config.get_jira_email()
    return HTTPBasicAuth(email, token)


def _get(path: str, token: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    url = f"{config.JIRA_HOST}{path}"
    resp = _SESSION.get(
        url,
        auth=_auth(token),
        headers={"Accept": "application/json"},
        params=params,
        timeout=30,
    )
    if resp.status_code in (401, 403):
        # Atlassian Cloud returns 401 for bad credentials and 403 for "auth
        # header structurally valid but wrong scheme" - both indicate the
        # user needs to fix env vars, not retry.
        sys.exit(
            f"error: Jira returned {resp.status_code} on {path}.\n"
            f"  Check both env vars are set:\n"
            f"    ${config.JIRA_EMAIL_ENV}  (your Atlassian account email)\n"
            f"    ${config.JIRA_TOKEN_ENV}  (token from id.atlassian.com)\n"
            f"  Quick test:\n"
            f"    curl -s -o /dev/null -w '%{{http_code}}\\n' \\\n"
            f"      -u \"${config.JIRA_EMAIL_ENV}:${config.JIRA_TOKEN_ENV}\" \\\n"
            f"      \"{url}\""
        )
    resp.raise_for_status()
    return resp.json()


def _put(path: str, body: Dict[str, Any], token: str) -> Dict[str, Any]:
    """PUT against Jira API. Used for version updates."""
    url = f"{config.JIRA_HOST}{path}"
    resp = _SESSION.put(
        url,
        auth=_auth(token),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    if resp.status_code in (401, 403):
        sys.exit(f"error: Jira returned {resp.status_code} on PUT {path} - check auth env vars")
    resp.raise_for_status()
    # Some PUT endpoints return empty bodies; tolerate that
    try:
        return resp.json()
    except Exception:
        return {}


def _post(path: str, body: Dict[str, Any], token: str) -> Dict[str, Any]:
    """POST against Jira API. Used for issue transitions."""
    url = f"{config.JIRA_HOST}{path}"
    resp = _SESSION.post(
        url,
        auth=_auth(token),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    if resp.status_code in (401, 403):
        sys.exit(f"error: Jira returned {resp.status_code} on POST {path} - check auth env vars")
    # Transition POST returns 204 No Content on success
    if resp.status_code == 204:
        return {}
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def fetch_version(version_id: str, token: str) -> Dict[str, Any]:
    """GET /rest/api/2/version/{id} - name, releaseDate, released, etc."""
    return _get(f"/rest/api/2/version/{version_id}", token)


def fetch_issue(key: str, token: str, fields: List[str] | None = None) -> Dict[str, Any]:
    """GET /rest/api/2/issue/{key}, optionally limited to `fields` (e.g.
    ['subtasks', 'issuelinks', 'status'])."""
    params = {"fields": ",".join(fields)} if fields else None
    return _get(f"/rest/api/2/issue/{key}", token, params=params)


def fetch_project_versions(project_key: str, token: str) -> List[Dict[str, Any]]:
    """Return every fix version configured for a Jira project."""
    payload = _get(f"/rest/api/2/project/{project_key}/versions", token)
    return payload if isinstance(payload, list) else []


def find_current_version(project_key: str, token: str) -> Optional[Dict[str, Any]]:
    """Pick the ACTIVE version whose name contains '(CURRENT)'.

    Archived and released versions are EXCLUDED outright - a stale '(CURRENT)'
    left on an archived/shipped version must never be announced or built against.
    (Real case: an archived `2026-06-25 (CURRENT)` lingered and `conductor announce`
    resolved it instead of the live train.) If several active versions still
    match, tiebreak by highest numeric id (most recent).
    """
    versions = fetch_project_versions(project_key, token)
    candidates = [
        v for v in versions
        if "(CURRENT)" in (v.get("name") or "")
        and not v.get("archived") and not v.get("released")
    ]
    if not candidates:
        return None

    def _id_int(v: Dict[str, Any]) -> int:
        try:
            return int(v.get("id") or 0)
        except (TypeError, ValueError):
            return 0

    candidates.sort(key=lambda v: -_id_int(v))
    return candidates[0]


def fetch_version_issues(
    version_id: str,
    token: str,
    fields: List[str] | None = None,
    page_size: int = 100,
    max_pages: int = 50,
) -> List[Dict[str, Any]]:
    """Return all issues with fixVersion={version_id}.

    Uses the /rest/api/2/search/jql endpoint (replacement for the removed
    classic /rest/api/2/search). Pagination is via nextPageToken - there is
    no `total` field in the new response shape.

    `max_pages` is a defensive cap. Atlassian has a known bug where the same
    nextPageToken can be returned across pages, producing infinite loops; we
    also dedupe seen tokens for the same reason.
    """
    if fields is None:
        fields = [
            "summary",
            "status",
            "issuetype",
            "fixVersions",
            "components",
            "labels",
            "assignee",
            "description",  # scanned for PR links conductor's other signals miss
            config.JIRA_GIT_PR_FIELD,  # "Git Pull Request" - a directly-linked PR URL
        ]

    issues: List[Dict[str, Any]] = []
    next_page_token: str | None = None
    seen_tokens: set[str] = set()

    for _ in range(max_pages):
        params: Dict[str, Any] = {
            "jql": f"fixVersion={version_id}",
            "fields": ",".join(fields),
            "maxResults": page_size,
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token

        payload = _get("/rest/api/2/search/jql", token, params=params)
        batch = payload.get("issues", [])
        if not batch:
            break
        issues.extend(batch)

        new_token = payload.get("nextPageToken")
        if not new_token:
            break
        if new_token in seen_tokens:
            # Known upstream bug: same token returned across pages. Stop.
            break
        seen_tokens.add(new_token)
        next_page_token = new_token

    return issues


def search_issues(
    jql: str,
    token: str,
    fields: List[str] | None = None,
    page_size: int = 100,
    max_pages: int = 50,
) -> List[Dict[str, Any]]:
    """Run an arbitrary JQL search via /rest/api/2/search/jql. Same pagination
    and same-token-loop guard as fetch_version_issues, but caller-supplied JQL
    (e.g. `parent = REL-47502 ORDER BY key ASC`)."""
    if fields is None:
        fields = ["summary", "status", "issuetype"]
    issues: List[Dict[str, Any]] = []
    next_page_token: str | None = None
    seen_tokens: set[str] = set()
    for _ in range(max_pages):
        params: Dict[str, Any] = {
            "jql": jql, "fields": ",".join(fields), "maxResults": page_size,
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token
        payload = _get("/rest/api/2/search/jql", token, params=params)
        batch = payload.get("issues", [])
        if not batch:
            break
        issues.extend(batch)
        new_token = payload.get("nextPageToken")
        if not new_token or new_token in seen_tokens:
            break
        seen_tokens.add(new_token)
        next_page_token = new_token
    return issues


def create_issue(fields: Dict[str, Any], token: str) -> Dict[str, Any]:
    """POST /rest/api/2/issue. `fields` is the raw Jira create payload's fields
    object (project, issuetype, summary, …). Returns the created issue
    (includes its new `key`)."""
    return _post("/rest/api/2/issue", {"fields": fields}, token)


def get_myself(token: str) -> Dict[str, Any]:
    """GET /rest/api/2/myself - the authenticated user (accountId, displayName …)."""
    return _get("/rest/api/2/myself", token)


def assign_issue(key: str, account_id: str, token: str) -> None:
    """Set an issue's assignee by accountId. PUT /rest/api/2/issue/{key}/assignee."""
    _put(f"/rest/api/2/issue/{key}/assignee", {"accountId": account_id}, token)


def update_issue_fix_versions(key: str, version_ids: List[str], token: str) -> None:
    """Set an issue's fixVersions to exactly `version_ids` (a list of version id
    strings). PUT /rest/api/2/issue/{key}. Callers compute the full target list
    (e.g. drop the CURRENT release version, add (NEXT)) so nothing else is lost."""
    _put(
        f"/rest/api/2/issue/{key}",
        {"fields": {"fixVersions": [{"id": str(v)} for v in version_ids]}},
        token,
    )


def get_issue_link_types(token: str) -> List[Dict[str, Any]]:
    """Available issue link types for the instance: [{id, name, inward, outward}]."""
    payload = _get("/rest/api/2/issueLinkType", token)
    return payload.get("issueLinkTypes", []) or []


def link_issues(inward_key: str, outward_key: str, type_name: str, token: str) -> None:
    """Create an issue link of `type_name` between two issues. The relationship
    reads: outwardIssue <outward> inwardIssue. POST /rest/api/2/issueLink."""
    _post("/rest/api/2/issueLink", {
        "type": {"name": type_name},
        "inwardIssue": {"key": inward_key},
        "outwardIssue": {"key": outward_key},
    }, token)


def verify(email: str, token: str) -> "tuple[bool, str]":
    """Probe /myself to confirm Jira creds actually work. Returns (ok, detail).
    Never raises - for `conductor doctor`."""
    try:
        resp = requests.get(
            f"{config.JIRA_HOST}/rest/api/2/myself",
            auth=HTTPBasicAuth(email, token), timeout=15,
        )
        if resp.status_code == 200:
            d = resp.json() or {}
            who = d.get("displayName") or d.get("emailAddress") or "authenticated"
            return True, f"{who} (probe OK)"
        if resp.status_code in (401, 403):
            return False, f"auth rejected (HTTP {resp.status_code}) - check email + token"
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, f"error: {e}"


def fetch_issue_dev_status(issue_id: str, token: str) -> Dict[str, Any]:
    """Return dev-status payload (linked GitHub PRs) for an issue.

    issue_id is the *internal numeric* ID - not the REL-XXXXX key. The numeric
    id is in `issue["id"]` from a search payload.
    """
    return _get(
        "/rest/dev-status/1.0/issue/detail",
        token,
        params={
            "issueId": issue_id,
            "applicationType": "GitHub",
            "dataType": "pullrequest",
        },
    )


def extract_pr_urls(dev_status_payload: Dict[str, Any]) -> List[str]:
    """Pull GitHub PR URLs out of a dev-status response.

    Schema (verified for Atlassian Cloud):
        { "detail": [ { "pullRequests": [ { "url": "...", "status": "MERGED", ... } ] } ] }

    We return only PRs whose status is MERGED.
    """
    urls: List[str] = []
    for detail in dev_status_payload.get("detail", []):
        for pr in detail.get("pullRequests", []):
            status = (pr.get("status") or "").upper()
            url = pr.get("url")
            if url and status == "MERGED":
                urls.append(url)
    return urls


def fetch_subtasks(parent_key: str, token: str, max_pages: int = 5) -> List[Dict[str, Any]]:
    """Return subtask issues of `parent_key` via the JQL search endpoint.

    parent_key is the REL-XXXXX form (not the numeric internal id).
    """
    issues: List[Dict[str, Any]] = []
    next_page_token: str | None = None
    for _ in range(max_pages):
        params: Dict[str, Any] = {
            "jql": f"parent = {parent_key}",
            "fields": f"summary,status,issuetype,assignee,{config.JIRA_GIT_PR_FIELD}",
            "maxResults": 50,
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token

        payload = _get("/rest/api/2/search/jql", token, params=params)
        batch = payload.get("issues", [])
        if not batch:
            break
        issues.extend(batch)
        new_token = payload.get("nextPageToken")
        if not new_token:
            break
        next_page_token = new_token
    return issues


# ─────────────────────────────────────────────────────────────────────────────
# Housekeeping (used by `conductor finalize`)
# ─────────────────────────────────────────────────────────────────────────────


def update_version(
    version_id: str,
    token: str,
    *,
    name: Optional[str] = None,
    released: Optional[bool] = None,
    release_date: Optional[str] = None,
    start_date: Optional[str] = None,
    archived: Optional[bool] = None,
) -> Dict[str, Any]:
    """Update fields on a Jira version.

    Only the kwargs you pass are sent - Jira leaves other fields untouched.
    Setting `released=True` is equivalent to "Release" in the UI; no
    moveUnfixedIssuesTo is provided, which matches the "Ignore unresolved
    work items" radio (unresolved issues stay associated with this version).

    `release_date` / `start_date` must be ISO-format `YYYY-MM-DD`.
    """
    body: Dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if released is not None:
        body["released"] = released
    if release_date is not None:
        body["releaseDate"] = release_date
    if start_date is not None:
        body["startDate"] = start_date
    if archived is not None:
        body["archived"] = archived
    if not body:
        return {}
    return _put(f"/rest/api/2/version/{version_id}", body, token)


def create_version(
    project_id: Any,
    name: str,
    token: str,
    *,
    start_date: Optional[str] = None,
    release_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new fix version. POST /rest/api/2/version.

    `project_id` is the numeric Jira project id (read it off any existing
    version via `fetch_version(...)['projectId']`). `start_date` / `release_date`
    are ISO `YYYY-MM-DD`; Jira requires start_date <= release_date. Returns the
    created version object (includes its new `id`).
    """
    body: Dict[str, Any] = {"projectId": int(project_id), "name": name}
    if start_date is not None:
        body["startDate"] = start_date
    if release_date is not None:
        body["releaseDate"] = release_date
    return _post("/rest/api/2/version", body, token)


def get_issue_transitions(
    issue_key: str, token: str, *, expand_fields: bool = False,
) -> List[Dict[str, Any]]:
    """List available workflow transitions for an issue from its current status.

    Each entry has at least: id, name, to.name (target status). What's
    available depends on the issue's current status and the caller's perms.
    With expand_fields=True, each entry also carries `fields` (transition-screen
    fields + their `required` flag and `allowedValues`) - needed to know whether
    a transition (e.g. Closed) requires a `resolution`, and which values it takes.
    """
    params = {"expand": "transitions.fields"} if expand_fields else None
    payload = _get(f"/rest/api/2/issue/{issue_key}/transitions", token, params=params)
    return payload.get("transitions", []) or []


def transition_issue(
    issue_key: str,
    transition_id: str,
    token: str,
    *,
    resolution: Optional[str] = None,
) -> None:
    """Execute a workflow transition on an issue.

    If `resolution` is given (e.g. "Fixed"), it's set on the transition screen.
    Some workflows require resolution at close time; passing it makes the
    transition succeed where it would otherwise 400.
    """
    body: Dict[str, Any] = {"transition": {"id": transition_id}}
    if resolution:
        body["fields"] = {"resolution": {"name": resolution}}
    _post(f"/rest/api/2/issue/{issue_key}/transitions", body, token)
