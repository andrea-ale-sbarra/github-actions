---
name: routine-triage
description: Use when the user invokes /routine-triage or asks to run the Testray routine analysis and triage what it produced — "lancia lo script di analisi", "fai girare l'analisi dei test", "triage dei ticket di routine", "analizza i ticket aperti dalla run". Runs ./run_local.sh --live, stops on a build the CI broke and helps decide abandon/force, reads the run log step by step, then gives every new or changed routine ticket a verdict (product bug, test to fix, flaky, duplicate, CI failure) with evidence, and applies on Jira only the actions the user approves.
---

# /routine-triage — run the routine analysis and triage its tickets

The analyzer (`analyze_testray_results.py`, via `run_local.sh`) does the mechanical part: groups failures, dedups by text, opens and closes tickets. What is left to a human is judgment: is this failure a product bug, a test to fix, a flaky test, a duplicate with a different text, or the CI? This skill does that judgment on the evidence `triage_context.py` gathers, and leaves every Jira write to the user's approval.

Talk to the user in Italian, plain and short. Slack text and Jira comments in English.

## Guardrails

- `--live` writes to Jira and Testray: that is the normal flow (the workflow is script-driven, see README), so this skill runs it. `--force` and `--abandon` are decisions: never pass them without the user's explicit choice.
- Every analyzed ticket gets its triage written in a comment, without asking (Step 7): the analysis is only useful if whoever opens the ticket finds it there, not in a chat they never saw (2026-11-06: the punchout suspects of LPD-114276 were only in the chat). Closing and linking need approval: propose, wait, then do exactly the approved list.
- Never edit the ticket description. The analyzer reads it as data: the "Test Name" table and the {code} errors drive dedup, the reopening of discarded tickets and the --abandon closures.
- A verdict without evidence is not a verdict: every one cites what supports it (history line, error text, commit, similar ticket). If the evidence is thin, say "incerto" and what is missing.
- If the triage shows a gap in the script (e.g. a group of duplicates the dedup missed), propose a script change; do not make it inside this skill. Script changes follow README "Changing the scripts".

## Step 1 — Is there something to analyze?

```bash
cd scripts/python   # if not already there
./run_local.sh --inspect
ls -t logs/*-live.log | head -3
```

Report the newest DONE build per routine (id, date). If a live log already names those build ids (`grep -l <id> logs/*-live.log`), they were analyzed: tell the user and stop unless asked to re-triage (then go to Step 4 with that log). Otherwise go on; the run itself also exits early with "No new build since task … was completed" when there is nothing new.

## Step 2 — Run

```bash
./run_local.sh --live
```

It takes several minutes: run it with a long timeout (or in background and wait for it). The first lines print `📝 Log: logs/run-…-live.log`: that file is the input of the next steps.

## Step 3 — Build stopped by the build check?

Look for `⛔` lines (also repeated after the recap). For each stopped routine:

```bash
./run_local.sh --triage build <routine>        # routine: commerce | user_management
```

Judge HOW the build fails:

- **CI broken**: most not-passed results under a few errors, most of them CI/harness ("results with a CI/harness error" close to "Not passed"), e.g. "The build failed prior to running the test", Poshi batch rows with the docker console tail, "Unable to run on CI". Case: run 9957 (2026-11-03), 468 of 481 Commerce results CI.
- **Real regression**: many tests but under product errors (assertions, locators, exceptions in product code); often the same count again in the next build. Case: UM 2026-10-07/08, 147-148 twice in a row.
- **Mixed**: say so, with the numbers.

Give a recommendation and ask with AskUserQuestion: abandon (recommended for CI) / force (recommended for a regression) / stop and look by hand. Then rerun only that routine:

```bash
./run_local.sh --live --routine <routine> --abandon    # or --force
```

`--abandon` creates the task as ABANDONED, opens no ticket, closes a ticket only if all its tests PASSED in that build, runs the PR check, and prints a Slack recap that says the build was discarded. Hand that recap to the user (Step 8). After `--force` continue with Step 4 on the new log.

## Step 4 — Read the log, in order

