"""`conductor scope` - inspect and act on SCOPE DRIFT: tickets added to the (CURRENT)
fix-version AFTER functional testing started (they'd ride into production untested).

`conductor test` freezes the in-scope ticket keys onto the run; a later Refresh diffs the
current tickets against that baseline. This command is the two remedies for what
that surfaces:

  status     - list the late add-ins (read-only).
  integrate  - you chose to FOLD the add-in into this train: after re-bumping stg
               and re-running tests so it's actually tested, re-baseline the tested
               scope to now, clearing the drift. The baseline never moves on its
               own (a plain re-trigger won't) - this is the explicit "yes, tested".
  defer      - you chose to WALL it OUT: move the add-in's fixVersion from this
               release to (NEXT), so it rides the next train instead.

Both write commands are dry-run by default; pass --apply. `integrate` writes only
the local manifest (records a `scope_baseline` action). `defer` writes Jira
(reassigns fixVersions), so it's confirm-gated.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional

from .. import config, jira_client, manifest as M


def _load(args):
    if args.manifest:
        p = Path(args.manifest).expanduser().resolve()
    else:
        p = M.default_manifest_path()
        if not p:
            sys.exit("no manifest in ~/.conductor/state/; run `conductor manifest build` first")
    if not p.exists():
        sys.exit(f"manifest not found: {p}")
    return p, M.read_manifest(p)


def _print_added(mf: M.Manifest) -> List:
    added = mf.tickets_added_after_test()
    baseline = mf.test_scope_keys()
    if baseline is None:
        print("  No tested-scope baseline yet - no functional-test run has recorded "
              "its scope for this release (nothing to drift from).")
        return []
    if not added:
        print(f"  ✓ No scope drift - all {len(mf.tickets)} ticket(s) were in scope when "
              f"testing started.")
        return []
    print(f"  ⚠ {len(added)} ticket(s) added to {mf.release_label} AFTER testing started "
          f"(rode in untested):")
    for t in added:
        typ = f", {t.issue_type}" if t.issue_type else ""
        print(f"      {t.key} [{t.status}{typ}]  {t.summary[:60]}")
    return added


# ─────────────────────────────────────────────────────────────────────────────
# status
# ─────────────────────────────────────────────────────────────────────────────

def _run_status(args) -> int:
    _p, mf = _load(args)
    print(f"\nScope check for release {mf.release_label}")
    added = _print_added(mf)
    if added:
        print("\n  Remedies:")
        print("    • fold it in : re-bump stg + re-run tests, then `conductor scope integrate`")
        print("    • wall it out: `conductor scope defer` → moves it to (NEXT)")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# integrate  (re-baseline the tested scope - local manifest write)
# ─────────────────────────────────────────────────────────────────────────────

def _run_integrate(args) -> int:
    manifest_path, mf = _load(args)
    print(f"\nIntegrate late add-ins into the tested scope for {mf.release_label}"
          f"{'' if args.apply else '  [dry-run]'}")
    added = mf.tickets_added_after_test()
    if mf.test_scope_keys() is None:
        print("  No tested-scope baseline yet - trigger tests first (nothing to re-baseline).")
        return 0
    if not added:
        print("  ✓ No scope drift - the tested baseline already matches current scope.")
        return 0

    print(f"  Will re-baseline the tested scope to the current {len(mf.tickets)} ticket(s), "
          f"folding in:")
    for t in added:
        print(f"      + {t.key}  {t.summary[:60]}")
    print("\n  ⚠ This marks the CURRENT scope as tested. Only do this AFTER you've "
          "re-bumped\n     stg (option 4) and re-run functional tests (option 6/7) with "
          "the add-in(s) in.")

    if not args.apply:
        print("\n  Dry-run - nothing written. Re-run with --apply to re-baseline.")
        return 0
    if not getattr(args, "yes", False):
        try:
            ans = input("\n  Confirm the add-in(s) were re-tested, and re-baseline? (y/N): ")
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.")
            return 1
        if ans.strip().lower() not in ("y", "yes"):
            print("  Cancelled.")
            return 0

    keys = [t.key for t in mf.tickets]
    folded = ", ".join(t.key for t in added)
    M.record_action(
        manifest_path, "scope_baseline",
        f"re-baselined tested scope (folded in {folded})",
        extra={"scope": keys},
    )
    print(f"  ✓ Re-baselined - {len(added)} add-in(s) now count as tested; drift cleared.")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# defer  (move late add-ins to (NEXT) - Jira write)
# ─────────────────────────────────────────────────────────────────────────────

def _find_next_version(token: str) -> Optional[Dict]:
    """The active `… (NEXT)` version (not archived/released), or None."""
    for v in jira_client.fetch_project_versions(config.JIRA_PROJECT, token):
        name = v.get("name") or ""
        if name.endswith(" (NEXT)") and not v.get("archived") and not v.get("released"):
            return v
    return None


def _run_defer(args) -> int:
    manifest_path, mf = _load(args)
    print(f"\nDefer late add-ins to (NEXT) for {mf.release_label}"
          f"{'' if args.apply else '  [dry-run]'}")

    added = mf.tickets_added_after_test()
    if mf.test_scope_keys() is None:
        print("  No tested-scope baseline yet - nothing has been flagged as a late add-in.")
        return 0

    want = set(getattr(args, "tickets", None) or [])
    if want:
        targets = [t for t in mf.tickets if t.key in want]
        unknown = want - {t.key for t in mf.tickets}
        if unknown:
            print(f"  ! not in this manifest (ignored): {', '.join(sorted(unknown))}")
        # Warn if asked to move a ticket that was actually in the tested scope.
        drift_keys = {t.key for t in added}
        not_drift = [t.key for t in targets if t.key not in drift_keys]
        if not_drift:
            print(f"  note: {', '.join(not_drift)} was in the tested scope (not a late "
                  f"add-in) - moving anyway as requested.")
    else:
        targets = added
    if not targets:
        print("  ✓ No late add-ins to defer.")
        return 0

    token = config.get_jira_token()
    config.get_jira_email()
    nxt = _find_next_version(token)
    if not nxt:
        sys.exit("  no active '(NEXT)' version found - check the ladder with `conductor ladder`.")
    next_id, next_name = str(nxt.get("id")), nxt.get("name")
    cur_id = str(mf.release_version_id)

    print(f"  Target: move fixVersion off {mf.release_label} → '{next_name}'\n")
    plans = []  # (key, new_ids, summary)
    for t in targets:
        try:
            issue = jira_client.fetch_issue(t.key, token, fields=["fixVersions", "summary"])
        except (Exception, SystemExit) as e:
            print(f"      ✗ {t.key}: couldn't read fixVersions ({e}) - skipped")
            continue
        cur_fvs = (issue.get("fields") or {}).get("fixVersions") or []
        cur_ids = [str(v.get("id")) for v in cur_fvs]
        if cur_id not in cur_ids:
            print(f"      · {t.key}: not on {mf.release_label} (fixVersions: "
                  f"{', '.join(v.get('name', '?') for v in cur_fvs) or 'none'}) - skipped")
            continue
        new_ids = [i for i in cur_ids if i != cur_id]
        if next_id not in new_ids:
            new_ids.append(next_id)
        plans.append((t.key, new_ids, t.summary))
        print(f"      {t.key}  [{', '.join(v.get('name', '?') for v in cur_fvs)}]  →  "
              f"'{next_name}'")

    if not plans:
        print("\n  Nothing to move.")
        return 0
    if not args.apply:
        print("\n  Dry-run - no Jira changes. Re-run with --apply to move.")
        return 0
    if not getattr(args, "yes", False):
        try:
            ans = input(f"\n  Move {len(plans)} ticket(s) to '{next_name}'? (y/N): ")
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.")
            return 1
        if ans.strip().lower() not in ("y", "yes"):
            print("  Cancelled.")
            return 0

    print()
    failures = 0
    for key, new_ids, _summary in plans:
        try:
            jira_client.update_issue_fix_versions(key, new_ids, token)
            print(f"  ✓ {key}: moved to '{next_name}'")
            M.record_action(manifest_path, "jira", f"{key} fixVersion → {next_name}")
        except (Exception, SystemExit) as e:
            failures += 1
            print(f"  ✗ {key}: move failed ({e})")
    print(f"\n  Refresh the manifest (option 1 / `conductor manifest build current`) so the moved "
          f"ticket(s) drop out of this release's scope.")
    return 1 if failures else 0


# ─────────────────────────────────────────────────────────────────────────────

def run(args) -> int:
    cmd = getattr(args, "scope_cmd", None)
    if cmd == "status":
        return _run_status(args)
    if cmd == "integrate":
        return _run_integrate(args)
    if cmd == "defer":
        return _run_defer(args)
    print(f"unknown scope command: {cmd}", file=sys.stderr)
    return 2
