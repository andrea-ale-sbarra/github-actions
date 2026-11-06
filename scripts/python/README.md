# Python scripts — Testray routine analysis

Set of Python scripts that analyze Testray routine results (Commerce, User Management) and keep the corresponding Jira tickets in sync. The entry point for local execution is `run_local.sh`, which loads credentials from `.env` and dispatches the right sub-command.

## Setup

1. Copy the configuration template:
   ```bash
   cp .env.example .env
   ```
2. Open `.env` and fill in the required variables:
   - `JIRA_API_USER`, `JIRA_API_TOKEN` — Jira credentials
   - `TESTRAY_CLIENT_ID`, `TESTRAY_CLIENT_SECRET` — Testray OAuth2 client
   - `EPIC_KEY` — Jira epic under which to create analysis tasks (e.g. `LPD-73638`)
   - `ROUTINE_LABEL`, `OUT_RC_LABEL` — Jira labels used to filter routine tickets
   - `FALLBACK_COMPONENT` — fallback Jira component when the Testray one doesn't exist on LPD
   - `GITHUB_TOKEN` *(optional)* — GitHub PAT for the PR review check. Without it the GitHub API is rate-limited to 60 req/h

`.env` is gitignored and is never committed. Dependencies are managed by `uv` (see `pyproject.toml` / `uv.lock`); `run_local.sh` invokes `uv run` to execute each script.

## Running

With no flags, `./run_local.sh` runs the main analyzer (`analyze_testray_results.py`) in **DRY_RUN** mode against all selectable routines: no writes to Jira or Testray, just logs of what it would do.

### Main flags

| Flag | Effect |
|---|---|
| `--live` | Disables DRY_RUN: actually applies changes to Jira and Testray |
| `--resume` | Resumes the most recent analysis task instead of starting from the newest build |
| `--routine <name\|id>` | Restricts the analysis to a single routine (e.g. `--routine user_management`). If omitted, it runs on all of them |
| `--force` | Analyzes a build even when the build check finds it out of scale (see below). Use it with `--routine` |
| `--abandon` | Discards the latest build instead of analyzing it: task marked ABANDONED, no ticket opened, open tickets closed only when every test they list PASSED in that build, PR check still run, Slack message says the build was discarded. Use it with `--routine` |

### Build check

Before creating the task of a new build, the analyzer compares the results not passed with the median of the previous 10 builds of the routine (`utils/build_health.py`). At 5x the median and at least 50 more (2.5x for Acceptance, used only by the PR check), the build is out of scale: the routine is not analyzed, in dry run too, and the run prints the numbers and the two ways on: `--routine <r> --abandon` or `--routine <r> --force`. Case behind it: run 9957 of 2026-11-03, broken by the CI (Commerce 481 not passed against ~45). Look at how the build fails before choosing: a real regression can also be out of scale (UM 2026-10-07/08). The PR check prints a warning under a ticket whose build is out of scale.

### Diagnostic sub-commands

These replace the main analyzer, they don't run alongside it.

