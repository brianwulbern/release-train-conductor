"""`conductor park` - park the current train (missed its release window) and advance the
CURRENT → NEXT → OVERNEXT → SUBSEQUENT Jira ladder to the next train, WITHOUT
releasing.

Use this when a release window (Tue/Thu, 8A-8P local) slips and the current train
won't ship on its date, but you still want to move on: the next train becomes
CURRENT while the missed one stays a distinct, UNRELEASED version with its tickets
intact - parked, not completed.

This is deliberately NOT `finalize`: finalize marks the version *released* (= done)
and would sweep-close its bug tickets. It's also cleaner than `ladder` repair,
whose date-anchored `assess` can't cope with a past-dated `(CURRENT)` it must
demote (it excludes past dates from the active ladder, so it would leave two
`(CURRENT)` labels or none). `park` demotes the named train explicitly.

What it does:
  1. Strip ` (CURRENT)` off the parked train's version name, leaving it UNRELEASED
     with its tickets. Parked, distinct, not completed.
  2. Shift the remaining future labels forward (NEXT → CURRENT, OVERNEXT → NEXT, …),
     date-anchored (reuses finalize's `_plan_label_shifts`), excluding the parked
     train so it can't grab CURRENT again.
  3. Create any missing ladder slot so CURRENT → … → SUBSEQUENT stays four deep,
     and clone the release-steps checklist for each new train
     (--no-extend-ladder / --no-release-steps to opt out).
  4. Print the follow-up `conductor manifest build <new CURRENT id>`.

Dry-run by default; --apply executes.
"""
from __future__ import annotations

import sys
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from .. import config, jira_client
from . import finalize, release_steps


def _resolve_new_current(
    versions: List[Dict[str, Any]],
    skip_id: Optional[str],
    after_date: Optional[date],
) -> Tuple[Optional[str], Optional[str]]:
    """The version that becomes (CURRENT) after the shift: the earliest active,
    non-archived/-released laddered version dated strictly after `after_date`,
    excluding the parked train. Returns (version_id, projected_current_name), or
    (None, None) when no such version exists yet (it'll be created by the ladder
    extend, and its id is known only after --apply)."""
    laddered: List[Tuple[date, str]] = []
    for v in versions:
        if v.get("archived") or v.get("released"):
            continue
        vid = str(v.get("id"))
        if skip_id and vid == str(skip_id):
            continue
        name = v.get("name") or ""
        if not finalize._ladder_suffix(name):
            continue
        d = finalize._parse_release_date(name)
        if d is None or (after_date is not None and d <= after_date):
            continue
        laddered.append((d, vid))
    laddered.sort(key=lambda t: t[0])
    if laddered:
        d, vid = laddered[0]
        return vid, d.isoformat() + finalize.CURRENT_SUFFIX
    return None, None


