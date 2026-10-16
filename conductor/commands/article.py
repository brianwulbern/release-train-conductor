"""The two questions finalize, recap and the menu ask about the customer-facing release
article. The production command also drafts the article itself from the deployed chart
values; that part reads private infrastructure and isn't exported."""
from __future__ import annotations

from typing import List, Optional


def published_url(mf) -> Optional[str]:
    """The article link recorded once it went live, newest wins."""
    urls = [a.get("url") for a in (getattr(mf, "actions", None) or [])
            if a.get("stage") == "article" and a.get("url")]
    return urls[-1] if urls else None


def needs_article(mf) -> List:
    """Service repos this train shipped that the article covers."""
    return [r for r in getattr(mf, "repos", None) or []
            if r.release_tag and r.classification == "service" and r.needs_pre_bump]
