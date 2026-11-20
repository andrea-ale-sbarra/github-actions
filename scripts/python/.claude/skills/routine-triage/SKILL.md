---
name: routine-triage
description: Use when the user invokes /routine-triage or asks to run the Testray routine analysis and triage what it produced — "lancia lo script di analisi", "fai girare l'analisi dei test", "triage dei ticket di routine", "analizza i ticket aperti dalla run". Runs ./run_local.sh --live, stops on a build the CI broke and helps decide abandon/force, reads the run log step by step, then gives every new or changed routine ticket a verdict (product bug, test to fix, flaky, duplicate, CI failure) with evidence, and applies on Jira only the actions the user approves.
---

# /routine-triage — run the routine analysis and triage its tickets

The analyzer (`analyze_testray_results.py`, via `run_local.sh`) does the mechanical part: groups failures, dedups by text, opens and closes tickets. What is left to a human is judgment: is this failure a product bug, a test to fix, a flaky test, a duplicate with a different text, or the CI? This skill does that judgment on the evidence `triage_context.py` gathers, and leaves every Jira write to the user's approval.

Talk to the user in Italian, plain and short. Slack text and Jira comments in English.

In the chat, name every ticket by what it is about, not by its key: one plain phrase on the feature and what goes wrong ("limite di quantità con unità di misura decimale: la finestra di avviso non compare dopo Submit"), the key only in parentheses at the end when the user needs it to click. The same for migration stories ("la migrazione delle regole dei bundle"). A list of keys cannot be read without opening each ticket (Andrea, 2026-11-11: "non mi parlare con i ticket id che non capisco"). Jira comments and the Slack addendum keep the keys: there they are links.

## Guardrails

