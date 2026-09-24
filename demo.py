#!/usr/bin/env python3
"""End-to-end demo on synthetic data: no network, no tokens, no private data.

One release train goes through the real pipeline code, talking to an in-memory issue tracker,
GitHub and Slack (conductor.mocks). What to watch for:

  1. The manifest: every stage reads and writes this one file; bump kinds come from ticket
     types (all Bugs -> patch, anything else -> minor).
  2. A charts bump rewrites exactly the two version lines and nothing else: comments, quote
     style, key order and every other line survive byte for byte.
  3. Slack: the announcement is the thread root; the scope freeze posts once even when asked
     twice; the tracker's checklist advances in DISPLAY order, so a step added to the template
     later (highest key number, shown first) still closes when a later step starts.
  4. Scope drift: a ticket added after testing started is caught against the frozen test scope,
     then walled out to the NEXT train.
  5. The menu's gating for this state: available, hard-disabled, and advisory rows.
  6. Finalize: rename, release, close the stuck Bug, shift the ladder, mint the next train and
     its checklist, promote the pre-release tags, close the checklist, then print the recap.
     Run twice: the second run changes nothing (the train only advances once).
  7. Ladder health: a degraded ladder is diagnosed and its repair planned.
"""
from __future__ import annotations

import difflib
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from conductor import config, jira_client, jira_progress, ladder, manifest as M, menu
from conductor import repos_config, slack_client, slack_roots, slack_thread, yaml_edit, yaml_utils
from conductor.commands import finalize, scope
from conductor.mocks import FakeGitHub, FakeSlack, FakeTracker, install

CURRENT = "2030-04-02"


def banner(title: str) -> None:
    print(f"\n{'═' * 78}\n  {title}\n{'═' * 78}")


# ─── the synthetic world ────────────────────────────────────────────────────────

def seed(tracker: FakeTracker, gh: FakeGitHub) -> str:
    tracker.add_version("2030-03-28", released=True, release_date="2030-03-28")
    cur = tracker.add_version(f"{CURRENT} (CURRENT)", release_date=CURRENT, start_date="2030-03-28")
    tracker.add_version("2030-04-04 (NEXT)", release_date="2030-04-04", start_date=CURRENT)
    tracker.add_version("2030-04-09 (OVERNEXT)", release_date="2030-04-09", start_date="2030-04-04")
    tracker.add_version("2030-04-11 (SUBSEQUENT)", release_date="2030-04-11", start_date="2030-04-09")

    # The checklist template every train is cloned from.
    tracker.add_issue(config.RELEASE_STEPS_TEMPLATE_KEY,
                      "[Release] Release Steps for Release Train for YYYY-MM-DD [TEMPLATE]")
    for n, summary in ((47505, "Update stg.yaml for Release train testing"),
                       (47506, "Run platform tests"),
                       (47507, "Update prd.yaml for production")):
        tracker.add_issue(f"REL-{n}", summary, issue_type="Sub-task",
                          parent=config.RELEASE_STEPS_TEMPLATE_KEY)

    # This train's checklist. REL-2009 was added to the template later: it has the HIGHEST key
    # but is shown FIRST. Ordering by key number would treat it as the last step.
    parent = tracker.add_issue("REL-2000", f"[Release] Release Steps for Release Train for {CURRENT}",
                               status="Selected for Development", fix_version=cur)
    for key, summary in (("REL-2009", "Bump service VERSION.txt files"),
                         ("REL-2001", "Update stg.yaml for Release train testing"),
                         ("REL-2002", "Validate API migrations"),
                         ("REL-2003", "Run platform tests"),
                         ("REL-2004", "Run services functional tests"),
                         ("REL-2005", "Update prd.yaml for production"),
                         ("REL-2006", "PR to merge develop into production"),
                         ("REL-2007", "Release Execution")):
        tracker.add_issue(key, summary, issue_type="Sub-task", parent=parent)

    # The work riding this train: a feature (Task) and a fix (Bug stuck at Ready for Release).
    tracker.add_issue("REL-2101", "api: paginate the project list endpoint", status="Closed",
                      fix_version=cur)
    tracker.add_issue("REL-2102", "backup: retry a failed snapshot upload", issue_type="Bug",
                      status="Ready for Release", fix_version=cur)

    gh.add_prerelease("api", "5.230.0")
    gh.add_prerelease("service-backup", "5.41.0")
    return cur


