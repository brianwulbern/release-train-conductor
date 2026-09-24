"""`conductor release steps <YYYY-MM-DD>` - clone the release-steps template ticket
for a new release train.

Each train needs a `[Release] Release Steps for Release Train for <date>` ticket
with its checklist of Technical-Task subtasks. In the UI that's "Clone REL-47502
with Subtasks", then hand-edit the date - a ~20s async job. There is no REST
clone endpoint, so this rebuilds it directly instead: create the parent Task
(summary date-swapped, [TEMPLATE] dropped) copying the template's issue type,
components, labels, priority and description, then create each subtask under it.
Every create is a synchronous POST - no async job to wait on - and subtasks are
created by parent KEY with a short retry so a momentary index lag can't drop one.

Attachments and links are intentionally NOT copied (matching the team's clone,
which only ticks "Subtasks"). Dry-run by default; --apply creates.
"""
from __future__ import annotations

import re
import sys
import time
from typing import Any, Dict, List, Optional

from .. import config, jira_client

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _new_summary(template_summary: str, date: str) -> str:
    """Swap the `YYYY-MM-DD` token for the train date and drop ` [TEMPLATE]`."""
    s = template_summary.replace(config.RELEASE_STEPS_DATE_TOKEN, date)
    s = s.replace(config.RELEASE_STEPS_TEMPLATE_MARKER, "")
    return s.strip()


def _issuetype_ref(issue_fields: Dict[str, Any]) -> Dict[str, str]:
    it = (issue_fields.get("issuetype") or {})
    return {"id": str(it.get("id"))} if it.get("id") else {}


def _priority_ref(issue_fields: Dict[str, Any]) -> Optional[Dict[str, str]]:
    pr = issue_fields.get("priority") or {}
    return {"id": str(pr.get("id"))} if pr.get("id") else None


def _parent_fields(
    tmpl: Dict[str, Any], new_summary: str, fix_version_id: Optional[str] = None,
) -> Dict[str, Any]:
    f = tmpl.get("fields") or {}
    fields: Dict[str, Any] = {
        "project": {"key": config.JIRA_PROJECT},
        "issuetype": _issuetype_ref(f),
        "summary": new_summary,
    }
    if f.get("description"):
        fields["description"] = f["description"]
    labels = f.get("labels") or []
    if labels:
        fields["labels"] = labels
    comps = [{"id": str(c["id"])} for c in (f.get("components") or []) if c.get("id")]
    if comps:
        fields["components"] = comps
    pr = _priority_ref(f)
    if pr:
        fields["priority"] = pr
    # Associate with the train's Jira version so `announce` auto-discovers this
    # as the release ticket (it scans the version's fixVersion issues).
    if fix_version_id:
        fields["fixVersions"] = [{"id": str(fix_version_id)}]
    return fields


def resolve_version_id(date: str, token: str) -> Optional[str]:
    """Return the id of the REL version for this train date (name is the date,
    with or without a ` (LABEL)` suffix), or None. Prefers a non-archived match."""
    try:
        versions = jira_client.fetch_project_versions(config.JIRA_PROJECT, token)
    except Exception:
        return None
    matches = [
        v for v in versions
        if (v.get("name") or "") == date or (v.get("name") or "").startswith(date + " ")
    ]
    matches.sort(key=lambda v: (1 if v.get("archived") else 0))
    return str(matches[0]["id"]) if matches and matches[0].get("id") else None


def _subtask_fields(sub: Dict[str, Any], parent_key: str) -> Dict[str, Any]:
    f = sub.get("fields") or {}
    fields: Dict[str, Any] = {
        "project": {"key": config.JIRA_PROJECT},
        "parent": {"key": parent_key},
        "issuetype": _issuetype_ref(f),
        "summary": f.get("summary") or "",
    }
    if f.get("description"):
        fields["description"] = f["description"]
    pr = _priority_ref(f)
    if pr:
        fields["priority"] = pr
    return fields


