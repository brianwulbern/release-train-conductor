# Release train conductor

Runs a scheduled software release train end to end from a single manifest file, with a human
gate at every step that needs judgment. A train leaves twice a week. Each one gathers the tickets
targeted at it, bumps the right version lines in a charts repository, triggers the functional
tests, promotes to production, and then does the bookkeeping nobody enjoys: releasing the tracker
version, rolling the next trains forward, closing the checklist, promoting the release tags, and
telling the team in one Slack thread.

> This is a generalized, sanitized extract of an internal release tool. The three external
> services it depends on (an issue tracker, GitHub, and Slack) are replaced here by small fake
> servers in `conductor/mocks.py`, so the demo runs offline. There is no real customer data, no
> credentials, and no internal references in this repository. The manifest, the YAML editor, the
> release-ladder planner, the finalize and park commands, scope-drift detection, the tracker
> progress engine, the Slack threading and the menu's gating are the real implementation,
> extracted as-is; the menu's step runners are stand-ins.

## Why it exists

A twice-weekly release is mostly mechanical, and the mechanical parts are where it goes wrong: a
version line bumped in the wrong file, a ticket that slipped in after testing started, a tracker
ladder left with no current version so the next announcement goes out for the wrong date, a
checklist half closed. The conductor does the mechanical parts and stops at the decisions. Every
write is a dry run until asked, every tracker write is previewed first, and steps that talk to
people (Slack, the tracker) are best effort: a hiccup there never fails the release step that
triggered it.

## How it works

```
 tracker tickets ─┐
 merged PRs ──────┼─▶ manifest ─▶ pre-bump ─▶ stg bump ─▶ release tags ─▶ tests ─▶ prd bump
 CI + tags ───────┘  (one file)                                                      │
      ▲                  │                                                            ▼
      │                  └──▶ scope drift check              promote ─▶ cluster sync ─▶ finalize
      │                                                                                  │
      └────────────────── next train becomes CURRENT, ladder extended, thread updated ◀─┘
```

### The manifest is the spine (`conductor/manifest.py`)

Every stage reads and writes one JSON file per train. It holds the tickets, the repositories in
scope with their target commits and release tags, and an append-only log of every side effect
taken (PRs opened, tags cut, tests triggered, posts made). Because the log rides along when the
manifest is rebuilt, progress is recorded rather than re-inferred, and the menu can show exactly
where the train is. Reading is tolerant of schema changes: unknown keys are dropped and missing
ones defaulted, so a field added next month doesn't break last month's file. Each file also
carries a format number: a change an older version would misread bumps it, and an older
conductor refuses a newer file with a clear message instead of guessing at it.

Derived decisions live on the manifest so every stage agrees: which test suite to run, the
deploy sync order, and whether a repository's bump is a patch (every contributing ticket is a Bug)
or a minor. A few small guards came from real trains. A release tag an earlier train already
shipped is never claimed again (`shipped_by`), so a train whose new work hasn't merged yet
doesn't inherit last week's tag and report itself ready. The same holds for a hotfix: when an
off-cycle train ships a tag the next train's manifest also picked up, the next train sees it as
already shipped (`RepoEntry.ships()`), so its recap and support-article follow-ups don't count
that release a second time. And the release image's tag keeps the
base prefix its master build carries (`release_image_tag`): an nginx release `6.13.0` is
published as `1.31.3-6.13.0`, and asking for the bare version deploys nothing.

### Byte-minimal YAML edits (`conductor/yaml_edit.py`, `conductor/yaml_utils.py`)

A version bump in a shared charts repository gets reviewed by people, so the diff should contain
the version and nothing else. A full YAML round trip reformats quotes, comments and ordering. The
editor uses a YAML parser only to find which line holds the target key, then swaps just the value
on that line with a text replacement, keeping indentation, quote style and the inline comment.
Image tags keep their packaged-version prefix (an nginx `1.31.2-` or a `2.541.3-lts-jdk21-` base
that moves on its own schedule), unless the registry reports the exact published tag, which wins.

### The release ladder (`conductor/commands/finalize.py`, `conductor/ladder.py`)

The tracker holds the next four trains as versions labeled CURRENT, NEXT, OVERNEXT and
SUBSEQUENT. When a train ships, `finalize` strips its label, releases it, shifts the others up one
rung, and creates the new SUBSEQUENT on the Tuesday/Thursday cadence along with its checklist
ticket (cloned from a template, subtasks in display order). Labels are assigned by date, not by
matching the old label, so a ladder with a missing rung heals itself instead of ending up with no
current train. That was a real incident: a label-matching shift once left no CURRENT at all, and
the next announcement picked a stale version from a month earlier. The shift only happens when
this run also strips the train's own label, so re-running finalize never advances the ladder
twice. `ladder.assess` diagnoses a degraded ladder and plans the same date-anchored repair. A
version created some other way (by hand in the tracker, say) gets no checklist ticket, so the
ladder check also asks which rungs have none, in one search
(`release_steps.rungs_without_steps`), and creates them on request.

Finalize also names every other ticket still open on the version and checks whether its work
shipped. Tickets with an unmerged PR, or no merged PR at all, are offered a move to the train that
just became CURRENT, so releasing the version doesn't quietly mark unshipped work as released.

