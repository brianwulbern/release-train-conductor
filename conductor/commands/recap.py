"""`conductor recap [manifest]` - a human-readable end-of-cycle summary of a release.

The pipeline scatters a release across ten commands and a growing activity log;
`recap` gathers it back into one page: what shipped (the tagged platform/service
artifacts), which tickets rode along (counted by type), the chronological trail
of side-effecting steps taken (from the manifest's action log), and any manual
follow-ups still owed (service articles, unclosed bugs).

It's the natural thing to read when a cycle finishes - finalize prints it on a
successful --apply - and the thing to paste into a Jira comment or Slack thread
when someone asks "what went out this train?" (`--markdown` for that).

Read-only and network-free: everything comes from the manifest on disk. Live
Jenkins verdicts live in `conductor test status`; the real QA sign-off in a sibling QA tool.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .. import manifest as M


# Issue types get a stable display order; anything else sorts alpha after these.
_TYPE_ORDER = ["Bug", "Task", "Story", "Epic", "Sub-task"]

# Friendly labels for the raw action-log stage keys.
_STAGE_LABELS = {
    "manifest": "manifest",
    "bump_pre": "pre-bump",
    "bump_stg": "stg bump",
    "bump_prd": "prd bump",
    "release": "release tag",
    "test": "test",
    "promote": "promote",
    "cluster": "cluster",
    "finalize": "finalize",
    "jira": "jira",
    "announce": "announce",
    "article": "article",
}


def _fmt_ts(iso: str) -> str:
    """Compact `MM-DD HH:MM` from an ISO timestamp; the raw string if unparseable."""
    try:
        d = datetime.fromisoformat(iso)
        return d.strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return (iso or "")[:16]


def _type_sort_key(t: str):
    try:
        return (0, _TYPE_ORDER.index(t))
    except ValueError:
        return (1, t.lower())


def _ticket_type_counts(mf: M.Manifest):
    """[(type_name, count)] in display order; blank types bucket as 'untyped'."""
    counts = {}
    for t in mf.tickets:
        typ = (t.issue_type or "untyped").strip() or "untyped"
        counts[typ] = counts.get(typ, 0) + 1
    return sorted(counts.items(), key=lambda kv: _type_sort_key(kv[0]))


def _shipped(mf: M.Manifest):
    """Tagged platform/service repos - the artifacts production actually pulls."""
    return [
        r for r in mf.repos
        if r.release_tag and r.classification in ("platform", "service")
    ]


def _is_finalized(mf: M.Manifest) -> bool:
    return any(a.get("stage") == "finalize" for a in mf.actions)


def build_recap(mf: M.Manifest, markdown: bool = False) -> List[str]:
    """Render the recap as a list of display lines. Pure - no network, no I/O.

    `markdown=True` emits a paste-friendly block (headers + bullets) for a Jira
    comment or Slack thread; otherwise a terminal-styled report.
    """
    L: List[str] = []
    h1 = (lambda s: L.append(f"## {s}")) if markdown else (lambda s: L.append(s))
    bullet = "- " if markdown else "  · "

    finalized = _is_finalized(mf)
    status = "finalized ✓" if finalized else "in flight"
    title = f"Release recap - {mf.release_label}  ({status})"
    if markdown:
        L.append(f"# {title}")
    else:
        L.append(title)
        L.append("─" * len(title))
    L.append("")

    # ── Shipped artifacts ────────────────────────────────────────────────────
    shipped = _shipped(mf)
    h1(f"Shipped ({len(shipped)} repo{'s' if len(shipped) != 1 else ''})")
    if shipped:
        width = max((len(r.repo) for r in shipped), default=0)
        for cls in ("platform", "service"):
            group = [r for r in shipped if r.classification == cls]
            if not group:
                continue
            if markdown:
                for r in sorted(group, key=lambda r: r.repo):
                    L.append(f"- **{cls}** {r.repo} - `{r.release_tag}`")
            else:
                L.append(f"  {cls}:")
                for r in sorted(group, key=lambda r: r.repo):
                    L.append(f"      {r.repo:<{width}}  {r.release_tag}")
    else:
        L.append(f"{bullet}(no platform/service repos tagged yet)")
    # Note untagged/meta repos so a charts-only or in-flight cycle isn't silent.
    other = [r for r in mf.repos if r not in shipped]
    if other:
        names = ", ".join(sorted(r.repo for r in other))
        L.append(f"{bullet}(also in scope, not tagged: {names})")
    L.append("")

    # ── Tickets ──────────────────────────────────────────────────────────────
    counts = _ticket_type_counts(mf)
    summary = ", ".join(f"{n} {typ}" for typ, n in counts) or "none"
    h1(f"Tickets ({len(mf.tickets)}) - {summary}")
    for t in sorted(mf.tickets, key=lambda t: _type_sort_key(t.issue_type or "")):
        typ = f", {t.issue_type}" if t.issue_type else ""
        L.append(f"{bullet}{t.key} [{t.status}{typ}]  {t.summary[:70]}")
    if not mf.tickets:
        L.append(f"{bullet}(no tickets recorded)")
    L.append("")

    # ── Timeline (the activity log) ──────────────────────────────────────────
    h1(f"Timeline ({len(mf.actions)} step{'s' if len(mf.actions) != 1 else ''})")
    if mf.actions:
        for a in mf.actions:
            stage = _STAGE_LABELS.get(a.get("stage", ""), a.get("stage", "") or "?")
            line = f"{bullet}{_fmt_ts(a.get('at', ''))}  {stage:<11} {a.get('summary', '')}"
            L.append(line)
            if a.get("url"):
                L.append(f"{'      ' if not markdown else '  '}↳ {a['url']}")
    else:
        L.append(f"{bullet}(no steps recorded yet)")
    L.append("")

    # ── Follow-ups ───────────────────────────────────────────────────────────
    followups: List[str] = []
    from . import article as _article
    if not _article.published_url(mf):
        for r in _shipped(mf):
            if r.classification == "service" and r.needs_pre_bump:
                followups.append(
                    f"service article required (support) - {r.repo} {r.release_tag}"
                )
    open_bugs = [
        t for t in mf.tickets
        if (t.issue_type or "").strip().lower() == "bug"
        and t.status.strip().lower() != "closed"
    ]
    if open_bugs:
        keys = ", ".join(t.key for t in open_bugs)
        followups.append(f"{len(open_bugs)} Bug(s) not yet Closed: {keys}")

    added = mf.tickets_added_after_test()
    if added:
        keys = ", ".join(t.key for t in added)
        followups.append(
            f"{len(added)} ticket(s) added AFTER testing started (shipped untested): {keys}"
        )

    if followups:
        h1("Follow-ups")
        for f in followups:
            L.append(f"{bullet}{'⚠ ' if not markdown else ''}{f}")
        L.append("")

    return L


def run(args) -> int:
    if args.manifest:
        manifest_path = Path(args.manifest).expanduser().resolve()
    else:
        manifest_path = M.default_manifest_path()
        if not manifest_path:
            sys.exit("no manifest in ~/.conductor/state/; run `conductor manifest build` first")
    if not manifest_path.exists():
        sys.exit(f"manifest not found: {manifest_path}")
    mf = M.read_manifest(manifest_path)

    lines = build_recap(mf, markdown=bool(getattr(args, "markdown", False)))
    print()
    for line in lines:
        print(line)
    return 0