# The repos this train touches. The production `manifest build` resolves these from the tickets'
# merged PRs, CI status and release tags on GitHub; here they are given directly.
REPOS = [
    M.RepoEntry(repo="api", classification="platform", target_sha="1a2b3c4d",
                target_tag="master_1a2b3c", release_tag="5.230.0",
                contributing_tickets=["REL-2101"]),
    M.RepoEntry(repo="service-backup", classification="service", target_sha="9f8e7d6c",
                target_tag="master_9f8e7d", release_tag="5.41.0", version_txt="5.41.0",
                needs_pre_bump=True, pre_bump_files=["VERSION.txt"],
                contributing_tickets=["REL-2102"]),
]


def build_manifest(version_id: str, prior: M.Manifest = None) -> Path:
    """Build (or refresh) the manifest from the tracker through the real client. The action log
    is carried forward across rebuilds, as the production reconcile does."""
    token = config.get_jira_token()
    version = jira_client.fetch_version(version_id, token)
    tickets = []
    for i in jira_client.fetch_version_issues(version_id, token):
        f = i["fields"]
        tickets.append(M.TicketEntry(
            key=i["key"], issue_id=i["id"], summary=f["summary"], status=f["status"]["name"],
            issue_type=f["issuetype"]["name"], fix_versions=[v["name"] for v in f["fixVersions"]]))
    mf = M.Manifest(
        release_version_id=version_id,
        release_label=finalize._strip_current(version["name"]),
        generated_at=M.now_iso(), tickets=tickets, repos=[M.RepoEntry(**vars(r)) for r in REPOS],
        actions=list(prior.actions) if prior else [],
    )
    return M.write_manifest(mf)


# ─── the charts bump ─────────────────────────────────────────────────────────────

STG_YAML = """\
# Staging values for the api chart. Reviewed by the charts owners.
global:
  image:
    tag: "5.229.0"          # bumped every train
  replicas: 3
configs:
  provisioning:
    envs:
      DEFAULT_BACKUP_IMAGE: 'acme/backup:5.40.1'   # pinned for staging
      DEFAULT_CI_IMAGE: acme/jenkins:2.541.3-lts-jdk21-6.4.0
"""


def bump_stg(mf: M.Manifest) -> None:
    text = STG_YAML
    for r in mf.repos:
        for loc in repos_config.get_repo_config(r.repo).get("chart_locations", []):
            if yaml_utils.stage_for_file(loc["file"]) != "stg" or loc["file"] != "deploy/api/stg.yaml":
                continue
            current = yaml_utils.get_value_at_key(text, loc["key"])
            new_value = yaml_utils.compute_new_value(
                loc["format"], current, r.target_tag, loc.get("tag_prefix", ""),
                r.version_txt or "")
            text, line_no, err = yaml_edit.edit_value_at_key(text, loc["key"], new_value)
            assert err is None, err
            print(f"  {r.repo:<15} line {line_no}: {current!r} -> {new_value!r}")
    print()
    for line in difflib.unified_diff(STG_YAML.splitlines(), text.splitlines(),
                                     "deploy/api/stg.yaml", "deploy/api/stg.yaml", lineterm=""):
        print(f"    {line}")


# ─── the run ─────────────────────────────────────────────────────────────────────

