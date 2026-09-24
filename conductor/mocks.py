"""In-memory stand-ins for the three external services, so the demo runs with no network, no
tokens and no private data.

The production tool talks to an issue tracker (Jira: release versions, tickets, workflow
transitions), GitHub (release objects) and Slack (a threaded release channel). Here each is a
small fake SERVER that answers exactly the REST calls the real clients make:

* `FakeTracker` answers the Jira REST paths behind `jira_client` (versions, JQL search,
  issues, transitions, assignee, links, create). It keeps a subtask's DISPLAY order separate
  from its key number, like the real tracker, because the ordering logic depends on that.
* `FakeGitHub` answers the two release calls behind `github_client`.
* `FakeSlack` answers `chat.postMessage` behind `slack_client` and records every post.

`install()` patches the HTTP seam the clients already use (the Jira client's session, and
`requests.post` / `requests.patch`), so the real clients, and everything built on them
(finalize, the Jira progress engine, scope defer, the release-steps cloner), run unchanged.
"""
from __future__ import annotations

import contextlib
import itertools
import json
import os
import re
from typing import Any, Dict, List, Optional
from unittest import mock
from urllib.parse import unquote

import requests

from . import config, github_client, jira_client

STATUS_CATEGORY = {
    "Open": "new",
    "Selected for Development": "indeterminate",
    "In Progress": "indeterminate",
    "Ready for Release": "indeterminate",
    "Closed": "done",
}
_RESOLUTIONS = ["Completed", "Fixed", "Duplicate"]


class _Resp:
    """The slice of `requests.Response` the clients use."""

    def __init__(self, status: int, payload: Any = None):
        self.status_code = status
        self._payload = payload
        self.text = "" if payload is None else json.dumps(payload)
        self.headers: Dict[str, str] = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} from fake server")


# ─────────────────────────────────────────────────────────────────────────────
# Issue tracker
# ─────────────────────────────────────────────────────────────────────────────


