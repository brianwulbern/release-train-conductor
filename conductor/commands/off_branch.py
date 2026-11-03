"""Merge readiness that asks where a PR merged, not only whether it did.

A ticket's PR can merge into a feature branch instead of the repo's default branch. It reads as
merged, but none of that work is in the release until the branch itself reaches the default.
During the manifest build, `_off_branch_prs` reads each ticket PR's base branch. When the base is
not the default and the branch hasn't reached it since (it isn't behind or identical to the
default, and no PR from it has merged there), the PR leaves the ticket's merged list and is shown
as BRANCH work, with the last merged PR and the next step: the open PR to the default branch, or
the compare link that opens one. `off_branch_lines` formats that for the operator.

Both functions below are the real implementation, extracted as-is from the manifest build.
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from .. import config, github_client


def _off_branch_prs(tickets, token) -> int:
    """Move each ticket's PRs that merged into a branch other than the repo's
    default (and whose branch hasn't reached it since) out of pr_urls, into
    unmerged_prs as state BRANCH with the last merged PR and the next step: the
    open PR to the default branch, or the link that opens one. Returns how many
    tickets it touched. Best-effort: a lookup that fails leaves the PR as is."""
    org = config.GITHUB_ORG.lower()
    urls = sorted({u for t in tickets for u in t.pr_urls
                   if (github_client.parse_pr_url(u) or ("",))[0].lower() == org})
    if not urls:
        return 0

    def _fetch(url):
        _o, repo, num = github_client.parse_pr_url(url)
        try:
            return url, repo, github_client.pr_payload(repo, int(num), token)
        except (Exception, SystemExit):
            return url, repo, {}

    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as ex:
        fetched = {u: (repo, pr) for u, repo, pr in ex.map(_fetch, urls)}
    defaults: Dict[str, Optional[str]] = {}
    covered: Dict[tuple, Optional[str]] = {}

    def _default(repo):
        if repo not in defaults:
            try:
                defaults[repo] = github_client.get_default_branch(repo, token)
            except (Exception, SystemExit):
                defaults[repo] = None
        return defaults[repo]

    def _next_step(repo, base, default):
        if (repo, base) not in covered:
            nxt = f"https://github.com/{config.GITHUB_ORG}/{repo}/compare/{default}...{base}?expand=1"
            try:
                if github_client.compare_status(repo, default, base, token) in ("behind", "identical"):
                    nxt = None
                else:
                    for pr in github_client.branch_prs(repo, base, default, token):
                        if pr.get("merged_at"):
                            nxt = None
                            break
                        if pr.get("state") == "open":
                            nxt = pr.get("html_url") or nxt
            except (Exception, SystemExit):
                nxt = None
            covered[(repo, base)] = nxt
        return covered[(repo, base)]

    touched = 0
    for t in tickets:
        groups: Dict[tuple, list] = defaultdict(list)
        for u in t.pr_urls:
            repo, pr = fetched.get(u, (None, {}))
            base = (pr.get("base") or {}).get("ref")
            default = _default(repo) if repo and pr.get("merged_at") else None
            if base and default and base != default:
                groups[(repo, base, default)].append((pr.get("merged_at"), u, pr.get("number")))
        moved = []
        for (repo, base, default), prs in groups.items():
            nxt = _next_step(repo, base, default)
            if nxt is None:
                continue
            prs.sort()
            moved += [u for _a, u, _n in prs]
            t.unmerged_prs.append({
                "url": prs[-1][1], "state": "BRANCH", "repo": repo, "base": base,
                "default": default, "prs": [n for _a, _u, n in prs], "next": nxt,
            })
        if moved:
            touched += 1
            t.pr_urls = [u for u in t.pr_urls if u not in moved]
            nexts = {e["next"] for e in t.unmerged_prs if e.get("state") == "BRANCH"}
            t.unmerged_prs = [e for e in t.unmerged_prs
                              if e.get("state") == "BRANCH" or e.get("url") not in nexts]
    return touched


def off_branch_lines(entry: dict) -> List[str]:
    nums = ", ".join(f"#{n}" for n in entry.get("prs") or [])
    default = entry.get("default") or "master"
    nxt = entry.get("next") or ""
    verb = "PR to " + default + " is open" if "/pull/" in nxt else "open a PR to " + default
    return [f"{entry.get('repo', '?')} {nums} merged into {entry.get('base')}, not {default}",
            f"last merged: {entry.get('url')}",
            f"{verb}: {nxt}"]