`park` is finalize's sibling for a train that missed its window: the ladder moves on, but the
missed train stays unreleased with its tickets intact, to ship later.

### Scope drift (`conductor/manifest.py`, `conductor/commands/scope.py`)

When functional tests start, the set of tickets in scope is frozen into the action log. A later
refresh compares the live tickets against that set, so a ticket someone added after testing began
is flagged instead of riding into production untested. There are two ways out: defer it (move it
to the NEXT train in the tracker) or integrate it (re-test, then explicitly re-baseline). A plain
re-run of the tests never moves the baseline on its own, so a flaky re-run can't quietly bless a
late addition.

### Tracker progress (`conductor/jira_progress.py`)

Each train has a checklist ticket with one subtask per step. When a step runs, its subtask moves
to In Progress and every subtask before it is closed, including the manual ones in between, since
reaching step N means steps 1 to N-1 are done. "Before" means the order people see in the
tracker, not key order: a step added to the template later has the highest key but is shown
first, and ordering by key would wrongly treat it as the last step. Workflow details are handled
the way the tracker requires (a close that needs a resolution gets an allowed one; a parent whose
in-progress status has a different name is matched by status category), and the full plan is
printed before any write.

### Slack threading (`conductor/slack_thread.py`)

Each train's announcement is the root of a thread; later steps post replies under it, and
milestones are also broadcast to the channel. Milestone posts are idempotent: a recorded action
guards each one, so opening the same step twice doesn't post twice, and the action is only
recorded when the post actually lands. Everything here is best effort. With no token, or no
thread root, or any Slack error, the post is skipped and the release step's result is untouched.

Finding the thread doesn't depend on one machine (`conductor/slack_roots.py`). Roots are stored
by train date and by tracker version id, so a train renamed to a later date keeps its thread. A
lookup that misses locally scans the channel for the newest announcement linking the train's
version, then reads a marker line the announcement wrote into the version's description, so a
second operator finds the same thread. The scope freeze ("closing the door") can be scheduled:
Slack holds the post and sends it at the announced cutoff, and closing early cancels it first, so
the channel gets exactly one door post.

### The menu's gating (`conductor/menu.py`)

Running the tool with no arguments opens a phase-aware menu. Every step is always listed in the
same place, either available or disabled with the reason in place of its hint, so the numbers
never shift and nothing is hidden. A train with nothing merged gets its own close-out path
instead of a wall of disabled steps, and a release tag that work has merged past offers a re-cut.
Once a train is finalized, the steps it can no longer run grey out with one reason, except the
deploy that moves the test environments onto the released images, which stays open until it has
run.

## Not in this extract

The production tool does more around testing that depends on private CI and infrastructure, so it
isn't included here: sorting a finished test run's failures into environment trouble, known issues
(tickets carrying a label) and new failures, with a ticket draft for each new one and a yes/no
prompt to file it; telling a partial rerun (one profile or a test filter) from a full run, so a
rerun credits the tests it passed instead of replacing the run; naming the package and fixed
version when a container vulnerability scan blocks an image, and, when a later dependency bump
that no ticket covers has already cleared the scan on the main branch, naming that commit and
asking for a ticket so the fix ships with the train instead of riding along untracked; and testing
a service on a pull request's image when its main branch can't produce one in time, with the run
marked as such until a run on merged code replaces it.

## Try the demo

```bash
python3 -m pip install -e ".[test]"
python3 demo.py        # one release train through the real pipeline code, offline
python3 -m pytest -q   # the test suite
```

The demo seeds a fake tracker with a four-train ladder and a checklist, then runs a train: it
builds the manifest through the real tracker client, bumps a sample `stg.yaml` (the diff shows
only the two version lines changing), announces, closes the door twice (one post), advances the
checklist (watch the out-of-order step close), catches a ticket added after testing started and
defers it to NEXT, prints the menu for that state, and runs `finalize` against the fake tracker:
rename, release, close the stuck Bug, shift the ladder, create the next train and its checklist,
promote the GitHub pre-releases, close the checklist, and print the recap. It runs finalize a
second time to show that nothing changes, then diagnoses a degraded ladder.

## Layout

```
conductor/
  manifest.py          the per-train manifest: tickets, repos, action log, derived decisions
  yaml_edit.py         locate-then-replace YAML edits (byte-minimal diffs)
  yaml_utils.py        chart value lookup, tag prefix rules, new-value composition
  repos_config.py      per-repo facts, loaded from repos.yaml (a small generic sample)
  ladder.py            ladder health: diagnose and plan a repair
  jira_client.py       tracker REST client
  jira_progress.py     checklist progress: close the prefix, start the current step
  github_client.py     the release calls finalize makes
  slack_client.py      Slack delivery (user token or webhook)
  slack_messages.py    the thread's message wording
  slack_roots.py       thread roots keyed by train
  slack_thread.py      best-effort, idempotent threaded posts
  menu.py              the menu's state detection and gating
  commands/            finalize, park, scope, recap, release steps, and the two
                       release-article checks finalize and recap ask
  config.py            placeholders and env var names (no secrets in code)
  mocks.py             fake tracker, GitHub and Slack servers
demo.py                end-to-end demo
tests/                 the extracted production tests plus tests for the showcase parts
```

## Notice

All rights reserved, no license granted. See `NOTICE.md`.