class FakeTracker:
    """A tiny issue tracker: one project, its fix versions, and issues with a workflow."""

    def __init__(self, project_key: str = "REL", project_id: int = 10001, first_key: int = 3000):
        self.project_key = project_key
        self.project_id = project_id
        self.versions: Dict[str, Dict[str, Any]] = {}
        self.issues: Dict[str, Dict[str, Any]] = {}
        self._version_ids = itertools.count(40001)
        self._issue_nums = itertools.count(first_key)
        self.me = {"accountId": "acc-demo", "displayName": "Demo Operator"}
        self.writes: List[str] = []  # a human-readable log of every write

    # ── seeding ──────────────────────────────────────────────────────────────

    def add_version(self, name: str, *, released: bool = False, release_date: str = "",
                    start_date: str = "") -> str:
        vid = str(next(self._version_ids))
        self.versions[vid] = {
            "id": vid, "name": name, "released": released, "archived": False,
            "releaseDate": release_date, "startDate": start_date, "projectId": self.project_id,
        }
        return vid

    def add_issue(self, key: str, summary: str, *, issue_type: str = "Task",
                  status: str = "Open", fix_version: Optional[str] = None,
                  parent: Optional[str] = None) -> str:
        fields: Dict[str, Any] = {
            "summary": summary,
            "status": self._status(status),
            "issuetype": {"id": "1" if issue_type == "Task" else "2", "name": issue_type},
            "fixVersions": [self._vref(fix_version)] if fix_version else [],
            "subtasks": [],
            "assignee": None,
            "resolution": None,
            "labels": [], "components": [], "issuelinks": [],
            "priority": {"id": "3", "name": "Medium"},
            "description": "",
        }
        if parent:
            fields["parent"] = {"key": parent}
            self.issues[parent]["fields"]["subtasks"].append({"key": key})  # display order
        self.issues[key] = {"key": key, "id": str(10_000 + len(self.issues)), "fields": fields}
        return key

    def add_to_version(self, key: str, version_id: str) -> None:
        self.issues[key]["fields"]["fixVersions"].append(self._vref(version_id))

    # ── views ────────────────────────────────────────────────────────────────

    def ladder(self) -> List[str]:
        """Version names, oldest first, with a released marker."""
        out = []
        for v in sorted(self.versions.values(), key=lambda v: v["name"]):
            out.append(f"{v['name']}{'   [released]' if v['released'] else ''}")
        return out

    # ── helpers ──────────────────────────────────────────────────────────────

    def _status(self, name: str) -> Dict[str, Any]:
        return {"name": name, "statusCategory": {"key": STATUS_CATEGORY[name]}}

    def _vref(self, vid: str) -> Dict[str, str]:
        return {"id": vid, "name": self.versions[vid]["name"]}

    def _issue_view(self, issue: Dict[str, Any]) -> Dict[str, Any]:
        """The issue as the API returns it: subtasks expanded with summary + status."""
        f = dict(issue["fields"])
        f["subtasks"] = [
            {"key": s["key"], "fields": {
                "summary": self.issues[s["key"]]["fields"]["summary"],
                "status": self.issues[s["key"]]["fields"]["status"]}}
            for s in issue["fields"]["subtasks"]
        ]
        f["fixVersions"] = [self._vref(v["id"]) for v in issue["fields"]["fixVersions"]]
        return {"key": issue["key"], "id": issue["id"], "fields": f}

    def _search(self, jql: str) -> List[Dict[str, Any]]:
        m = re.fullmatch(r"fixVersion=(\d+)", jql.strip())
        if m:
            hits = [i for i in self.issues.values()
                    if any(v["id"] == m.group(1) for v in i["fields"]["fixVersions"])]
            return [self._issue_view(i) for i in sorted(hits, key=lambda i: _num(i["key"]))]
        m = re.match(r"parent\s*=\s*([A-Z]+-\d+)", jql.strip())
        if m:
            hits = [i for i in self.issues.values()
                    if (i["fields"].get("parent") or {}).get("key") == m.group(1)]
            return [self._issue_view(i) for i in sorted(hits, key=lambda i: _num(i["key"]))]
        m = re.search(r'summary ~ "([^"]+)"', jql)
        if m:
            words = m.group(1).lower()
            hits = [i for i in self.issues.values() if words in i["fields"]["summary"].lower()]
            return [self._issue_view(i) for i in hits]
        raise AssertionError(f"FakeTracker: unsupported JQL {jql!r}")

    def _transitions(self, key: str) -> List[Dict[str, Any]]:
        current = self.issues[key]["fields"]["status"]["name"]
        out = []
        for tid, target in (("11", "In Progress"), ("21", "Closed")):
            if target == current:
                continue
            t = {"id": tid, "name": f"to {target}", "to": self._status(target)}
            if target == "Closed":
                t["fields"] = {"resolution": {
                    "required": True, "allowedValues": [{"name": n} for n in _RESOLUTIONS]}}
            out.append(t)
        return out

    # ── the HTTP seam ────────────────────────────────────────────────────────

    def handle(self, method: str, url: str, params=None, body=None) -> _Resp:
        path = unquote(url.split("://", 1)[-1].split("/", 1)[1])
        path = "/" + path
        params = params or {}

        m = re.fullmatch(r"/rest/api/2/version/(\d+)", path)
        if m and method == "GET":
            return _Resp(200, dict(self.versions[m.group(1)]))
        if m and method == "PUT":
            v = self.versions[m.group(1)]
            for k in ("name", "released", "releaseDate", "startDate", "archived"):
                if k in body:
                    v[k] = body[k]
            self.writes.append(f"version {m.group(1)} <- {body}")
            return _Resp(200, dict(v))
        if path == "/rest/api/2/version" and method == "POST":
            vid = self.add_version(body["name"], release_date=body.get("releaseDate", ""),
                                   start_date=body.get("startDate", ""))
            self.writes.append(f"created version {body['name']}")
            return _Resp(201, dict(self.versions[vid]))
        if re.fullmatch(r"/rest/api/2/project/[A-Z]+/versions", path):
            return _Resp(200, [dict(v) for v in self.versions.values()])
        if path == "/rest/api/2/search/jql":
            return _Resp(200, {"issues": self._search(params.get("jql", ""))})
        if path == "/rest/api/2/myself":
            return _Resp(200, dict(self.me))
        if path == "/rest/api/2/issueLinkType":
            return _Resp(200, {"issueLinkTypes": [{"id": "1", "name": "Relates"}]})
        if path == "/rest/api/2/issueLink" and method == "POST":
            self.writes.append(f"linked {body['inwardIssue']['key']} -> {body['outwardIssue']['key']}")
            return _Resp(201, {})
        if path == "/rest/api/2/issue" and method == "POST":
            return self._create(body["fields"])

        m = re.fullmatch(r"/rest/api/2/issue/([A-Z]+-\d+)(/transitions|/assignee)?", path)
        if m:
            key, sub = m.group(1), m.group(2)
            issue = self.issues[key]
            if sub == "/transitions" and method == "GET":
                return _Resp(200, {"transitions": self._transitions(key)})
            if sub == "/transitions" and method == "POST":
                target = next(t for t in self._transitions(key)
                              if t["id"] == body["transition"]["id"])
                issue["fields"]["status"] = self._status(target["to"]["name"])
                res = ((body.get("fields") or {}).get("resolution") or {}).get("name")
                if res:
                    issue["fields"]["resolution"] = {"name": res}
                self.writes.append(f"{key} -> {target['to']['name']}")
                return _Resp(204)
            if sub == "/assignee":
                issue["fields"]["assignee"] = {"accountId": body["accountId"]}
                return _Resp(204)
            if method == "PUT":
                ids = [v["id"] for v in body["fields"]["fixVersions"]]
                issue["fields"]["fixVersions"] = [{"id": i} for i in ids]
                self.writes.append(f"{key} fixVersions <- {ids}")
                return _Resp(204)
            return _Resp(200, self._issue_view(issue))
        raise AssertionError(f"FakeTracker: unhandled {method} {path}")

    def _create(self, fields: Dict[str, Any]) -> _Resp:
        key = f"{self.project_key}-{next(self._issue_nums)}"
        parent = (fields.get("parent") or {}).get("key")
        itype = "Sub-task" if parent else "Task"
        self.add_issue(key, fields["summary"], issue_type=itype, parent=parent,
                       status="Open")
        for v in fields.get("fixVersions") or []:
            self.add_to_version(key, v["id"])
        self.writes.append(f"created {key}: {fields['summary']}")
        return _Resp(201, {"key": key, "id": self.issues[key]["id"]})


