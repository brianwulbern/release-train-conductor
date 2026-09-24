"""Release-ladder health - detect (and plan a repair for) a degraded
CURRENT → NEXT → OVERNEXT → SUBSEQUENT Jira version ladder.

Born from a live 2026-07-30 incident: a finalize left the ladder with **no
active `(CURRENT)`** (a missing `(NEXT)` rung meant the label shift promoted
nothing up to CURRENT), and `conductor announce` then resolved a stale, *archived*
`(CURRENT)` from a month earlier. v0.86.0 stopped finalize from *creating* that
state; this module detects an already-degraded ladder before it bites at
announce, and plans the same date-anchored normalization.

Pure + testable: `assess(versions, today)` takes the raw Jira version list (as
`jira_client.fetch_project_versions` returns it) and today's date, and returns a
`LadderHealth` with the problems found and the exact renames/creations that would
fix them. Applying the plan lives in `commands/ladder.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from .commands.finalize import (
    LADDER_SUFFIXES,
    _ladder_suffix,
    _next_train_date,
    _parse_release_date,
    _prev_train_date,
)

CURRENT_SUFFIX = LADDER_SUFFIXES[0]  # " (CURRENT)"


@dataclass
class LadderHealth:
    ok: bool                       # False ⇒ BLOCKING (breaks announce / needs repair)
    issues: List[str] = field(default_factory=list)       # blocking problems
    advisories: List[str] = field(default_factory=list)   # non-blocking (short ladder, stale archived)
    # (date, id, name, suffix) for the active laddered versions, by date.
    active: List[Tuple[date, str, str, str]] = field(default_factory=list)
    stale_current: List[Dict[str, Any]] = field(default_factory=list)
    renames: List[Tuple[str, str, str]] = field(default_factory=list)     # (id, old_name, new_name)
    creations: List[Tuple[str, str, str]] = field(default_factory=list)   # (name, start_iso, release_iso)

    @property
    def needs_repair(self) -> bool:
        return bool(self.renames or self.creations)


def _active_ladder(versions: List[Dict[str, Any]], today: date) -> List[Tuple[date, str, str, str]]:
    """Non-archived, non-released versions carrying a ladder suffix with a
    parseable date on/after `today`, sorted by date (earliest = the current
    train)."""
    out: List[Tuple[date, str, str, str]] = []
    for v in versions:
        if v.get("archived") or v.get("released"):
            continue
        name = v.get("name") or ""
        suf = _ladder_suffix(name)
        if not suf:
            continue
        d = _parse_release_date(name)
        if d is None or d < today:
            continue
        out.append((d, str(v.get("id")), name, suf))
    out.sort(key=lambda t: t[0])
    return out


def _plan_creations(
    active: List[Tuple[date, str, str, str]],
    versions: List[Dict[str, Any]],
    today: date,
) -> List[Tuple[str, str, str]]:
    """Versions to CREATE so the ladder is four deep, chaining each new date off
    the previous slot's date on the Tue/Thu cadence. Dedupes against any existing
    non-archived version date."""
    existing = {
        d for v in versions if not v.get("archived")
        for d in [_parse_release_date(v.get("name") or "")] if d
    }
    creations: List[Tuple[str, str, str]] = []
    last: Optional[date] = active[-1][0] if active else None
    for idx in range(len(active), len(LADDER_SUFFIXES)):
        nd = _next_train_date(last) if last is not None else _next_train_date(today)
        while nd in existing:
            nd = _next_train_date(nd)
        creations.append(
            (nd.isoformat() + LADDER_SUFFIXES[idx], _prev_train_date(nd).isoformat(), nd.isoformat())
        )
        existing.add(nd)
        last = nd
    return creations


def assess(versions: List[Dict[str, Any]], today: date) -> LadderHealth:
    """Diagnose the ladder and plan its repair. `today` anchors which trains are
    'future' (the earliest active future train should be `(CURRENT)`)."""
    active = _active_ladder(versions, today)
    n = len(LADDER_SUFFIXES)

    # Date-anchored target labels: the earliest active future train is (CURRENT),
    # the next (NEXT), and so on. A rename is planned for any that's mislabeled.
    renames: List[Tuple[str, str, str]] = []
    for idx, (_d, vid, name, suf) in enumerate(active[:n]):
        target = LADDER_SUFFIXES[idx]
        if suf != target:
            renames.append((vid, name, name[: -len(suf)] + target))

    stale = [
        v for v in versions
        if CURRENT_SUFFIX in (v.get("name") or "")
        and (v.get("archived") or v.get("released"))
    ]

    issues: List[str] = []
    active_current = [t for t in active if t[3] == CURRENT_SUFFIX]
    if not active_current:
        issues.append("no active (CURRENT) version - the current train is unlabeled")
    elif len(active_current) > 1:
        issues.append(
            f"{len(active_current)} active versions are labeled (CURRENT): "
            + ", ".join(t[2] for t in active_current)
        )
    for _vid, old, new in renames:
        d_str = old.rsplit(" (", 1)[0]
        old_suf = (_ladder_suffix(old) or "").strip()
        new_suf = (_ladder_suffix(new) or "").strip()
        issues.append(f"{d_str} is {old_suf}, should be {new_suf}")

    advisories: List[str] = []
    if len(active) < n:
        advisories.append(f"ladder is {len(active)} deep, want {n} "
                          f"(will create {n - len(active)} future train(s))")
    for v in stale:
        advisories.append(f"stale (CURRENT) label on archived/released {v.get('name')!r} "
                          f"- ignored by announce, but consider stripping it")

    creations = _plan_creations(active[:n], versions, today)
    ok = not issues  # advisories don't block; a short/stale ladder still announces fine
    return LadderHealth(
        ok=ok, issues=issues, advisories=advisories, active=active,
        stale_current=stale, renames=renames, creations=creations,
    )
