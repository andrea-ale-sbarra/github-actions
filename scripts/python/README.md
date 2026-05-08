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

`.env` is gitignored and is never committed. Dependencies are managed by `uv` (see `pyproject.toml` / `uv.lock`); `run_local.sh` invokes `uv run` to execute each script.

## Running

With no flags, `./run_local.sh` runs the main analyzer (`analyze_testray_results.py`) in **DRY_RUN** mode against all selectable routines: no writes to Jira or Testray, just logs of what it would do.

### Main flags

| Flag | Effect |
|---|---|
| `--live` | Disables DRY_RUN: actually applies changes to Jira and Testray |
| `--resume` | Resumes the most recent analysis task instead of starting from the newest build |
| `--routine <name\|id>` | Restricts the analysis to a single routine (e.g. `--routine user_management`). If omitted, it runs on all of them |

### Diagnostic sub-commands

These replace the main analyzer, they don't run alongside it.

| Flag | Script run | Purpose |
|---|---|---|
| `--summary` | `print_slack_summary.py` | Re-prints the Slack recap for the latest DONE build of every routine, without re-running the analysis |
| `--diagnose` | `diagnose_closed_commerce_tickets.py` | For every ticket labeled `commerce_routine_tasks` closed today, extracts the SHA cited in the closing comment and cross-references it against the most recent 20 Commerce and UM builds. Used to distinguish legitimate closures (SHA exclusive to Commerce) from incorrect ones (SHA exclusive to UM) |
| `--inspect` | `inspect_builds.py` | Lists the most recent 20 builds of every selected routine, with `id`, `importStatus`, `dateCreated`, `dueDate`, `gitHash`. Exposes the fields the Testray UI doesn't show but that the picker uses |
| `--build <id> [<id> ...]` | `inspect_builds.py <ids>` | Inspects specific builds by id. Useful when a build appears in the Testray UI but isn't returned by the routine's API |
| `--priorities` | `discover_jira_priorities.py` | Lists the Jira priorities defined in the instance and the ones allowed on LPD/Task. Use it when updating `_PRIORITY_LADDER` in `utils/testray_helpers.py` |

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

# Inspect two specific builds by id
./run_local.sh --build 470911677 --build 470910001
```

## File layout

### Executable scripts

- **`analyze_testray_results.py`** — main pipeline. For every selected routine: fetches the builds, analyzes the testflow (`analyze_testflow`), updates Jira tasks, computes the AFT ratio of the most recent build and produces the Slack recap and the closures report. This is the script that runs by default from `run_local.sh` and that also runs from the GitHub Actions workflow.
- **`print_slack_summary.py`** — re-prints the Slack message for the latest DONE build of every routine without re-running the analysis. Useful to recover the recap after a run has already finished.
- **`diagnose_closed_commerce_tickets.py`** — diagnostic for automatic closures. For every Commerce ticket closed today, extracts the SHA cited in the closing comment (format `Closed. Not reproducible in SHA <hash>`) and compares it against the latest Commerce and UM builds, flagging closures that are likely incorrect.
- **`inspect_builds.py`** — picker diagnostic. Prints the most recent builds of every routine as the Testray API returns them (`id`, `importStatus`, `dateCreated`, `dueDate`, `gitHash`, etc.) or, if you pass it some ids (integers > 1000), inspects those specific ones.
- **`discover_jira_priorities.py`** — lists the Jira priorities available in the instance and the ones allowed by `createmeta` for LPD/Task. Re-run it when the Jira schema changes, to align `_PRIORITY_LADDER` in `utils/testray_helpers.py`.

### Support modules (`utils/`)

- **`testray_api.py`** — wrapper around the Testray API (OAuth2 authentication, fetching of routines, builds, tasks, case results).
- **`testray_helpers.py`** — high-level logic: routine selection (`select_routines`), testflow analysis (`analyze_testflow`), AFT report, Slack recap, closures report, priority ladder (`_PRIORITY_LADDER`).
- **`jira_helpers.py`** — Jira wrapper (issue create/update/close, comment and label management).

## Run modes

- **DRY_RUN (default)** — no write calls to Jira or Testray; every action is logged as "would do".
- **LIVE (`--live`)** — real changes on Jira (ticket create/update/close) and Testray (task assignment).
- **RESUME (`--resume`)** — instead of starting from the newest build, resumes the most recent already-open analysis task. Useful to resume an interrupted execution or to re-iterate on the same build.