def _num(key: str) -> int:
    return int(key.rsplit("-", 1)[1])


# ─────────────────────────────────────────────────────────────────────────────
# GitHub and Slack
# ─────────────────────────────────────────────────────────────────────────────


class FakeGitHub:
    """Release objects keyed by (repo, tag). Tags start as pre-releases, as `release tag` cuts them."""

    def __init__(self):
        self.releases: Dict[tuple, Dict[str, Any]] = {}
        self._ids = itertools.count(501)

    def add_prerelease(self, repo: str, tag: str) -> None:
        self.releases[(repo, tag)] = {"id": next(self._ids), "tag_name": tag, "prerelease": True}

    def handle_get(self, url: str) -> _Resp:
        m = re.search(r"/repos/[^/]+/([^/]+)/releases/tags/(.+)$", url)
        rel = self.releases.get((m.group(1), m.group(2))) if m else None
        return _Resp(200, dict(rel)) if rel else _Resp(404, {"message": "Not Found"})

    def handle_patch(self, url: str, body: Dict[str, Any]) -> _Resp:
        rid = int(url.rsplit("/", 1)[1])
        for rel in self.releases.values():
            if rel["id"] == rid:
                rel["prerelease"] = bool(body.get("prerelease"))
                rel["latest"] = body.get("make_latest") == "true"
                return _Resp(200, dict(rel))
        return _Resp(404, {"message": "Not Found"})


class FakeSlack:
    """Records every `chat.postMessage`; threaded replies carry their parent's ts."""

    def __init__(self, channel_id: str = "C0RELEASES"):
        self.channel_id = channel_id
        self.posts: List[Dict[str, Any]] = []
        self._ts = itertools.count(1)

    def handle_post(self, url: str, body: Dict[str, Any]) -> _Resp:
        if not url.endswith("chat.postMessage"):
            return _Resp(200, {"ok": True})
        ts = f"1700000000.{next(self._ts):06d}"
        self.posts.append({"ts": ts, "text": body["text"], "thread_ts": body.get("thread_ts"),
                           "broadcast": bool(body.get("reply_broadcast"))})
        return _Resp(200, {"ok": True, "ts": ts, "channel": self.channel_id})


# ─────────────────────────────────────────────────────────────────────────────
# Wiring
# ─────────────────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def install(tracker: FakeTracker, gh: FakeGitHub, slack: FakeSlack):
    """Route the real clients to the fakes, with throwaway credentials in the environment."""
    env = {
        config.JIRA_EMAIL_ENV: "demo@example.com",
        config.JIRA_TOKEN_ENV: "demo-token",
        config.GITHUB_TOKEN_ENV: "demo-token",
        config.SLACK_TOKEN_ENV: "demo-token",
    }

    def s_get(url, params=None, **_kw):
        if "api.github.com" in url:
            return gh.handle_get(url)
        return tracker.handle("GET", url, params=params)

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, env))
        stack.enter_context(mock.patch.object(jira_client._SESSION, "get", side_effect=s_get))
        stack.enter_context(mock.patch.object(
            jira_client._SESSION, "put",
            side_effect=lambda url, json=None, **_kw: tracker.handle("PUT", url, body=json)))
        stack.enter_context(mock.patch.object(
            jira_client._SESSION, "post",
            side_effect=lambda url, json=None, **_kw: tracker.handle("POST", url, body=json)))
        stack.enter_context(mock.patch.object(
            github_client._SESSION, "get", side_effect=lambda url, **_kw: gh.handle_get(url)))
        stack.enter_context(mock.patch.object(
            requests, "patch", side_effect=lambda url, json=None, **_kw: gh.handle_patch(url, json)))
        stack.enter_context(mock.patch.object(
            requests, "post", side_effect=lambda url, json=None, **_kw: slack.handle_post(url, json)))
        yield