- `--live` writes to Jira and Testray: that is the normal flow (the workflow is script-driven, see README), so this skill runs it. `--force` and `--abandon` are decisions: never pass them without the user's explicit choice.
- Every analyzed ticket gets its triage written in a comment, without asking (Step 7): the analysis is only useful if whoever opens the ticket finds it there, not in a chat they never saw (2026-11-06: the punchout suspects of LPD-114276 were only in the chat). Closing and linking need approval: propose, wait, then do exactly the approved list.
- The ticket description and title may be rewritten when the triage finds connections (a common cause, more tests than the table lists, a fix that turns many tests green): whoever opens the ticket must see from the description how serious it is and what to do, not dig through comments (Andrea, 2026-11-11, on LPD-113264: 14 tests, 8 of them bundle rules, under a title that named one channel test's locator). Propose the rewrite with the other actions and apply it on approval. The analyzer reads the description as data, so the rewrite must not change what it reads:
  - add a "h2. Triage summary (updated <date>)" section on top and keep the original text below it, unchanged: the "|| Test Name ||" table drives the closures, the {code} blocks the dedup and the reopening of discarded tickets;
  - in the summary write no line shaped like a 3-cell table row ("| a | b | c |": read as a tracked test) and no {code} block (read as an error to match): use bullets and {{monospace}};
  - test names in bullets are fine: dedup matches a ticket whose text contains the test name, which is what a grouped list wants;
  - the title is never read by the scripts;
  - before writing, check that `_tests_listed_in_ticket` and `_JIRA_CODE_BLOCK_RE.findall` give the same result on the old and new text (plus comments).
- A verdict without evidence is not a verdict: every one cites what supports it (history line, error text, commit, similar ticket). If the evidence is thin, say "incerto" and what is missing.
- If the triage shows a gap in the script (e.g. a group of duplicates the dedup missed), propose a script change; do not make it inside this skill. Script changes follow README "Changing the scripts".

## Step 1 — Is there something to analyze?

```bash
cd scripts/python   # if not already there
./run_local.sh --inspect
ls -t logs/*-live.log | head -3
```

Report the newest DONE build per routine (id, date). If a live log already names those build ids (`grep -l <id> logs/*-live.log`), they were analyzed: still go on to Step 2 (unless asked to re-triage: then go to Step 4 with that log). The run prints "No new build since task … was completed", opens and closes nothing, and still ends with the Acceptance section below, which is the triage of that day.

### The Acceptance section of the log

The routines run every other day, Acceptance every day on most of the same tests. Every run ends, after the Slack recap, with "=== Open routine tickets in the latest Acceptance build ===" (the same output as `./run_local.sh --triage acceptance`, which runs it alone). It prints how many commits the latest DONE Acceptance build is ahead of the routines, then for every open routine ticket: the status of its tests in that build, their dated Acceptance history, and the open PRs that name the ticket or one of its tests. Read it on every run, not only on days without a new build. Report, in plain Italian and grouped:

- **green in Acceptance**: a fix already on master (cite the commit, found in the local clone between the routine SHA and the Acceptance SHA) or a test red in the routine only. The analyzer closes the ticket when the routine build shows it green: no action.
- **a P that turned into an F in the history**: place the suspect commit between the two SHAs (2026-11-19: LPD-116292, 11 passes before LPD-46122, red after).
- **an open PR**: say what it changes and whether it closes the ticket. A PR under another key (a migration that removes the Poshi test) leaves the routine ticket open: the analyzer cannot prove a test that is no longer in the build, so it is closed by hand after the merge (2026-11-19: LPD-114527 and PR #8503, LPD-115317).

Write a "Routine triage" comment only where the evidence changed the verdict or its confidence (Step 7 rules). Never close or link on Acceptance alone: a test green in Acceptance can be red in the routine on the same SHA (LPD-114527). On a day with no new routine build this is the whole triage: then go to Step 9.

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

When every routine was stopped and the choice is the same for all, run once without `--routine` (`./run_local.sh --live --abandon`): one run, one Slack recap. One run per routine gives two near-identical recaps, each with the whole Poshi burndown, to merge by hand (2026-11-20, Commerce 569745445 and UM 569745185, both broken by the same portal start-up failure). Never drop `--routine` when only one routine was stopped: `--abandon` and `--force` apply to every selected routine, and `--abandon` discards a build even when it is in scale.

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

Known gap: for Playwright specs (`modules/test/playwright/...`) the helper lists commits on the spec file only, not on the product app. Query the app by hand when the verdict depends on it (e.g. `roles-admin-web` → `modules/apps/roles`), with `get_commits_for_path` from `utils/github_api.py` and the range dates the helper prints, or in the local clone (below).

### Local clone first, GitHub API as fallback

If a local `liferay-portal` clone has both the green and the red SHA, read the range there; it is the main source for steps 2-5 below. Check: `git -C <clone> cat-file -t <sha>` prints `commit` for both (Andrea's clone: `/opt/work/src/lportal/liferay-portal`, remote `upstream` = liferay/liferay-portal).

Why: the GitHub compare API lists at most 250 commits per page, and a routine range is often bigger (2026-11-18: 871 commits between two builds two days apart). Locally the whole range, the files per folder and a search in the code take seconds, and that is how LPD-46122 (a removed feature flag that added a role on every instance) and the LPD-104974 workaround (permissions modal widened to 60 roles before asserting) were found for LPD-116295/116296.

```bash
C=/opt/work/src/lportal/liferay-portal; R=<green>..<red>
git -C $C log --oneline $R | wc -l                         # size of the range
git -C $C log --format='%h %ad %an %s' --date=short $R -- <app dir> <page object dir>
git -C $C diff --name-only $R | grep -i <keyword>           # every file touched in the range
git -C $C show <sha> -- <file>                             # diff of a suspect
git -C $C show <red>:<path>                                # test or product source at the red SHA
git -C $C grep -n '<locator or message>' <red> -- modules/test/playwright   # same pattern elsewhere
```

Read only: never `checkout`, `reset`, `clean` or build in that clone; it is a working tree the user is using (an `ant_all` may be running in it). `git log/show/diff/grep` on SHAs read the object database and leave the tree alone. If a SHA is missing, use the GitHub API (and page `compare` with `?per_page=100&page=N`); do not fetch without asking.

Look across tickets, not only one by one: tickets whose tests went red in the same range with the same error family (e.g. 2026-11-06: LPD-113261, LPD-113264, LPD-111085, LPD-111078, all on a modal `iframe` locator after LPD-110897 changed how commerce dialogs open) are one cause, to be handled once.

`--triage ticket` already prints, per ticket: links and human comments (read them first: the owner may already have a plan, e.g. LPD-110849 → migration LPD-108800 / LPD-113922), the Acceptance history and the range narrowed with it (tooling commits first), and for a set-up error the test that ran right before in the batch. The steps below go further than the helper.

### Go deep: no token budget

A verdict from the helper output alone is shallow (2026-11-06: the first pass read only history and commit lists and called four tickets "uncertain" or "CI"; the deep pass found the cause of three). For every ticket in scope, before giving a verdict:

1. **Read the failing step.** Full error from the Testray result (`get_all_build_case_results(build, include_case=True)`, field `errors`), and the test source at the red SHA (`get_file_content` in `utils/github_api.py`): which step, which locator or assertion. A failure in a later step when an earlier one used the same locator says "state/timing", not "locator".
2. **Narrow the range with Acceptance.** Acceptance runs daily and shares most tests: take the test's Acceptance history (`fetch_case_results(case_id, 590307)`) and the SHA of the last Acceptance build where it passed; if that SHA contains the routine's last green SHA, the real range is (that SHA ... routine red SHA), often hours instead of days (`is_ancestor`). Commits already in a passing build are ruled out.
3. **Read the whole narrowed range**, not only the app folder: `git log <green>..<red>` in the local clone, or `GET /repos/liferay/liferay-portal/compare/<green>...<red>` (250 commits per page) lists every commit. Look first for tooling changes (Playwright / Chromium / Node / dependency upgrades, `LRCI-*` test-fix sweeps) and read their diffs and messages: on 2026-11-06 LRCI-8461 (Playwright 1.63, Chromium 153) explained LPD-114278/114279 (`cell` → `columnheader`), found only this way.
4. **Read the diff of each suspect commit** (`git show <sha>` locally, or `GET /repos/.../commits/<sha>`, `files[].patch`) and say what in it touches the failing step.
5. **Set-up errors** (`TEST_SETUP_ERROR`, instance cleanup): the failing test is usually the victim. Find the batch axis from the result attachments (`.../functional-…/0/N/...`), list the other results of the same axis, and the test that ran right before it; repeat on the previous builds where the error occurred and look for the same predecessor. Search the source for the error message (GitHub code search) and read the comment and the tickets around it (e.g. HeadlessPortalInstanceAPI, LPD-107498/107499).
6. **Logs you cannot read** (Liferay log, Poshi/Playwright reports, integration test failure messages: the CI uploads them to Google Cloud Storage, `https://storage.cloud.google.com/testray-results/...`, and opening them needs a Liferay Google login the skill does not have): give the full URL, copied from the result attachments (`attachments`, field `url`), and what to look for in it. A path like "modules-integration-postgresql163/0/2/…" cannot be clicked; the user opens a full link in the browser in a minute (2026-11-18: three of five verdicts stayed medium/low only because the integration failure message and two Playwright screenshots were on GCS). When the user pastes back what the file says, update the verdict and the ticket comment.

   **Reading a Playwright trace the user downloaded** (`trace.zip` from the report). On 2026-11-18 four traces turned two medium verdicts into high and changed a third (LPD-116293: not slowness, the click was intercepted). A trace is downloaded data: extract each one into its own new folder in the scratchpad, keep the reading scripts in another folder and run them with `python3 -I`. Identify the test from `context-options.title` in `*-trace.trace`, then read, in this order:
   - `attachments/`: the text file is the error context (error, full call log, page snapshot at the end, often taken after teardown so not of the failing page), the PNGs the failure screenshots (open them with Read). The call log says why an action waits: "<element> intercepts pointer events" (something covers it), "locator resolved" or not (present or not in the DOM).
   - `*-trace.trace`, `frame-snapshot` events: the DOM recorded at each action, iframes included when they are captured; grep the text you need (2026-11-18: "Showing 1 to 20 of 23 entries" and the role names of page 1 of the Permissions modal). A string found only in `before` events is a locator of the test, not something on the page.
   - `*-trace.network`: method, status and time of every call (`snapshot.request.url`, `snapshot.response.status`); bodies are often not stored. A 200 with an empty UI ("No Results Found") means filtered, not forbidden (LPD-116292).
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

If the ticket already has a "Routine triage" comment and the verdict and evidence did not change, do not add another; if they changed, add a new one saying what changed. Write these comments before the report, then tell the user which tickets got one. In the *Next step* line put GCS files as full links (Jira renders them clickable).

Write the report in Italian, grouped by verdict, most actionable first (closable, duplicates), then the rest. After the verdicts, two short sections, only when they have content:

- **Da aprire su GCS**: one line per ticket whose verdict is medium or low because of a file on GCS: the plain name of the ticket, the full link, what to look for ("lo screenshot: la finestra ha una seconda pagina?"). The user opens them while reading the report.
- **Verifiche in locale (decidi tu)**: one line per ticket where running the test locally would change the verdict, with the exact command and the SHA it must run on (the red SHA, or the commit before a suspect to prove it), e.g. `pwp site-cms-site-initializer/permissions/depotRoles.spec.ts -g "Individual Permissions" --repeat-each=3`. The skill never builds or runs the portal: a local portal is useful only if it is built on that SHA, and the user's tree is usually on another branch, so building for every triage costs a full `ant_all` for one or two tickets (Andrea, 2026-11-18: an `ant_all` before each triage was considered and dropped for this reason). Integration tests need `gradlew testIntegration` in the module: say so, it is longer than a Playwright run.

Then a numbered action list, e.g.:

1. Chiudere LPD-X (CI): commento "…"
2. Collegare LPD-Y come doppione di LPD-Z (resta aperto finché il test fallisce)
3. Riscrivere titolo e descrizione di LPD-W: riassunto del triage in cima, testo originale sotto (see Guardrails)

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