From the live log, collect:

| Line | Meaning |
|---|---|
| `✔ Created investigation task for subtask … : LPD-X` | new ticket |
| `♻ Reopened discarded ticket LPD-X` | failing again after a Discarded close: likely flaky |
| `♻ Reusing existing open ticket LPD-X` / `♻ Linking N result(s) … to LPD-X` | still failing, already tracked |
| `✔ Reported N new test(s) on LPD-X` | the ticket now covers more tests: changed |
| `Found N issues to close` + `Would close`/closure report | closed as not reproducible |
| `Skipped result …`, `⚠ COMMERCE`/area flags | skipped rows, portal-log errors with no ticket |
| `Tasks NOT completed` section | subtasks the script could not triage |

Scope of the triage: **new + changed** (Created, Reopened, Reported new tests). List the other touched tickets by key only, without re-analyzing them. Tell the user the counts before going on.

## Step 5 — Gather evidence

```bash
./run_local.sh --triage build <routine>          # once per routine: error clusters
./run_local.sh --triage ticket LPD-A LPD-B ...   # the tickets in scope
```

Known gap: for Playwright specs (`modules/test/playwright/...`) the helper lists commits on the spec file only, not on the product app. Query the app by hand when the verdict depends on it (e.g. `roles-admin-web` → `modules/apps/roles`), with `get_commits_for_path` from `utils/github_api.py` and the range dates the helper prints.

Look across tickets, not only one by one: tickets whose tests went red in the same range with the same error family (e.g. 2026-11-06: LPD-113261, LPD-113264, LPD-111085, LPD-111078, all on a modal `iframe` locator after LPD-110897 changed how commerce dialogs open) are one cause, to be handled once.

### Go deep: no token budget

A verdict from the helper output alone is shallow (2026-11-06: the first pass read only history and commit lists and called four tickets "uncertain" or "CI"; the deep pass found the cause of three). For every ticket in scope, before giving a verdict:

1. **Read the failing step.** Full error from the Testray result (`get_all_build_case_results(build, include_case=True)`, field `errors`), and the test source at the red SHA (`get_file_content` in `utils/github_api.py`): which step, which locator or assertion. A failure in a later step when an earlier one used the same locator says "state/timing", not "locator".
2. **Narrow the range with Acceptance.** Acceptance runs daily and shares most tests: take the test's Acceptance history (`fetch_case_results(case_id, 590307)`) and the SHA of the last Acceptance build where it passed; if that SHA contains the routine's last green SHA, the real range is (that SHA ... routine red SHA), often hours instead of days (`is_ancestor`). Commits already in a passing build are ruled out.
3. **Read the whole narrowed range**, not only the app folder: `GET /repos/liferay/liferay-portal/compare/<green>...<red>` lists every commit. Look first for tooling changes (Playwright / Chromium / Node / dependency upgrades, `LRCI-*` test-fix sweeps) and read their diffs and messages: on 2026-11-06 LRCI-8461 (Playwright 1.63, Chromium 153) explained LPD-114278/114279 (`cell` → `columnheader`), found only this way.
4. **Read the diff of each suspect commit** (`GET /repos/.../commits/<sha>`, `files[].patch`) and say what in it touches the failing step.
5. **Set-up errors** (`TEST_SETUP_ERROR`, instance cleanup): the failing test is usually the victim. Find the batch axis from the result attachments (`.../functional-…/0/N/...`), list the other results of the same axis, and the test that ran right before it; repeat on the previous builds where the error occurred and look for the same predecessor. Search the source for the error message (GitHub code search) and read the comment and the tickets around it (e.g. HeadlessPortalInstanceAPI, LPD-107498/107499).
6. **Logs you cannot read** (Liferay log, Poshi/Playwright reports on GCS need a Google login): name the exact file and what to look for in it as the next step.
7. **Re-check earlier verdicts** when new evidence touches them (2026-11-06: LPD-114280 had been closed as CI; it went red in the same range as the Chromium upgrade, so its comment says the closure is weaker).

## Step 6 — Verdict per ticket