def run(args) -> int:
    try:
        token = config.get_jira_token()
        config.get_jira_email()
    except SystemExit:
        sys.exit("error: Jira creds needed - set CONDUCTOR_JIRA_EMAIL + CONDUCTOR_JIRA_TOKEN.")

    all_versions = jira_client.fetch_project_versions(config.JIRA_PROJECT, token)

    # 1. Identify the train to park: the active (CURRENT), or --version-id override.
    version_id = getattr(args, "version_id", None)
    if version_id:
        parked = next((v for v in all_versions if str(v.get("id")) == str(version_id)), None)
        if parked is None:
            sys.exit(f"error: no version with id {version_id} in {config.JIRA_PROJECT}.")
    else:
        parked = jira_client.find_current_version(config.JIRA_PROJECT, token)
        if parked is None:
            sys.exit(
                "error: no active '(CURRENT)' version to park. Nothing carries the "
                "(CURRENT) suffix (or it's archived/released). Pass --version-id to "
                "name one explicitly."
            )

    parked_id = str(parked.get("id"))
    parked_name = parked.get("name") or ""
    parked_date = finalize._parse_release_date(parked_name)
    project_id = parked.get("projectId")

    if not parked_name.lower().endswith(finalize.CURRENT_SUFFIX.lower()):
        sys.exit(
            f"error: version {parked_id} ('{parked_name}') has no ' (CURRENT)' suffix "
            f"to strip - it isn't the current train. Nothing to park."
        )

    apply_changes = bool(args.apply)
    tag = "" if apply_changes else "  [dry-run]"
    print(f"\nPark current train {parked_name}{tag}")
    print(f"  version_id: {parked_id}")

    # 2. Plan: strip (CURRENT) off the parked train (keep it UNRELEASED).
    parked_new_name = finalize._strip_current(parked_name)

    # 3. Plan: shift the remaining future labels forward, date-anchored, excluding
    # the parked train (by id AND by date so its past date can't grab CURRENT).
    shifts = finalize._plan_label_shifts(
        all_versions, skip_version_id=parked_id, after_date=parked_date,
    )

    # 4. Plan: create any missing ladder slot so CURRENT→…→SUBSEQUENT stays 4 deep.
    ladder_creations: List[Tuple[str, str, str]] = []
    if not getattr(args, "no_extend_ladder", False):
        ladder_creations = finalize._plan_ladder_creations(
            all_versions, shifts, parked_date,
        )

    # 5. Plan: clone the release-steps checklist for each newly-created train.
    release_steps_plan: List[Tuple[str, str, int, List[str]]] = []
    rs_tmpl = rs_subs = None
    if ladder_creations and not getattr(args, "no_release_steps", False):
        try:
            rs_tmpl, rs_subs = release_steps.fetch_template(token)
        except Exception:
            rs_tmpl = None
        if rs_tmpl:
            tmpl_summary = (rs_tmpl.get("fields") or {}).get("summary") or ""
            for _name, _start, rel in ladder_creations:
                release_steps_plan.append((
                    rel, release_steps._new_summary(tmpl_summary, rel),
                    len(rs_subs or []), release_steps.existing_for_date(rel, token),
                ))

    new_current_id, new_current_name = _resolve_new_current(
        all_versions, parked_id, parked_date,
    )

    # 6. Print the plan.
    print()
    print("Plan:")
    print(f"  • park version (strip label, stays UNRELEASED, tickets kept):")
    print(f"      '{parked_name}' → '{parked_new_name}'")
    if shifts:
        print(f"  • shift {len(shifts)} future-cycle label(s):")
        for _vid, old, new in shifts:
            print(f"      '{old}' → '{new}'")
    else:
        print(f"  · shift future-cycle labels: nothing to shift")
    if getattr(args, "no_extend_ladder", False):
        print(f"  · create missing future versions: skipped (--no-extend-ladder)")
    elif ladder_creations:
        print(f"  • create {len(ladder_creations)} missing future version(s) (Tue/Thu cadence):")
        for name, start, rel in ladder_creations:
            print(f"      '{name}'  start={start}  release={rel}")
    else:
        print(f"  · create missing future versions: ladder already complete")
    if getattr(args, "no_release_steps", False):
        print(f"  · clone release-steps tickets: skipped (--no-release-steps)")
    elif release_steps_plan:
        to_make = [p for p in release_steps_plan if not p[3]]
        if to_make:
            print(f"  • clone {len(to_make)} release-steps ticket(s) (Task + subtasks):")
        for rel, summ, n_sub, existing in release_steps_plan:
            if existing:
                print(f"      · {rel}: already exists ({', '.join(existing)}) - skip")
            else:
                print(f"      '{summ}' (+{n_sub} subtasks)")

    if new_current_name:
        where = f" (id={new_current_id})" if new_current_id else " (to be created)"
        print(f"\n  → new CURRENT train: {new_current_name}{where}")

    if not apply_changes:
        print("\nDry-run - no changes written. Re-run with --apply to execute.")
        if new_current_id:
            print(f"  after apply: conductor manifest build {new_current_id}")
        return 0

    if not args.yes:
        try:
            confirm = input("\nApply these changes? (y/N): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 1
        if confirm not in ("y", "yes"):
            print("Cancelled.")
            return 0

    # 7. Execute.
    print()
    failures = 0

    try:
        jira_client.update_version(parked_id, token, name=parked_new_name)
        print(f"  ✓ parked: '{parked_name}' → '{parked_new_name}' (unreleased)")
    except Exception as e:
        failures += 1
        print(f"  ✗ park failed: {e}")

    for vid, old_name, new_name in shifts:
        try:
            jira_client.update_version(vid, token, name=new_name)
            print(f"  ✓ shifted: '{old_name}' → '{new_name}'")
        except Exception as e:
            failures += 1
            print(f"  ✗ shift failed: '{old_name}' → '{new_name}': {e}")

    created_version_ids: Dict[str, str] = {}
    for name, start, rel in ladder_creations:
        if project_id is None:
            print(f"  ✗ create '{name}' skipped: couldn't determine projectId")
            failures += 1
            continue
        try:
            v = jira_client.create_version(
                project_id, name, token, start_date=start, release_date=rel,
            )
            if v.get("id"):
                created_version_ids[rel] = str(v["id"])
            print(f"  ✓ created version '{name}' (start={start}, release={rel})")
        except Exception as e:
            failures += 1
            print(f"  ✗ create failed '{name}': {e}")

    for rel, summ, _n_sub, existing in release_steps_plan:
        if existing:
            print(f"  · release-steps for {rel}: already exists ({', '.join(existing)}) - skip")
            continue
        if rs_tmpl is None:
            continue
        try:
            pkey, _created, sub_fails = release_steps.create_ticket(
                rel, token, rs_tmpl, rs_subs,
                fix_version_id=created_version_ids.get(rel), indent="  ",
            )
            failures += sub_fails
        except Exception as e:
            failures += 1
            print(f"  ✗ release-steps clone for {rel} failed: {e}")

    # Resolve the authoritative new CURRENT id from live Jira (an existing version
    # renamed, or one just created) so the follow-up build command is exact.
    print()
    if failures == 0:
        print("✓ train parked and ladder advanced")
        try:
            nc = jira_client.find_current_version(config.JIRA_PROJECT, token)
        except (Exception, SystemExit):
            nc = None
        if nc and nc.get("id"):
            print(f"\n  Next: build the new CURRENT manifest")
            print(f"    conductor manifest build {nc['id']}    # {nc.get('name')}")
        else:
            print("\n  Next: `conductor manifest build <new CURRENT version id>` "
                  "(couldn't resolve it automatically - check the ladder in Jira).")
        return 0
    else:
        print(f"! {failures} operation(s) failed - re-run with --apply after investigating")
        return 1