def _create_subtask_with_retry(fields: Dict[str, Any], token: str, tries: int = 3):
    """Create a subtask, retrying briefly: right after the parent is created its
    index entry can lag, so a parent-reference create may transiently fail."""
    last = None
    for attempt in range(tries):
        try:
            return jira_client.create_issue(fields, token)
        except Exception as e:  # noqa: BLE001 - surface after retries
            last = e
            if attempt < tries - 1:
                time.sleep(2)
    raise last


# ── Reusable building blocks (also used by `conductor finalize`) ──────────────────


def fetch_template(token: str):
    """Return (template_issue, subtasks) for the release-steps template, with the
    subtasks in the template's DISPLAY (train) order - carrying summary/issuetype/
    priority/description for faithful copies.

    Order is load-bearing: created subtasks inherit sequential keys in creation
    order, and jira_progress treats key order AS train order. The template's
    "Bump *-services …" (pre-bump) subtask was added later, so its key (REL-48284)
    sorts AFTER all the others - an `ORDER BY key` fetch pushes pre-bump to the
    BOTTOM, and then advancing the `pre` stage closes the entire train above it
    (the exact bug seen on the 2026-07-14 train). So we take order from the
    parent's `fields.subtasks` (the manual/display order) and use the JQL search
    only to enrich each subtask with its description."""
    tmpl = jira_client.fetch_issue(
        config.RELEASE_STEPS_TEMPLATE_KEY, token,
        fields=["summary", "issuetype", "components", "labels", "priority",
                "description", "subtasks"],
    )
    detailed = jira_client.search_issues(
        f"parent = {config.RELEASE_STEPS_TEMPLATE_KEY}", token,
        fields=["summary", "issuetype", "priority", "description"],
    )
    by_key = {i.get("key"): i for i in detailed}
    display_order = [s.get("key") for s in (tmpl.get("fields", {}).get("subtasks") or [])]
    subtasks = [by_key[k] for k in display_order if k in by_key]
    # Defensive: never silently drop a step - append any detailed subtask that
    # wasn't in the parent's display list (shouldn't happen).
    seen = set(display_order)
    subtasks += [i for i in detailed if i.get("key") not in seen]
    return tmpl, subtasks


def _relates_link_type(token) -> Optional[str]:
    """Name of a symmetric 'relates'-style link type for the instance (prefers
    one whose name contains 'relat'; falls back to the first available). Used to
    link a clone back to the template for traceability."""
    try:
        types = jira_client.get_issue_link_types(token)
    except Exception:
        return None
    for t in types:
        if "relat" in (t.get("name") or "").lower():
            return t.get("name")
    return types[0].get("name") if types else None


def create_ticket(date, token, tmpl, subtasks, fix_version_id=None, indent="  "):
    """Create the parent Task + its subtasks for `date`. Prints progress with
    `indent`. When `fix_version_id` is given, the parent is tagged with that
    Jira version so `announce` can auto-discover it. The parent is also assigned
    to the caller and linked back to the template ticket for traceability (both
    best-effort). Returns (parent_key, created_subtask_count, failures)."""
    new_summary = _new_summary((tmpl.get("fields") or {}).get("summary") or "", date)
    parent = jira_client.create_issue(
        _parent_fields(tmpl, new_summary, fix_version_id=fix_version_id), token)
    parent_key = parent.get("key")
    print(f"{indent}✓ created {parent_key}: {new_summary}")

    # Assign the new parent to whoever's running this (best-effort - done after
    # creation so a permission/lookup hiccup can't block the ticket itself).
    try:
        me = (jira_client.get_myself(token) or {}).get("accountId")
        if me:
            jira_client.assign_issue(parent_key, me, token)
            print(f"{indent}    ↳ assigned to you")
    except Exception as e:  # noqa: BLE001
        print(f"{indent}    · couldn't assign: {e}")

    # Link back to the template for traceability (best-effort).
    link_type = _relates_link_type(token)
    if link_type:
        try:
            jira_client.link_issues(
                parent_key, config.RELEASE_STEPS_TEMPLATE_KEY, link_type, token)
            print(f"{indent}    ↳ linked to template {config.RELEASE_STEPS_TEMPLATE_KEY} ({link_type})")
        except Exception as e:  # noqa: BLE001
            print(f"{indent}    · couldn't link to template: {e}")
    failures = 0
    created = 0
    for s in subtasks:
        summ = ((s.get("fields") or {}).get("summary") or "")[:60]
        try:
            sub = _create_subtask_with_retry(_subtask_fields(s, parent_key), token)
            print(f"{indent}    ✓ {sub.get('key')}: {summ}")
            created += 1
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"{indent}    ✗ subtask failed ({summ}): {e}")
    return parent_key, created, failures