| Flag | Script run | Purpose |
|---|---|---|
| `--summary` | `print_slack_summary.py` | Re-prints the Slack recap for the latest DONE build of every routine, without re-running the analysis |
| `--diagnose` | `diagnose_closed_commerce_tickets.py` | For every ticket labeled `commerce_routine_tasks` closed today, extracts the SHA cited in the closing comment and cross-references it against the most recent 20 Commerce and UM builds. Used to distinguish legitimate closures (SHA exclusive to Commerce) from incorrect ones (SHA exclusive to UM) |
| `--inspect` | `inspect_builds.py` | Lists the most recent 20 builds of every selected routine, with `id`, `importStatus`, `dateCreated`, `dueDate`, `gitHash`. Exposes the fields the Testray UI doesn't show but that the picker uses |
| `--build <id> [<id> ...]` | `inspect_builds.py <ids>` | Inspects specific builds by id. Useful when a build appears in the Testray UI but isn't returned by the routine's API |
| `--check-pr-failures` | `check_pr_failures.py` | For every Jira ticket tagged with the routine's `check_label` (`commerce_check_failures` / `um_check_failures`), scans the ticket's comments for the final reviewer's "View total diff: `<base>...<head>`" link, calls GitHub's compare endpoint for the file list, finds the Acceptance build whose `gitHash` matches `<head>`, and prints a Slack-ready block with the status of every test in each touched test file (PASSED / FAILED / BLOCKED / NOT RUN). Read-only |
| `--priorities` | `discover_jira_priorities.py` | Lists the Jira priorities defined in the instance and the ones allowed on LPD/Task. Use it when updating `_PRIORITY_LADDER` in `utils/testray_helpers.py` |
| `--triage ticket <LPD-X> [...]` / `--triage build <routine> [<id>]` | `triage_context.py` | Read-only evidence for triaging, used by the routine-triage skill. `ticket`: error and tests of the ticket, each test's status in the latest build and recent history, other tickets with the same test or error, commits touching the test file and its app between the ticket's last green and first red SHA. `build`: health against the previous builds and the not-passed results grouped by error, with how many look like CI/harness failures |
| `--poshi-burndown` | `poshi_burndown.py` | Scans the Poshi `.testcase` source files and reports, per file, how many `test {}` blocks are left to convert — live tests vs dead `@ignore` stubs. The count is file-based on purpose (the migration target is every test still in the tree, not just what Testray ran). Read-only. The same block is also folded into the tail of the main run's recap. Override the scanned directory with `POSHI_TESTS_DIR` or a path arg |

### Examples

```bash
# Full dry run (default)
./run_local.sh

# Real run, User Management only
./run_local.sh --live --routine user_management

# Resume the most recent in-flight analysis task, in dry run
./run_local.sh --resume

# Only the Slack message for the latest run, without re-analyzing
./run_local.sh --summary

# Check whether today's Commerce closures are legitimate
./run_local.sh --diagnose

# Inspect the most recent Commerce builds
./run_local.sh --inspect --routine commerce

# How many Poshi tests are left to convert (per file)
./run_local.sh --poshi-burndown

# Inspect two specific builds by id
./run_local.sh --build 470911677 --build 470910001

# PR review check: re-check the merged Jira tickets against the latest
# Acceptance build, without re-running the full testflow analysis
./run_local.sh --check-pr-failures
./run_local.sh --check-pr-failures --routine user_management
```

## File layout

### Executable scripts

- **`analyze_testray_results.py`** — main pipeline. For every selected routine: fetches the builds, analyzes the testflow (`analyze_testflow`), updates Jira tasks, computes the AFT ratio of the most recent build and produces the Slack recap and the closures report. This is the script that runs by default from `run_local.sh` and that also runs from the GitHub Actions workflow.
- **`print_slack_summary.py`** — re-prints the Slack message for the latest DONE build of every routine without re-running the analysis. Useful to recover the recap after a run has already finished.
- **`diagnose_closed_commerce_tickets.py`** — diagnostic for automatic closures. For every Commerce ticket closed today, extracts the SHA cited in the closing comment (format `Closed. Not reproducible in SHA <hash>`) and compares it against the latest Commerce and UM builds, flagging closures that are likely incorrect.
- **`inspect_builds.py`** — picker diagnostic. Prints the most recent builds of every routine as the Testray API returns them (`id`, `importStatus`, `dateCreated`, `dueDate`, `gitHash`, etc.) or, if you pass it some ids (integers > 1000), inspects those specific ones.
- **`check_pr_failures.py`** — standalone PR review check. Iterates every Jira ticket tagged with each routine's `check_label`, parses the final reviewer's "View total diff" link out of the comments, looks up the Acceptance build by the merge HEAD SHA, and reports the status of every test in each touched test file (PASSED/FAILED/BLOCKED/NOT RUN). The tests the diff changes (mapped from the patch lines to the test blocks of the file at the merge HEAD) are listed first with their link; the other tests of the file that did not pass are listed apart, noting whether the PR also changes code they share (imports, fixtures, `setUp`). The same routine is also invoked at the end of every main run, with the block appended to the Slack recap. **Operator step**: paste the reviewer's "Merged. Thank you. View total diff: `<base>...<head>`" comment into the Jira ticket (alongside the `*_check_failures` label) before running.
- **`triage_context.py`** — read-only evidence for the routine-triage skill (`.claude/skills/routine-triage/`): per ticket and per build, see `--triage` above. Commits are listed per path (test file, `modules/apps/<app>`) in the time window of the ticket's SHA range, because GitHub's compare stops at 250 commits and a range between two routine builds can hold 700.
- **`discover_jira_priorities.py`** — lists the Jira priorities available in the instance and the ones allowed by `createmeta` for LPD/Task. Re-run it when the Jira schema changes, to align `_PRIORITY_LADDER` in `utils/testray_helpers.py`.