One of:

- **CI / infrastructure → chiudibile**: CI/harness error (see patterns in `triage_context.py`), network errors against the CI host (`net::ERR_…` on `http://i-…:8080`), or the test already PASSED in the latest build with no product change in between.
  NOT CI: `TEST_SETUP_ERROR: Unable to delete the portal instances …`. An instance that survives deletion was left dirty by another test (or deletion itself is broken): a signal that something is wrong, to investigate, never to close as noise (Andrea, 2026-11-06; LPD-109582, 109861, 110849, 111086). Look at which test ran before it in the same batch.
- **Test fragile**: history alternates (≥2 pass/fail flips in 12), no commit on the test file or app explains it, often reopened tickets.
- **Doppione di LPD-Y**: same test, or same error family (same locator up to a small variation, same exception) as another ticket, or same error cluster in `--triage build`; name the ticket to keep (the oldest still open, or the one someone is working on).
- **Test da sistemare**: a commit in the range changed the UI/API the test relies on on purpose (renamed label, moved element, removed command); cite the commit.
- **Bug probabile**: the test checks behavior and a commit in the app changed that behavior without changing the test; cite the commit and what it touches.
- **Incerto**: say what is missing (e.g. range too wide, commit outside the app folder, no history).

Each verdict: one line of conclusion, 1-3 evidence lines, confidence (alta / media / bassa), proposed action.

## Step 7 — Write the triage in each ticket, then report and approval

For every ticket in scope add one comment (English, Jira wiki markup), always with this heading so it can be found:

```
h3. Routine triage (<YYYY-MM-DD>)
*Verdict:* <verdict> (confidence: high|medium|low)
* <evidence 1: history, error, commit sha + message + LPD key, similar ticket>
* <evidence 2>
*Next step:* <what would confirm it, or who should look>
```

If the ticket already has a "Routine triage" comment and the verdict and evidence did not change, do not add another; if they changed, add a new one saying what changed. Write these comments before the report, then tell the user which tickets got one.



Write the report in Italian, grouped by verdict, most actionable first (closable, duplicates), then the rest. Then a numbered action list, e.g.:

1. Chiudere LPD-X (CI): commento "…"
2. Collegare LPD-Y come doppione di LPD-Z (resta aperto finché il test fallisce)

Ask which to apply (the user answers with numbers, "tutte", or edits). Apply only those:

```bash
set -a && source .env && set +a && DRY_RUN=false uv run python -c "
import sys; sys.path.insert(0, 'utils')
from jira_helpers import close_issue, add_comment, _jira
# close (Discarded), keeps the parent/sub-task rules of the analyzer:
print(close_issue('LPD-X', '<sha of the latest build>', comment='Closed. Not reproducible in SHA <sha>: <reason>'))
# duplicate: link ('LPD-Y duplicates LPD-Z') and comment, but do NOT close
# while its test still fails: the next run would find it among the recently
# Discarded tickets with the same test and reopen it as flaky. The analyzer
# closes it when the test passes.
_jira().create_issue_link('Duplicate', 'LPD-Y', 'LPD-Z')
add_comment('LPD-Y', 'Same failure as LPD-Z: <why>. Tracked there. Left open on purpose: ...')
# comment:
add_comment('LPD-W', '<text>')
"
```

Keep the "Closed. Not reproducible in SHA" prefix: `diagnose_closed_commerce_tickets.py` parses it. Before the first duplicate link in a session, check the link type name exists (`[t.name for t in _jira().issue_link_types()]`). `close_issue` leaves alone tickets in progress or with active sub-tasks: report those instead of forcing them.

## Step 8 — Slack

Give the user the recap block from the log (between "Plain-text summary (copy-paste below)" and the closing dashes) unchanged: the user reads and edits it before posting. Offer, as a separate block, a short English "Triage" addendum: one line per verdict group with the ticket keys, plus the actions applied.

## Step 9 — Close

One line on what changed on Jira. If the triage showed something the script should catch by itself (a missed duplicate group, a CI error pattern opening tickets), propose it as a script change with the case that shows it.