def existing_for_date(date: str, token: str) -> List[str]:
    """Return keys of any non-template ticket already titled for this train, so a
    re-run doesn't create a duplicate."""
    try:
        # `~` is a contains match; guard the date and exclude the template itself.
        jql = (
            f'project = {config.JIRA_PROJECT} '
            f'AND summary ~ "Release Steps for Release Train for {date}"'
        )
        found = jira_client.search_issues(jql, token, fields=["summary"])
    except Exception:
        return []
    marker = config.RELEASE_STEPS_TEMPLATE_MARKER.strip()
    keys = []
    for i in found:
        summ = ((i.get("fields") or {}).get("summary") or "")
        if date in summ and marker not in summ:
            keys.append(i.get("key"))
    return [k for k in keys if k]


def run(args) -> int:
    date = (args.date or "").strip()
    if not _DATE_RE.match(date):
        sys.exit(f"error: date must be YYYY-MM-DD (got {date!r})")

    apply_changes = bool(args.apply)
    tag = "" if apply_changes else "  [dry-run]"
    template_key = config.RELEASE_STEPS_TEMPLATE_KEY
    print(f"\nClone release-steps ticket for {date}{tag}")
    print(f"  template: {template_key}")

    token = config.get_jira_token()
    config.get_jira_email()

    # 1. Fetch the template + its subtasks (with descriptions).
    try:
        tmpl, subtasks = fetch_template(token)
    except Exception as e:
        sys.exit(f"error fetching template {template_key}: {e}")
    new_summary = _new_summary((tmpl.get("fields") or {}).get("summary") or "", date)

    # 2. Dedupe: don't create a second ticket for the same train.
    existing = existing_for_date(date, token)

    # 2b. Resolve the train's Jira version so the new ticket carries a fixVersion
    # (lets `announce` auto-discover it as the release ticket).
    fix_version_id = resolve_version_id(date, token)

    # 3. Plan
    print(f"\nPlan:")
    print(f"  • create Task: '{new_summary}'")
    if fix_version_id:
        print(f"      fixVersion: {date} (version {fix_version_id}) - announce will auto-discover it")
    else:
        print(f"      fixVersion: none found for {date} - create the Jira version first "
              f"(or run finalize) for announce discovery")
    print(f"  • with {len(subtasks)} subtask(s):")
    for s in subtasks:
        print(f"      - {((s.get('fields') or {}).get('summary') or '')[:70]}")
    if existing:
        print(f"\n  ⚠ a ticket for {date} already exists: {', '.join(existing)}")
        print(f"    Re-running would create a duplicate. Pass --force to do it anyway.")
        if not getattr(args, "force", False):
            print("\n  Skipping (no --force).")
            return 0

    if not apply_changes:
        print("\nDry-run - no changes written. Re-run with --apply to create.")
        return 0

    if not getattr(args, "yes", False):
        try:
            confirm = input(f"\nCreate this ticket + {len(subtasks)} subtask(s)? (y/N): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 1
        if confirm not in ("y", "yes"):
            print("Cancelled.")
            return 0

    # 4. Create parent + subtasks
    print()
    try:
        parent_key, created_subs, failures = create_ticket(
            date, token, tmpl, subtasks, fix_version_id=fix_version_id)
    except Exception as e:
        sys.exit(f"error creating parent Task: {e}")

    print()
    print(f"  {parent_key}  →  {config.JIRA_HOST}/browse/{parent_key}")
    if failures == 0:
        print(f"✓ created {parent_key} with {created_subs} subtask(s)")
        return 0
    print(f"! {parent_key} created, but {failures} subtask(s) failed - add them manually or re-run")
    return 1