### Support modules (`utils/`)

- **`testray_api.py`** — wrapper around the Testray API (OAuth2 authentication, fetching of routines, builds, tasks, case results).
- **`testray_helpers.py`** — high-level logic: routine selection (`select_routines`), testflow analysis (`analyze_testflow`), AFT report, Slack recap, closures report, priority ladder (`_PRIORITY_LADDER`).
- **`jira_helpers.py`** — Jira wrapper (issue create/update/close, comment and label management).
- **`github_api.py`** — minimal read-only wrapper around the GitHub REST API. The PR review check uses the `compare/{base}...{head}` endpoint and, to map the diff to tests, the `contents` endpoint at the merge HEAD.
- **`build_health.py`** — build check: is a build out of scale compared with the previous builds of its routine (thresholds and the builds they were checked on are in the module docstring).
- **`pr_check.py`** — PR review check logic: parses the final reviewer's "View total diff" link out of the Jira comments on a `check_label`-tagged ticket, fetches the changed files via GitHub compare, matches touched test files against case results in the matching Acceptance build, and produces the Slack-ready block consumed by both the main run and the standalone script.

## Run modes

- **DRY_RUN (default)** — no write calls to Jira or Testray; every action is logged as "would do".
- **LIVE (`--live`)** — real changes on Jira (ticket create/update/close) and Testray (task assignment).
- **RESUME (`--resume`)** — instead of starting from the newest build, resumes the most recent already-open analysis task. Useful to resume an interrupted execution or to re-iterate on the same build.

Every analyzer run (dry or live) is also saved to `logs/run-<date>-<time>-<live|dryrun>.log` (gitignored). The closure report, the skipped shard rows and the blocking subtasks are computed only during the run, so the log is the only place they survive.

## Routine triage skill

`.claude/skills/routine-triage/SKILL.md` (Claude Code, `/routine-triage`) runs the whole daily flow: checks for a new build, runs `--live`, stops on a build out of scale and helps choose `--abandon` / `--force`, reads the run log, gives each new or changed ticket a verdict (CI, flaky, duplicate, test to fix, probable bug) with evidence from `--triage`, and applies on Jira only the actions approved. The skill never changes the scripts; gaps it finds become script changes under the rules below.

## Changing the scripts

Every change to these scripts explains its context and its reasons, split between the code and the commit message. The rules the analyzer applies (what to skip, how failures are grouped, when a ticket is closed) come from cases met on real builds, and a reader without that history would undo them.

**In the code**, next to the rule, a comment says what a future reader needs in order not to undo it:

- why the rule exists, with the case that triggered it (Jira ticket, Testray build id);
- what the rule deliberately does not do, and why (e.g. "a shard row with no failing test still opens a ticket: it may be the only trace of a crash");
- known limits and open points.

**In the commit message**, the history of the decision:

- the problem seen, with tickets and builds;
- the evidence the change was checked against (which builds, what came out, e.g. "every failing shard row of builds 556026987 and 559103559 has a failing test in the same shard");
- alternatives considered and why they were dropped;
- what changes in behavior.

Don't copy the same text in both places: the comment keeps what stays true as long as the code does, the commit keeps how the decision was reached. Check a rule in dry run, or with a read-only script on real builds, before committing it, and write in the commit what came out.