def main() -> int:
    tracker, gh, slack = FakeTracker(), FakeGitHub(), FakeSlack()
    with tempfile.TemporaryDirectory() as state, install(tracker, gh, slack), \
            mock.patch.object(config, "STATE_DIR", Path(state)):
        cur = seed(tracker, gh)

        banner("1. The manifest (one file; every stage reads and writes it)")
        path = build_manifest(cur)
        mf = M.read_manifest(path)
        print(f"  release {mf.release_label}: {len(mf.tickets)} tickets, {len(mf.repos)} repos")
        print(f"  test path:        {mf.classify_test_path()}  (a platform repo is in scope)")
        print(f"  deploy order:     {', '.join(mf.argocd_sync_order())}")
        for r in mf.repos:
            print(f"  bump kind:        {r.repo} -> {mf.judge_bump_kind(r)}")
        print(f"  services test params: {mf.services_jenkins_params()}")

        banner("2. Charts bump: only the two version lines change")
        bump_stg(mf)

        banner("3. Slack thread + the tracker checklist")
        _ok, _err, ts, channel = slack_client.post_message_ex(
            f"Release Train {CURRENT} is open for features.")
        M.record_action(path, "announce", f"announced {CURRENT}",
                        extra={"slack_channel": channel, "slack_thread_ts": ts})
        slack_roots.set_root(mf.release_label, channel, ts)
        print(f"  announcement posted (thread root ts={ts})")
        for attempt in (1, 2):
            slack_thread.close_the_door(path, quiet=True)
            doors = sum(1 for p in slack.posts if p["text"].startswith("closing the door"))
            print(f"  close the door, attempt {attempt}: {doors} scope-freeze post(s) in the thread")
        print("\n  starting the stg bump step advances the checklist:")
        jira_progress.advance(path, "stg")

        banner("4. Scope drift: a ticket added after testing started")
        M.record_action(path, "test", "platform tests triggered",
                        extra={"scope": [t.key for t in M.read_manifest(path).tickets]})
        tracker.add_issue("REL-2103", "api: new audit field (added late)", fix_version=cur)
        path = build_manifest(cur, prior=M.read_manifest(path))  # Refresh
        drift = M.read_manifest(path).tickets_added_after_test()
        print(f"  after Refresh: {[t.key for t in drift]} rode in after the test scope was frozen")
        scope.run(SimpleNamespace(scope_cmd="defer", manifest=str(path), apply=True, yes=True,
                                  tickets=None))
        path = build_manifest(cur, prior=M.read_manifest(path))  # Refresh again
        print(f"  after the next Refresh: drift = "
              f"{[t.key for t in M.read_manifest(path).tickets_added_after_test()]}")

        banner("5. The menu for this state")
        state_now = menu._detect_state()
        actions = menu._build_actions(state_now, {})
        menu._print_actions(actions, [])
        kind, msg = menu._resolve_choice("3", actions)
        print(f"\n  picking 3 -> {kind}: {msg}")
        services_only = M.Manifest(
            release_version_id="x", release_label=CURRENT, generated_at="x",
            repos=[M.RepoEntry(repo="service-backup", classification="service")])
        row = menu._build_actions(menu.PipelineState(
            manifest_path=path, manifest=services_only, scope_count=1, untagged_count=1), {})[5]
        print(f"  on a services-only train, row 6 is advisory (dimmed, still selectable):")
        print(f"    ·  6. {row.label} - {row.soft_reason}")

        banner("6. Finalize (then run it again)")
        fin_args = SimpleNamespace(manifest=str(path), apply=True, yes=True)
        finalize.run(fin_args)
        print("\n  tracker versions now:")
        for name in tracker.ladder():
            print(f"    {name}")
        print(f"  GitHub: " + ", ".join(
            f"{repo} {tag} prerelease={rel['prerelease']}" for (repo, tag), rel in gh.releases.items()))
        print("\n  second run:")
        finalize.run(fin_args)

        banner("7. Ladder health: a degraded ladder (a rung is missing)")
        versions = [
            {"id": "1", "name": "2030-04-09 (NEXT)"},
            {"id": "2", "name": "2030-04-11 (OVERNEXT)"},
        ]
        health = ladder.assess(versions, date(2030, 4, 8))
        for issue in health.issues:
            print(f"  problem: {issue}")
        for _vid, old, new in health.renames:
            print(f"  repair:  rename '{old}' -> '{new}'")
        for name, start, rel in health.creations:
            print(f"  repair:  create '{name}' (start {start})")

        print(f"\nSlack thread ({len(slack.posts)} posts):")
        for p in slack.posts:
            where = "thread" if p["thread_ts"] else "channel"
            print(f"  [{where}{', also to channel' if p['broadcast'] else ''}] "
                  f"{p['text'].splitlines()[0][:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
