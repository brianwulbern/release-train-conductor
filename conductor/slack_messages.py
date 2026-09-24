"""Pure builders for the threaded release-cycle Slack messages.

Each function returns the message text (Slack mrkdwn) for one pipeline step; the
`slack_thread.post_update` helper delivers it as a threaded reply under the pinned
announcement. Kept pure (no I/O) so the exact wording/emoji is trivially testable
and matches the team's canonical hand-posted flow.

Emoji: standard ones are literal Unicode (what the operator types by hand);
`:review:` is a Slack custom emoji shortcode and passes through verbatim.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from . import config


def closing_the_door() -> str:
    """The scope-freeze broadcast - fired by the `d` utility or auto on the first
    bump. Deliberately carries no PR links (it can fire early, before any exist);
    the pre-bump post links its own PRs. The 🤝 carries the operator's configured
    skin tone (config.SLACK_SKIN_TONE) since the API won't apply it automatically."""
    return ("closing the door 🚪 on this release train 🚂. "
            f"Please add any ongoing work to the NEXT cycle. Thanks 🤝{config.SLACK_SKIN_TONE}")


def pre_bump(pr_urls: Sequence[str]) -> str:
    """Service VERSION.txt pre-bump PR(s) (option 3)."""
    lines = [":review: service pre-bump PR(s):", *pr_urls]
    return "\n".join(lines)


def stg_bump(pr_url: str) -> str:
    """stg.yaml bump for testing on develop (option 4)."""
    return f":review: pull to bump `stg.yaml` on `develop`\n{pr_url}"


def release_tags_cut(tags: Sequence[Tuple[str, str]]) -> str:
    """Pre-release tags cut (option 5). `tags` = [(repo, tag_url), …]."""
    lines = ["pre-release tags cut:", *[url for _repo, url in tags]]
    return "\n".join(lines)


def platform_testing(build: object, url: Optional[str] = None) -> str:
    """Platform functional tests kicked off (option 6). `build` is the Jenkins
    build number; when a `url` is known the number links to the run. `:loading:`
    is a Slack custom-emoji shortcode (an animated wheel) - passes through verbatim."""
    ref = f"<{url}|{build}>" if url else f"{build}"
    return f"Platform testing ({ref}) :loading:"


def services_testing(build: object, url: Optional[str] = None) -> str:
    """Services functional tests kicked off (option 7)."""
    ref = f"<{url}|{build}>" if url else f"{build}"
    return f"Services testing ({ref}) :loading:"


def prd_bump(pr_url: str) -> str:
    """prd.yaml bump on develop (option 8)."""
    return f":review: pull to bump `prd.yaml` on `develop`\n{pr_url}"


def promote(pr_url: str, reviewer_ids: Optional[Sequence[str]] = None) -> str:
    """develop→production promote PR (option 9), @-mentioning the charts reviewers.
    reviewer_ids are Slack member ids (U…); rendered as `<@U…>` real mentions."""
    line = f":review: pull to bump `prd.yaml` on `production`\n{pr_url}"
    mentions = " ".join(f"<@{u}>" for u in (reviewer_ids or []) if u)
    return f"{line}\n{mentions}" if mentions else line


def argocd_sync(date_label: str) -> str:
    """About to sync ArgoCD (option 12) - broadcast."""
    return f"📢 Syncing ArgoCD for Release Train {date_label}"


def finished_syncing(date_label: str) -> str:
    """Cycle finished (new step) - broadcast."""
    return (f"🏁 Finished syncing. Release Train {date_label} is finished 🏁\n"
            "Thank you everyone involved!")


def empty_cycle(date_label: str, next_label: str = "") -> str:
    """No code shipped this cycle (option 13, empty-train variant) - broadcast.
    Posted in place of `finished_syncing` when the manifest has no repos, so the
    thread gets a clean close-out before finalize instead of an abrupt end. Reads
    the same with or without a known next-train date (falls back to a bare 'next
    train begins momentarily')."""
    nxt = (f" The next train ({next_label}) begins momentarily."
           if next_label else " The next train begins momentarily.")
    return (f"No new code landed for Release Train {date_label}, so there's "
            f"nothing to test this cycle. Wrapping it up now.{nxt} 🚂")
