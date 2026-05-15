import sys
import os

sys.path.append(os.path.dirname(__file__))

import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from urllib.parse import quote_plus

from sentence_transformers import SentenceTransformer, util
from functools import lru_cache
from types import SimpleNamespace
from jira_helpers import (
    get_issue_status_by_key,
    get_issue_type_by_key,
    create_jira_task,
    get_all_issues,
    close_issue,
    is_valid_jira_key,
    jira_issue_url,
)
from testray_api import (
    STATUS_FAILED_BLOCKED_TESTFIX,
    ACCEPTANCE_ROUTINE_ID,
    COMMERCE_ROUTINE_ID,
    USER_MANAGEMENT_ROUTINE_ID,
    get_build_tasks,
    get_build_sha,
    create_testflow,
    get_build_info,
    fetch_case_results,
    get_all_build_case_results,
    get_component_name,
    get_build_metrics,
    create_task,
    get_task_status,
    autofill_build,
    get_task_subtasks,
    get_subtask_case_results,
    assign_issue_to_case_result_batch,
    update_subtask_status,
    complete_task,
    get_case_type_id_by_name,
    get_case_count_by_type_in_build,
    get_case_info,
    get_case_type_name,
    get_routine_to_builds,
)


ANALYZED_ROUTINES = (
    {
        "id": COMMERCE_ROUTINE_ID,
        "name": "Commerce",
        "components": (
            "Product Information Management",
            "Order Management",
            "Shopping Experience",
        ),
        # No override: the Testray component → Jira component mapping is used.
        "jira_component_override": None,
        # Commerce labels keep reading from .env so existing tickets
        # tagged via the previous setup remain reachable.
        "routine_label": os.getenv("ROUTINE_LABEL", "commerce_routine_tasks"),
        "out_rc_label": os.getenv("OUT_RC_LABEL", "commerce_out_rc"),
        # Tickets flagged by the developer (cover image on the Jira board)
        # to be cross-checked against Acceptance after the PR is merged.
        "check_label": "commerce_check_failures",
    },
    {
        "id": USER_MANAGEMENT_ROUTINE_ID,
        "name": "User Management",
        "components": ("User Management",),
        # Force every ticket created during this routine's run to be tagged
        # with the "User Management" Jira component, regardless of what
        # Testray reports for the case.
        "jira_component_override": "User Management",
        "routine_label": "um_routine_tasks",
        "out_rc_label": "um_out_rc",
        "check_label": "um_check_failures",
    },
)


def select_routines():
    """
    Return the routines to process this run, filtered by the optional
    ROUTINE env var (matches routine id or a case-insensitive substring of
    the name). Unset → all routines.
    """
    selected = os.getenv("ROUTINE", "").strip()
    if not selected:
        return ANALYZED_ROUTINES

    needle = selected.lower().replace("_", " ")
    matched = tuple(
        r for r in ANALYZED_ROUTINES
        if needle == str(r["id"]) or needle in r["name"].lower().replace("_", " ")
    )
    if not matched:
        available = ", ".join(f"{r['name']} ({r['id']})" for r in ANALYZED_ROUTINES)
        raise SystemExit(
            f"ROUTINE='{selected}' did not match any routine. Available: {available}"
        )
    return matched


# Routine in scope for the case-history lookups during the current
# analyze_testflow run. Set by analyze_testflow before doing any work
# so the deep history helpers (_get_case_result_history_for_routine,
# _get_case_result_history_for_routine_not_passed) hit the right routine
# without having to thread routine_id through every intermediate function.
_CURRENT_ROUTINE_ID = COMMERCE_ROUTINE_ID


def analyze_testflow(builds, routine_id):
    """
    Slim orchestration:
      1) find latest DONE build + ensure task exists
      2) fetch testing epic + maybe autofill from previous completed task
      3) process subtasks & results (collect updates only)
      4) apply updates and attempt task completion/cleanup

    Returns (task_id, closure_summary) where:
      - task_id is None if no analysis happened
      - closure_summary is None if no stale closure was attempted, otherwise
        a dict {build_id, build_hash, attempted, closed} from
        _close_stale_routine_tasks. The caller uses it to print the per-run
        closure report.
    """
    global _CURRENT_ROUTINE_ID
    previous_routine_id = _CURRENT_ROUTINE_ID
    _CURRENT_ROUTINE_ID = routine_id
    try:
        latest_build = _pick_build_to_analyze(builds)
        if not latest_build:
            return None, None

        task_id, latest_build_id = _prepare_task(latest_build)

        if not task_id and os.getenv("DRY_RUN", "false").lower() == "true":
            task_id, latest_build_id = _find_analyzable_build_for_dry_run(builds)

        if not task_id:
            print("✘ Could not find or create a valid task, exiting.")
            return None, None

        sha = get_build_sha(latest_build_id)
        acceptance_build_id = get_acceptance_build_id_for_current_sha(sha)

        epic = _find_testing_epic()
        _maybe_autofill_from_previous(builds, latest_build)

        _process_task_subtasks(
            task_id=task_id,
            latest_build_id=latest_build_id,
            epic=epic,
            acceptance_build_id=acceptance_build_id,
        )

        closure_summary = _finalize_task_completion(
            task_id=task_id,
            latest_build_id=latest_build_id,
        )

        return task_id, closure_summary
    finally:
        _CURRENT_ROUTINE_ID = previous_routine_id


def print_slack_summary(routine_runs, pr_check_by_routine_id=None):
    """
    Print one plain-text recap covering every routine analyzed in this run.

    The output is structured as four sections, in order:

      1. Open Jira tickets per routine (with the Testray analysis task
         link and the per-component JQL URLs).
      2. Closing suggestion + "Thanks!".
      3. Closure report — tickets auto-closed as not reproducible during
         this run (only present when the run is invoked with closure
         summaries, i.e. through `analyze_testray_results.py`).
      4. Test analysis — PR review check, listing the tests Testray ran
         for each merged ticket carrying the routine's `check_label`.

    `routine_runs` is an iterable of (routine_config, task_id) or
    (routine_config, task_id, closure_summary) tuples, where
    routine_config is an entry from ANALYZED_ROUTINES. closure_summary
    may be None (no closure attempt) or a dict
    {build_id, build_hash, attempted, closed} from
    _close_stale_routine_tasks. Routines whose task_id is None (no DONE
    build, dry-run, etc.) are skipped.

    `pr_check_by_routine_id` (optional) maps routine id → list of PR
    check result dicts (see `utils.pr_check.check_pr_failures_for_routine`).
    When provided, the "test analysis" section is appended at the end.

    No Slack mrkdwn is emitted: links are rendered as plain URLs so the
    block can be copy-pasted into any chat client without rewriting.
    """
    pr_check_by_routine_id = pr_check_by_routine_id or {}

    # Local import: pr_check pulls in jira_helpers and testray_api, which is
    # the same module set testray_helpers itself imports, but we keep the
    # import lazy so callers that don't need the PR check (e.g. older
    # print_slack_summary.py invocations) don't have to satisfy
    # github_api's environment requirements.
    from pr_check import format_pr_check_block

    active_runs = [entry for entry in routine_runs if entry[1]]
    if not active_runs:
        return

    section1 = []
    for entry in active_runs:
        routine, task_id = entry[0], entry[1]
        section1.extend(_format_routine_block(routine, task_id))

    section2 = [
        "A quick suggestion: have a look to check that recent PRs aren't "
        "the cause of the failures, then kick off a Claude task to either "
        "fix a failing test, convert a Poshi test, or fix a real bug. The "
        "more we chip away at these, the cleaner our acceptance and "
        "commerce routines will be - which makes it much easier to spot "
        "real bugs and ship new releases.",
        "",
        "Thanks!",
        "",
    ]

    section3 = _format_closure_report_lines(routine_runs)

    section4 = []
    pr_blocks = []
    for entry in active_runs:
        routine = entry[0]
        pr_results = pr_check_by_routine_id.get(routine["id"])
        if pr_results:
            pr_blocks.extend(format_pr_check_block(routine, pr_results))
    if pr_blocks:
        section4 = ["Test analysis - PRs reviewed and merged:", ""]
        section4.extend(pr_blocks)

    lines = [
        "Hi all,",
        "",
        "The routine test analysis run just finished and the "
        "investigation tickets are ready for review.",
        "",
        *section1,
        *section2,
        *section3,
        *section4,
    ]

    message = "\n".join(lines)
    separator = "-" * 70
    print()
    print(separator)
    print("Plain-text summary (copy-paste below):")
    print(separator)
    print(message)
    print(separator)


def _format_routine_block(routine, task_id):
    """Build the lines that describe one routine inside the summary."""
    routine_label = routine["routine_label"]
    testray_url = f"https://testray.liferay.com/web/testray#/testflow/{task_id}"
    lines = [
        f"{routine['name']}",
        f"Testray analysis task: {testray_url}",
    ]

    components = routine.get("components") or ()
    if components:
        lines.append("Open Jira tickets by component:")
        for component in components:
            jql = (
                f'labels = "{routine_label}" '
                f'AND statusCategory != Done '
                f'AND component = "{component}" '
                f"ORDER BY priority DESC"
            )
            url = f"https://liferay.atlassian.net/issues/?jql={quote_plus(jql)}"
            lines.append(f"- {component}: {url}")
    else:
        jql = (
            f'labels = "{routine_label}" '
            f'AND statusCategory != Done '
            f"ORDER BY priority DESC"
        )
        url = f"https://liferay.atlassian.net/issues/?jql={quote_plus(jql)}"
        lines.append(f"Open Jira tickets: {url}")

    lines.append("")
    return lines


def _format_closure_report_lines(routine_runs, recent_build_window=20):
    """Return the plain-text closure-report lines (empty if nothing to report).

    For each routine that closed tickets in this run, the report
    cross-checks the build SHA cited in the closure comment against the
    most recent N builds of every other routine in ANALYZED_ROUTINES.
    If the same SHA is also present in another routine's recent builds,
    the report flags it as SHA-ambiguous - same diagnostic check as
    diagnose_closed_commerce_tickets.py, but scoped to what this run
    just closed.

    Accepts both 2-tuples (routine, task_id) and 3-tuples (..., closure_summary).
    """
    actionable = []
    for entry in routine_runs:
        if len(entry) < 3:
            continue
        routine, _tid, summary = entry[0], entry[1], entry[2]
        if summary and summary.get("closed"):
            actionable.append((routine, summary))

    if not actionable:
        return []

    sha_index_by_routine = {}
    for routine in ANALYZED_ROUTINES:
        try:
            builds = get_routine_to_builds(routine["id"])[:recent_build_window]
        except Exception as e:
            print(
                f"Could not fetch builds for {routine['name']} cross-check: {e}"
            )
            builds = []
        sha_index_by_routine[routine["id"]] = {
            b.get("gitHash"): (b["id"], b.get("name"))
            for b in builds
            if b.get("gitHash")
        }

    lines = [
        "Closure report - tickets closed by this run as 'not reproducible':",
        "",
    ]

    for routine, summary in actionable:
        sha = summary.get("build_hash")
        sha_short = (sha or "")[:12] or "<unknown>"
        build_id = summary.get("build_id")
        closed = summary.get("closed", [])
        attempted = summary.get("attempted", [])
        skipped = [k for k in attempted if k not in closed]

        lines.append(f"{routine['name']}  build {build_id} (SHA {sha_short})")

        others_with_sha = []
        for other in ANALYZED_ROUTINES:
            if other["id"] == routine["id"]:
                continue
            other_index = sha_index_by_routine.get(other["id"], {})
            if sha and sha in other_index:
                bid, bname = other_index[sha]
                others_with_sha.append((other["name"], bid, bname))

        if not sha:
            lines.append("  No build SHA recorded for this closure batch.")
        elif others_with_sha:
            for name, bid, bname in others_with_sha:
                lines.append(
                    f"  SHA also present in {name} build {bid} "
                    f"({bname}) - closures are SHA-ambiguous; "
                    "verify by closure timestamp if needed."
                )
        else:
            lines.append(
                f"  SHA only present in {routine['name']}'s recent "
                f"{recent_build_window} builds - closures are unambiguous."
            )

        lines.append(f"  Closed ({len(closed)}):")
        for key in closed:
            lines.append(f"    - {key}  {jira_issue_url(key)}")

        if skipped:
            lines.append(
                f"  Attempted but NOT closed ({len(skipped)}) - see logs above:"
            )
            for key in skipped:
                lines.append(f"    - {key}  {jira_issue_url(key)}")

        lines.append("")

    return lines


def print_closure_report(routine_runs, recent_build_window=20):
    """Backwards-compatible wrapper that prints the closure report on its own.

    The combined summary in `print_slack_summary` already includes the
    closure section, so the standard run no longer needs this function.
    Kept as a thin print() of `_format_closure_report_lines` for any
    standalone caller that still relies on it.
    """
    out = _format_closure_report_lines(routine_runs, recent_build_window)
    if not out:
        return
    separator = "-" * 70
    print()
    print(separator)
    print("\n".join(out).rstrip())
    print(separator)


def report_aft_ratio_for_latest(builds):
    """
    Compute and print AFT ratio KPI for latest DONE build vs beginning of quarter.
    (Same behavior as your previous get_automated_functional_tests_ratio flow, centralized here.)
    """
    latest_build = _get_latest_done_build(builds)
    if not latest_build:
        return

    # Beginning-of-quarter build discovery
    quarter_start_date, _, _ = _get_current_quarter_info()
    quarter_start = datetime.combine(quarter_start_date, time.min)

    best_build = None
    best_delta = None
    for b in builds:
        due_str = b.get("dueDate")
        if not due_str:
            continue
        dt = _parse_execution_date(due_str)
        if not dt or dt < quarter_start:
            continue
        delta = dt - quarter_start
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best_build = b

    if not best_build:
        print(
            "✘ Could not find a build from the beginning of the quarter to calculate test ratio."
        )
        return

    latest_build_id = latest_build["id"]
    aft_case_type_id = get_case_type_id_by_name("Automated Functional Test")
    if not aft_case_type_id:
        print("✘ Could not find case type ID for 'Automated Functional Test'.")
        return

    print("⏳ Calculating automated functional test counts...")
    start_of_quarter_count = get_case_count_by_type_in_build(
        best_build["id"], aft_case_type_id
    )
    current_count = get_case_count_by_type_in_build(latest_build_id, aft_case_type_id)
    print("✔ Counts calculated.")

    _report_poshi_tests_decrease(start_of_quarter_count, current_count)


def _get_latest_done_build(builds):
    """Return the newest build only if its import status is DONE; else None."""
    if not builds:
        return None
    latest_build = builds[0]
    if latest_build.get("importStatus", {}).get("key") != "DONE":
        print(f"✘ Latest build '{latest_build.get('name')}' is not DONE.")
        return None
    return latest_build


def _pick_build_to_analyze(builds):
    """
    In RESUME mode: scan builds newest-to-oldest and pick the first one whose
    task is still in analysis (i.e. not COMPLETE and not ABANDONED). This lets
    you finish an older routine before a fresher build steals the focus.
    Without RESUME: fall back to the latest DONE build (default behaviour).
    """
    if os.getenv("RESUME", "false").lower() != "true":
        return _get_latest_done_build(builds)

    for b in builds:
        for t in get_build_tasks(b["id"]):
            status = t.get("dueStatus", {}).get("key")
            if status and status not in ("COMPLETE", "ABANDONED"):
                print(
                    f"ℹ [RESUME] Resuming build '{b.get('name')}' (id={b['id']}) "
                    f"task {t['id']} (status={status})"
                )
                return b

    print(
        "ℹ [RESUME] No build with an in-analysis task found. "
        "Nothing to resume — drop --resume to analyze the latest DONE build normally."
    )
    return None


def get_acceptance_build_id_for_current_sha(current_sha):
    """
    Find the acceptance build ID matching the given git SHA.
    If no exact match is found, return the latest (first) build available.
    """
    builds = get_build_metrics(ACCEPTANCE_ROUTINE_ID)

    if not builds:
        print("⚠ No builds returned from Testray.")
        return None

    # 1. Try to match by git hash
    for build in builds:
        if build.get("testrayBuildGitHash") == current_sha:
            return build.get("testrayBuildId")

    # 2. No match → fall back to latest build
    latest_build = builds[0]  # Testray returns newest first
    print(
        f"⚠ No build with SHA {current_sha}. "
        f"Falling back to latest build: {latest_build.get('testrayBuildId')} "
        f"({latest_build.get('testrayBuildName')})"
    )
    return latest_build.get("testrayBuildId")


def _prepare_task(latest_build):
    """
    Ensure a task exists for latest_build and is actionable.
    Returns (task_id or None, latest_build_id).
    """
    latest_build_id = latest_build["id"]
    build_to_tasks = get_build_tasks(latest_build_id)

    if not build_to_tasks:
        if os.getenv("DRY_RUN", "false").lower() == "true":
            print(
                f"[DRY RUN] No task exists for build '{latest_build['name']}'. "
                "Skipping task/testflow creation and subsequent processing."
            )
            return None, latest_build_id
        print(
            f"[CREATE] No tasks for build '{latest_build['name']}', creating task and testflow."
        )
        task = create_task(latest_build)
        create_testflow(task["id"])
        print(f"✔ Using build {latest_build_id} and task {task['id']}")
        return task["id"], latest_build_id

    for task in build_to_tasks:
        due_status_key = task.get("dueStatus", {}).get("key")
        if due_status_key == "ABANDONED":
            print(f"Task {task['id']} has been ABANDONED.")
            return None, latest_build_id

        print(f"[USE] Using existing task {task['id']} with status {due_status_key}.")
        task_id = task["id"]

        status = get_task_status(task_id)
        if status.get("dueStatus", {}).get("key") == "COMPLETE":
            print(
                f"✔ Task {task_id} for build {latest_build_id} is now complete. No further processing required."
            )
            return None, latest_build_id

        print(f"✔ Using build {latest_build_id} and task {task_id}")
        return task_id, latest_build_id

    return None, latest_build_id


def _find_analyzable_build_for_dry_run(builds):
    """
    In dry-run, if the latest build has no task (so we can't create one), scan older
    builds for an existing active task and use that for the preview.
    Returns (task_id or None, build_id or None).
    """
    for b in builds:
        tasks = get_build_tasks(b["id"])
        for task in tasks:
            due_status = task.get("dueStatus", {}).get("key")
            if due_status in ("ABANDONED", "COMPLETE"):
                continue
            print(
                f"ℹ [DRY RUN] Falling back to build '{b.get('name')}' "
                f"(id={b['id']}) with task {task['id']} for preview."
            )
            return task["id"], b["id"]

    print(
        "ℹ [DRY RUN] No build with an active (non-complete, non-abandoned) task "
        "was found in recent history. Nothing to preview."
    )
    return None, None


def _testing_epic_jql():
    epic_key = os.getenv("EPIC_KEY", "").strip()
    if not epic_key:
        raise RuntimeError(
            "EPIC_KEY is not set. Add EPIC_KEY=LPD-XXXXX (the key of the Jira "
            "epic under which investigation tasks will be filed) to your .env file."
        )
    return f"issue = {epic_key}"


def _normalize_error(error):
    """Normalize and clean error messages for comparison and pattern matching."""
    if not error:
        return ""

    # Collapse whitespace
    error = " ".join(error.strip().split())

    # Remove timestamps, memory addresses, test durations, or dynamic values
    error = re.sub(r"\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}", "", error)  # timestamps
    error = re.sub(r"0x[0-9A-Fa-f]+", "", error)  # memory addresses
    error = re.sub(r"\d+\s*(ms|s|seconds|minutes|m)", "", error)  # durations
    error = re.sub(r'".*?"', '"..."', error)  # replace quoted strings with placeholder

    return error


def _parse_execution_date(date_str):
    date_str = date_str.strip().rstrip("Z").replace("T", " ")

    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def _find_testing_epic():
    jql = _testing_epic_jql()

    related_epics = get_all_issues(jql, fields=["summary", "key"])
    print(f"✔ Retrieved {len(related_epics)} related Epics from JIRA")

    epic = related_epics[0] if len(related_epics) == 1 else None
    print(
        f"✔ Found testing epic: {epic}"
        if epic
        else f"✘ Expected 1 related epic, but found {len(related_epics)}"
    )
    return epic


def _maybe_autofill_from_previous(builds, latest_build):
    """
    If some previous build has a COMPLETED task, autofill into the latest build.
    """

    def _first_completed_build():
        for b in builds:
            for t in get_build_tasks(b["id"]):
                if t.get("dueStatus", {}).get("key") == "COMPLETE":
                    return b
        return None

    latest_complete = _first_completed_build()
    if latest_complete:
        print("Autofill from latest analysed build...")
        autofill_build(latest_complete["id"], latest_build["id"])
        print("✔ Completed")


def _process_task_subtasks(*, task_id, latest_build_id, epic, acceptance_build_id):
    """
    Iterate subtasks, detect unique failures grouped by error, reuse or create Jira tasks.
    For each subtask, apply its case-result updates AND mark it COMPLETE immediately,
    so a crash mid-loop doesn't force a full restart on the next run.
    """
    subtasks = get_task_subtasks(task_id)

    for subtask in subtasks:
        subtask_id = subtask["id"]
        results = get_subtask_case_results(subtask_id)
        if not results:
            continue

        # Always collect any pre-existing result-level issues so they get bubbled up
        subtask_issues = set()
        existing_issue_keys = _collect_result_issue_keys(results)
        if existing_issue_keys:
            subtask_issues.update(existing_issue_keys)

        # 1) Handle already-complete subtasks (backfill issues once if needed)
        if _is_subtask_complete(subtask):
            _backfill_subtask_issues_if_needed(subtask_id, subtask, results)
            continue

        # 2) Scan current results for unique failures (skip known errors)
        unique_failures, first_result_skipped = _scan_unique_failures(
            subtask_id, results
        )

        # Group failures by normalized error so each group can map to its own issue(s)
        groups = _group_failures_by_error(unique_failures)

        subtask_updates = []
        resolved_all_groups = True
        for error_key, group in groups.items():
            updates, issues_str, resolved = _resolve_unique_failures(
                epic=epic,
                latest_build_id=latest_build_id,
                task_id=task_id,
                subtask_id=subtask_id,
                unique_failures=group,
                acceptance_build_id=acceptance_build_id,
            )
            subtask_updates.extend(updates)
            if issues_str:
                subtask_issues.add(issues_str)
            resolved_all_groups = resolved_all_groups and resolved

        # 3) Persist this subtask's result-level links IMMEDIATELY so a later
        # crash doesn't force ticket re-creation on the next run.
        if subtask_updates:
            assign_issue_to_case_result_batch(subtask_updates)

        # 4) Mark subtask COMPLETE immediately if fully handled
        no_unique_failures = len(unique_failures) == 0
        all_handled = first_result_skipped or no_unique_failures or resolved_all_groups
        if all_handled:
            issues_to_add = _join_issues(subtask_issues)
            print(
                f"✔ Marking subtask {subtask_id} as complete and associating issues: {issues_to_add}"
            )
            update_subtask_status(subtask_id, issues=issues_to_add)


def _finalize_task_completion(*, task_id, latest_build_id):
    """
    If all subtasks are COMPLETE, close stale routine tickets and complete the task.
    Individual subtask completion already happened in _process_task_subtasks.

    Returns the closure summary from _close_stale_routine_tasks (or None if
    the task is not yet complete and no closure was attempted), so the
    end-of-run report can show what was closed.
    """
    subtasks = get_task_subtasks(task_id)
    if not all(s.get("dueStatus", {}).get("key") == "COMPLETE" for s in subtasks):
        print(f"✔ Task {task_id} is not completed. Further processing required.")
        return None

    seen_issue_keys = _collect_issue_keys_from_subtasks(subtasks)
    closure_summary = _close_stale_routine_tasks(latest_build_id, seen_issue_keys)

    print(f"✔ All subtasks are complete, completing task {task_id}")
    complete_task(task_id)
    print(f"✔ Task {task_id} is now complete. No further processing required.")
    return closure_summary


def _is_subtask_complete(subtask):
    return subtask.get("dueStatus", {}).get("key") == "COMPLETE"


def _backfill_subtask_issues_if_needed(subtask_id, subtask, results):
    """
    When a subtask is COMPLETE but the aggregated 'issues' field is empty,
    aggregate from result-level 'issues' and write once.
    """
    if subtask.get("issues"):
        return
    issues = {r.get("issues") for r in results if r.get("issues")}
    if issues:
        update_subtask_status(subtask_id, issues=_join_issues(issues))


def _scan_unique_failures(subtask_id, results):
    """
    Return (unique_failures:list[dict], first_result_skipped:bool).
    We short-circuit the subtask if the first result matches a global skip.
    """
    unique_failures = []
    first_result = True
    first_result_skipped = False

    for r in results:
        error = r.get("errors") or ""

        # First result can short-circuit the subtask
        if first_result and _should_skip_result(error):
            update_subtask_status(subtask_id)
            first_result_skipped = True
            first_result = False
            continue

        first_result = False

        # Already handled or globally skippable
        if r.get("issues") or _should_skip_result(error):
            continue

        # Consider as unique failure (unhandled)
        unique_failures.append(
            {
                "error": error,
                "subtask_id": subtask_id,
                "case_id": r["r_caseToCaseResult_c_caseId"],
                "component_id": r.get("r_componentToCaseResult_c_componentId"),
                "result_id": r["id"],
            }
        )

    return unique_failures, first_result_skipped


def _group_failures_by_error(unique_failures):
    """
    Group failures by normalized error so each group can map to its own Jira issue(s).
    """
    groups = defaultdict(list)
    for f in unique_failures:
        groups[_normalize_error(f["error"])].append(f)
    return groups


def is_case_in_build(case_id, build_id, routine_id):
    """
    Check if a given case_id appears in a specific build within a routine.
    Uses fetch_case_results() to gather all history, then matches r_buildToCaseResult_c_buildId.

    Returns:
        bool: True if the case result exists for the given build_id, else False.
    """
    case_results = fetch_case_results(case_id, routine_id)

    for result in case_results:
        if result.get("testrayBuildId") == build_id:
            return True
    return False


def _resolve_unique_failures(
    *, epic, latest_build_id, task_id, subtask_id, unique_failures, acceptance_build_id
):
    """
    Try to reuse similar open Jira issues; otherwise create an investigation.
    Returns (batch_updates, issues_str|None, resolved_bool).
    """
    if not unique_failures:
        return [], None, True

    # Reuse existing open issue(s) if the error is similar (lookup by the first item in this group)
    probe = unique_failures[0]
    has_similar_issue, blocked_dict = _find_similar_open_issues(
        probe["case_id"],
        probe["error"],
    )

    acceptance_present = is_case_in_build(
        probe["case_id"], acceptance_build_id, ACCEPTANCE_ROUTINE_ID
    )

    if has_similar_issue and blocked_dict:
        issue_keys_str = blocked_dict["issues"]
        updates = [
            _blocked_update(f["result_id"], blocked_dict["dueStatus"], issue_keys_str)
            for f in unique_failures
        ]
        return updates, issue_keys_str, True

    # Otherwise, create a brand-new investigation task for this group
    print(
        f"No similar issue found → create new investigation task for subtask {subtask_id}"
    )
    issue = _create_investigation_task_for_subtask(
        acceptance_present=acceptance_present,
        subtask_unique_failures=unique_failures,
        subtask_id=subtask_id,
        latest_build_id=latest_build_id,
        epic=epic,
        task_id=task_id,
        case_history_cache={},
    )

    if not issue:
        print(
            f"⚠ Could not create investigation task for subtask {subtask_id} "
            f"(failures in group: {len(unique_failures)}, "
            f"first case_id={probe['case_id']}, error preview={probe['error'][:120]!r}). "
            f"Subtask will stay INANALYSIS."
        )
        return [], None, False

    issue_key = issue.key
    updates = [
        _blocked_update(
            f["result_id"], {"key": "BLOCKED", "name": "Blocked"}, issue_key
        )
        for f in unique_failures
    ]
    return updates, issue_key, True


def _blocked_update(result_id, due_status_dict, issues_str):
    return {"id": result_id, "dueStatus": due_status_dict, "issues": issues_str}


def _join_issues(issues_iterable):
    """
    Normalize a collection (or None) of issue strings into a single CSV or None.
    Each element may itself be a CSV; we split/trim/unique before joining.
    """
    if not issues_iterable:
        return None
    parts = {
        key.strip()
        for chunk in issues_iterable
        if chunk
        for key in str(chunk).split(",")
        if key.strip()
    }
    return ", ".join(sorted(parts)) if parts else None


def _collect_issue_keys_from_subtasks(subtasks):
    return {
        k.strip()
        for s in subtasks
        for k in str(s.get("issues", "")).split(",")
        if is_valid_jira_key(k)
    }


def _collect_result_issue_keys(results):
    """
    From subtask results, collect any issue keys present in the `issues` field.
    Skip free-text entries that don't look like Jira keys (e.g. "CI error").
    """
    return {
        k.strip()
        for r in results
        if r.get("issues")
        for k in str(r["issues"]).split(",")
        if is_valid_jira_key(k)
    }


def _close_stale_routine_tasks(latest_build_id, seen_issue_keys):
    """
    Close open routine tickets that did not appear in this run (not reproducible).

    Scoped to the current routine's labels: each routine in ANALYZED_ROUTINES
    has its own routine_label/out_rc_label so the JQL only matches tickets
    that actually belong to the routine being analyzed. Running one routine
    in isolation never touches the other routine's tickets.

    Returns a dict describing what was attempted/closed, so the caller can
    surface a closure recap at the end of the run:
        {
            "build_id": int,
            "build_hash": str | None,
            "attempted": [issue_key, ...],
            "closed":    [issue_key, ...],
        }
    """
    summary = {
        "build_id": latest_build_id,
        "build_hash": None,
        "attempted": [],
        "closed": [],
    }

    routine = _get_routine_config(_CURRENT_ROUTINE_ID)
    if not routine:
        print(
            f"⚠ No routine config for id {_CURRENT_ROUTINE_ID}; "
            "skipping stale-ticket closure."
        )
        return summary

    routine_label = routine["routine_label"]
    out_rc_label = routine["out_rc_label"]
    jql = (
        f"labels in ('{routine_label}') "
        f"AND labels not in ('test_fix', '{out_rc_label}') "
        f"AND statusCategory != Done"
    )
    open_jira_issues = get_all_issues(jql, fields=["key"])
    open_keys = {issue.key for issue in open_jira_issues}
    to_close = sorted(open_keys - seen_issue_keys)
    if to_close:
        build_hash = _get_current_build_hash(latest_build_id)
        summary["build_hash"] = build_hash
        summary["attempted"] = list(to_close)
        print(
            f"ℹ Found {len(to_close)} issues to close as they are not reproducible in this run."
        )
        for issue_key in to_close:
            if close_issue(issue_key, build_hash):
                summary["closed"].append(issue_key)
    return summary


def _get_routine_config(routine_id):
    for r in ANALYZED_ROUTINES:
        if r["id"] == routine_id:
            return r
    return None


def _sort_cases_by_duration(subtask_case_pairs, case_duration_lookup):
    def safe_duration(c_id):
        d = case_duration_lookup.get(int(c_id))
        return d if isinstance(d, (int, float)) else float("inf")

    return sorted(subtask_case_pairs, key=lambda pair: safe_duration(pair[1]))


def _format_duration(ms):
    """Convert milliseconds into human-readable duration."""
    if not isinstance(ms, (int, float)):
        return "N/A"
    minutes = int(ms // 60000)
    seconds = int((ms % 60000) // 1000)
    return f"{minutes}m {seconds}s"


def _get_current_quarter_info():
    """
    Returns:
        quarter_start (datetime.date): Start date of the current quarter.
        quarter_number (int): Quarter number (1, 2, 3, or 4).
        year (int): Current year.
    """
    today = datetime.today()
    quarter_number = (today.month - 1) // 3 + 1
    start_month = (quarter_number - 1) * 3 + 1
    quarter_start = datetime(today.year, start_month, 1).date()
    return quarter_start, quarter_number, today.year


def _build_case_rows(sorted_cases, case_duration_lookup, build_id, history_cache):
    printed_rows = []
    rca_info = None
    rca_batch = None
    rca_selector = None
    rca_compare = None
    case_type_name = None

    component_name = "Unknown"

    for _, case_id, component_id in sorted_cases:
        try:
            case_info = get_case_info(case_id)
            case_name = case_info.get("name", "N/A")
            case_type_id = case_info.get("r_caseTypeToCases_c_caseTypeId")
            case_type_name = (
                get_case_type_name(case_type_id) if case_type_id else "Unknown"
            )
            component_name = (
                get_component_name(component_id) if component_id else "Unknown"
            )
            print(
                f"  [component] case_id={case_id} component_id={component_id} "
                f"→ '{component_name}'"
            )
            raw_duration = case_duration_lookup.get(int(case_id))
            duration = raw_duration if isinstance(raw_duration, (int, float)) else None

            passing_hash = _get_last_passing_git_hash(case_id, build_id, history_cache)
            failing_hash = _get_first_failing_git_hash(case_id, build_id, history_cache)

            github_compare = (
                f"https://github.com/liferay/liferay-portal/compare/{passing_hash}...{failing_hash}"
                if passing_hash and failing_hash
                else "###"
            )

            batch_name, test_selector = _get_batch_info(case_name, case_type_name)

            if not rca_info and batch_name and test_selector:
                rca_info = _build_rca_block(batch_name, test_selector, github_compare)
                rca_batch = batch_name
                rca_selector = test_selector
                rca_compare = github_compare

            elif not rca_info:
                rca_info = f"\nCompare: {github_compare}"

            row = [case_name, _format_duration(duration), component_name]
            printed_rows.append(row)

        except Exception as e:
            print(f"[ERROR] Failed to fetch data for case_id={case_id} → {e}")

    return printed_rows, rca_info, rca_batch, rca_selector, rca_compare, component_name, case_type_name


def _get_last_passing_git_hash(case_id, build_id, history_cache):
    entire_history = history_cache.get(case_id)
    if entire_history is None:
        entire_history = _get_case_result_history_for_routine(case_id)
        history_cache[case_id] = entire_history

    result_history_for_build = _filter_case_result_history_by_build(
        entire_history, build_id
    )
    if not result_history_for_build:
        return None

    failing_hash_execution_date = result_history_for_build[0].get("executionDate")
    item = _get_last_passing_result(entire_history, failing_hash_execution_date)
    last_passing_hash = item.get("gitHash") if item else None
    return last_passing_hash


def _get_first_failing_git_hash(case_id, build_id, history_cache):
    """
    Find the first failing git hash after the last passing run for this case.
    """
    entire_history = history_cache.get(case_id)
    if entire_history is None:
        entire_history = _get_case_result_history_for_routine(case_id)
        history_cache[case_id] = entire_history

    if not entire_history:
        return None

    result_history_for_build = _filter_case_result_history_by_build(
        entire_history, build_id
    )
    if not result_history_for_build:
        return None

    failing_execution_date = result_history_for_build[0].get("executionDate")

    last_passing = _get_last_passing_result(entire_history, failing_execution_date)
    if not last_passing:
        return result_history_for_build[0].get("gitHash")

    last_pass_date = _parse_execution_date(last_passing["executionDate"])
    for item in reversed(entire_history):
        exec_date = _parse_execution_date(item.get("executionDate"))
        if not exec_date:
            continue
        if (
            exec_date > last_pass_date
            and item.get("status") in STATUS_FAILED_BLOCKED_TESTFIX
        ):
            return item.get("gitHash")

    return None


def _get_batch_info(case_name, case_type_name):
    if case_type_name == "Playwright Test":
        selector = case_name.split(" >")[0] if " >" in case_name else case_name
        return "playwright-js-tomcat101-postgresql163", selector
    elif case_type_name == "Automated Functional Test":
        return "functional-tomcat101-postgresql163", case_name
    elif case_type_name == "Modules Integration Test":
        trimmed_name = case_name.split(".")[-1]
        return (
            "modules-integration-postgresql163",
            f"\\*\\*/src/testIntegration/\\*\\*/{trimmed_name}.java",
        )
    return None, None


def _build_rca_block(batch_name, test_selector, github_compare):
    return (
        "\nParameters to run Root Cause Analysis on https://test-1-1.liferay.com/job/root-cause-analysis-tool/ :\n"
        f"PORTAL_BATCH_NAME: {batch_name}\n"
        f"PORTAL_BATCH_TEST_SELECTOR: {test_selector}\n"
        f"PORTAL_BRANCH_SHAS: {github_compare}\n"
        f"PORTAL_GITHUB_URL: https://github.com/liferay/liferay-portal/tree/master\n"
        f"PORTAL_UPSTREAM_BRANCH_NAME: master"
    )


def _should_skip_result(error):
    if "AssertionError" in error:
        return False

    skip_error_keywords = [
        "Failed prior to running test",
        "PortalLogAssertorTest#testScanXMLLog",
        "Skipped test",
        "The build failed prior to running the test",
        "test-portal-testsuite-upstream-downstream(master) timed out after",
        "Unable to run test on CI",
    ]
    return any(keyword in (error or "") for keyword in skip_error_keywords)


def _find_similar_open_issues(case_id, result_error, *, return_list=False):
    """
    Look for similar errors in history that have open Jira issues.

    Returns:
        If return_list=True: List[str]
        Else: Tuple[bool, dict or None]
    """
    seen_issues = set()
    history = _get_case_result_history_for_routine_not_passed(case_id)
    result_error_norm = _normalize_error(result_error)

    for past_result in history:
        # --- Check error similarity first ---
        past_error = past_result.get("error", "")
        if not _are_errors_similar(result_error_norm, _normalize_error(past_error)):
            continue  # irrelevant past result, skip entirely

        # --- Now check issues only for similar errors ---
        issues_str = past_result.get("issues", "")
        if not issues_str:
            continue

        open_issues = []
        for raw_key in issues_str.split(","):
            issue_key = raw_key.strip()
            if not issue_key or issue_key in seen_issues:
                continue
            if not is_valid_jira_key(issue_key):
                seen_issues.add(issue_key)
                continue

            try:
                _, _, category = get_issue_status_by_key(issue_key)
                # Compare on the status category (locale-independent) so that
                # localized status names ("Chiusa", "Risolta", …) don't
                # masquerade as still-open tickets. "done" covers Closed,
                # Resolved, Done — any terminal state in the workflow.
                if category != "done":
                    open_issues.append(issue_key)
            except Exception as e:
                print(f"Error retrieving issue {issue_key}: {e}")
            finally:
                seen_issues.add(issue_key)

        if not open_issues:
            continue

        # --- Found similar error with open issues ---
        if return_list:
            bug_issues = []
            other_issues = []

            for key in open_issues:
                issue_type = get_issue_type_by_key(key)
                if issue_type == "Bug":
                    bug_issues.append(key)
                else:
                    other_issues.append(key)

            return bug_issues or other_issues

        # default return format
        return True, {
            "dueStatus": {"key": "BLOCKED", "name": "Blocked"},
            "issues": ", ".join(open_issues),
        }

    # No similar errors with open issues found
    return [] if return_list else (False, None)


_INVESTIGATE_SUMMARY_RE = re.compile(
    r"^(?:\[[^\]]+\]\s+)?Investigate\s+(.*?)(?:\.{3})?\s*$"
)


def _find_existing_open_ticket_for_error(epic, error_text):
    """
    Pre-creation dedup: search Jira for an already-open routine task whose
    summary describes the same error semantically. Catches the case where the
    case-result history lookup misses a ticket (e.g. issues field never
    persisted on the previous result, or autofill didn't run).
    Returns the existing issue key, or None.
    """
    routine = _get_routine_config(_CURRENT_ROUTINE_ID)
    routine_label = (
        routine["routine_label"] if routine
        else os.getenv("ROUTINE_LABEL", "routine_tasks")
    )

    jql_parts = [
        f'labels = "{routine_label}"',
        'statusCategory != Done',
    ]
    if epic is not None:
        jql_parts.append(f'parent = "{epic.key}"')
    jql = " AND ".join(jql_parts)

    try:
        candidates = get_all_issues(jql, fields=["summary"])
    except Exception as e:
        print(f"⚠ JQL pre-creation dedup lookup failed ({e}); proceeding to create.")
        return None

    if not candidates:
        return None

    new_marker = (error_text or "")[:80]
    new_norm = _normalize_error(error_text or "")

    for c in candidates:
        summary = (getattr(c.fields, "summary", None) or "").strip()
        if new_marker and new_marker in summary:
            return c.key

        m = _INVESTIGATE_SUMMARY_RE.match(summary)
        if not m:
            continue
        candidate_error = m.group(1).strip()
        if not candidate_error:
            continue
        if _are_errors_similar(new_norm, _normalize_error(candidate_error)):
            return c.key

    return None


def _report_poshi_tests_decrease(start_of_quarter_count, current_count):
    if start_of_quarter_count == 0:
        print("Cannot calculate decrease percentage (division by zero).")
        return

    items_less = start_of_quarter_count - current_count
    decrease_percent = (items_less / start_of_quarter_count) * 100

    if decrease_percent < 10.0:
        print(
            f"The total number of POSHI tests has gone down by {decrease_percent:.2f}% "
            f"compared to what it was at the beginning of the quarter. "
            f"We're targeting a 10% decrease, so there's still work to do."
        )
    else:
        print(
            f"The total number of POSHI tests has gone down by {decrease_percent:.2f}% "
            f"compared to what it was at the beginning of the quarter. "
            f"KPI of 10% accomplished, but keep pushing!"
        )


@lru_cache()
def _load_model():
    return SentenceTransformer("all-MiniLM-L6-v2")


def _are_errors_similar(current_norm, history_norm, threshold=0.8):
    """
    Compare two error messages semantically using sentence embeddings.
    """
    model = _load_model()
    emb_a = model.encode(current_norm, convert_to_tensor=True)
    emb_b = model.encode(history_norm, convert_to_tensor=True)
    similarity = util.pytorch_cos_sim(emb_a, emb_b).item()
    return similarity >= threshold


def _group_errors_by_type(unique_tasks):
    error_to_cases = defaultdict(list)
    for item in unique_tasks:
        error_to_cases[item["error"]].append(
            (item["subtask_id"], item["case_id"], item["component_id"])
        )
    return error_to_cases


def _build_case_duration_lookup(unique_tasks, build_id):
    raw_build_results = get_all_build_case_results(build_id)
    interested_case_ids = {
        int(item["case_id"]) for item in unique_tasks if item.get("case_id")
    }

    return {
        int(item["r_caseToCaseResult_c_caseId"]): item.get("duration")
        for item in raw_build_results
        if item.get("r_caseToCaseResult_c_caseId")
        and int(item["r_caseToCaseResult_c_caseId"]) in interested_case_ids
    }


def _get_case_result_history_for_routine(case_id):
    items = fetch_case_results(case_id, _CURRENT_ROUTINE_ID)
    return _sort_by_execution_date_desc(items)


def _get_case_result_history_for_routine_not_passed(case_id):
    items = fetch_case_results(
        case_id, _CURRENT_ROUTINE_ID, status=STATUS_FAILED_BLOCKED_TESTFIX
    )
    return _sort_by_execution_date_desc(items)


_PRIORITY_LADDER = [
    (13, "High"),
    (8, "Medium"),
    (5, "Normal"),
    (3, "Low"),
]


def _days_since_last_pass(case_id, history_cache):
    """
    Returns days since the most recent PASSED execution for this case.
    Returns None if history is empty or dates can't be parsed,
    so the caller can apply a sensible default.
    Returns 9999 if the case has a history but never passed,
    so the caller can treat it as chronically failing.
    """
    history = history_cache.get(case_id)
    if history is None:
        history = _get_case_result_history_for_routine(case_id)
        history_cache[case_id] = history

    if not history:
        return None

    for item in history:
        if item.get("status") == "PASSED":
            dt = _parse_execution_date(item.get("executionDate", ""))
            if dt:
                return (datetime.now() - dt).days

    return 9999


def _priority_for_days(days):
    """Map days-since-last-pass to Jira priority name. No history → Low."""
    if days is None:
        return "Low"
    for threshold, name in _PRIORITY_LADDER:
        if days >= threshold:
            return name
    return "Low"


def _sort_by_execution_date_desc(items):
    def get_sort_key(item):
        date_str = item.get("executionDate", "")
        parsed_date = _parse_execution_date(date_str)
        return parsed_date or datetime.min

    return sorted(items, key=get_sort_key, reverse=True)


def _get_last_passing_result(entire_history, max_execution_date):
    status_passed = "PASSED"

    if isinstance(max_execution_date, str):
        max_execution_date = _parse_execution_date(max_execution_date)
        if not max_execution_date:
            print("❌ Invalid max_due_date format")
            return None

    last_passing = None
    last_date = None

    for item in entire_history:
        if item.get("status") != status_passed:
            continue

        execution_date_str = item.get("executionDate")
        if not execution_date_str:
            continue

        execution_date = _parse_execution_date(execution_date_str)
        if not execution_date or execution_date >= max_execution_date:
            continue

        if last_date is None or execution_date > last_date:
            last_passing = item
            last_date = execution_date

    return last_passing


def _filter_case_result_history_by_build(history, build_id):
    """Filter case result history by build ID."""
    return [item for item in history if item.get("testrayBuildId") == build_id]


def _get_current_build_hash(build_id):
    build = get_build_info(build_id)
    git_hash = build.get("gitHash")
    return git_hash

def _build_investigation_intro(
        task_id, subtask_id, acceptance_present, test_type
):
    out_rc_label = os.getenv("OUT_RC_LABEL", "out_rc")
    lines = []

    # ---- INTRO ----
    lines.extend(
        [
            "h2. 🔍 Investigation Purpose & Instructions",
            "",
            "*Purpose of this issue*",
            "",
            "The purpose of this ticket is to investigate one or more test failures detected in the "
            "Testray test routine.",
            "",
            "This issue aggregates *unique failures* for the related Testray subtask and defines the "
            "investigation workflow to determine:",
            "* whether the failure is caused by a real product *Bug*, or",
            "* whether it requires a *test fix* (including flakiness or test-layer mismatch).",
            "",
        ]
    )

    if acceptance_present:
        lines.extend(
            [
                "*⚠️ Acceptance Failure*",
                "",
                "This failure is also triggered as part of the *EE Development Acceptance (master)* routine.",
                "Issues with the label *acceptance_failure* have *higher priority* and must be investigated first.",
                "",
            ]
        )

    # ---- INVESTIGATION WORKFLOW ----
    lines.extend(
        [
            "h3. 🧭 Investigation Workflow (Mandatory)",
            "",
            "*Step 1: Run the failing test(s) locally*",
            "",
            "* If the test(s) *PASS locally*: ",
            "** The failure is likely caused by *flakiness*.",
            "** Check the *Testray history* for this case in the test routine.",
            "** Determine whether this was a one-time fluke or a strong flakiness case that needs to be addressed.",
            "",
            "* If the test(s) *FAIL locally*: continue with Step 2.",
            "",
            "*Step 2: Determine whether this is a test fix due to an intended behavior/locator change*",
            "",
            "* Using *RCA* information or manual research, try to identify the commit that introduced the change.",
            "* In most cases, determining whether the change is intended requires consulting the "
            "*developer or team* who made the change.",
            "",
            "*As a rule of thumb:*",
            "* Very small and obvious changes that require test fix (for example, copy/text updates) can be handled directly.",
            "* Changes affecting shared behavior (for example, locators or APIs used by multiple tests) "
            "should be confirmed with the responsible developer or team.",
            "",
            "* If this is confirmed to be a *test fix*, add the label *test_fix* to this issue.",
            "",
        ]
    )

    # ---- TEST TYPE HANDLING (AFTER STEP 2) ----
    if test_type == "poshi":
        lines.extend(
            [
                "h3. 🧪 Poshi (Automated Functional Tests)",
                "",
                "* Poshi tests must be migrated to the *Integration* or *Playwright* layer.",
                "* You may:",
                "** work on this ticket directly if the migration is trivial or owned by our team, or",
                "** move the issue back to *Open* if higher-priority work exists.",
                "",
            ]
        )

    elif test_type == "playwright":
        lines.extend(
            [
                "h3. 🎭 Playwright Tests",
                "",
                "* Fix the issue directly in the *Playwright* layer if owned by our team.",
                "* If the change was introduced by an external team:",
                f"** add the label *{out_rc_label}* to this ticket (mandatory) ,",
                "** reassign the component accordingly,",
                "** set assignee to *Automatic*.",
                "* Leave a comment describing:",
                "** failing step(s),",
                "** observed vs expected behavior,",
                "** relevant investigation details.",
                "",
            ]
        )

    elif test_type in ("integration", "unit"):
        lines.extend(
            [
                "h3. 🔧 Integration/Unit Tests",
                "",
                "* If you are confident the failure is only a test fix (e.g. JSON copy or expected data change), keep it as a *Test Fix*.",
                "* If the change is intended and you know what introduced it, you may handle the fix yourself.",
                "* If the failure was introduced by another team:",
                f"** always add the label *{out_rc_label}* to this ticket (mandatory),",
                "* If you are blocked, reprioritized, or the root cause is unclear:",
                "** leave investigation and ownership to the team or developer who introduced the change,",
                "** reassign the component to the LPD corresponding to the commit that introduced the change, if handing over to another team,",
                "** set assignee to *Automatic* when handing it over.",
                "* If it is unclear whether this is a test fix or a product issue:",
                "** convert the task to a *Regression Bug* and follow the Bug Finalization steps.",
                "* Always leave a detailed comment describing:",
                "** the failing test(s),",
                "** manual reproduction steps, if a minimal workflow is known,",
                "** a link to the root cause commit or commit range, if identified.",
                "",
            ]
        )
    # ---- STEP 3: BUG ----
    lines.extend(
        [
            "*Step 3: Real product issue (Bug)*",
            "",
            "* If the test(s) *FAIL locally* and clearly expose a product issue:",
            "** Create a *Regression Bug*.",
            "** Use RCA or manual research to identify the causing commit.",
            "** Add detailed information to the Bug, including:",
            "*** which test(s) fail,",
            "*** what behavior is incorrect,",
            "*** optional reproduction steps if a minimal workflow is known.",
            "** Address the Bug with the team that caused the issue.",
            "",
        ]
    )

    # ---- BUG WRAP-UP ----
    lines.extend(
        [
            "h3. 🐞 Bug Finalization",
            "",
            "*Once a Bug is created:*",
            f"** If bug introduced by an external team: add the label *{out_rc_label}* to this ticket (mandatory).",
            "** Link the Bug LPD as *Caused By* to this ticket (mandatory).",
            "** Replace this ticket’s LPD with the Bug LPD in:",
            f"** [Testray Subtask|https://testray.liferay.com/web/testray#/testflow/{task_id}/subtasks/{subtask_id}] → Subtask Details → ISSUES",
            "** Close this investigation ticket.",
            "",
            "*⚠️ Always keep ticket and subtask status accurate.*",
            "* If working on a technical solution or code change → set *In Progress*.",
            "* Otherwise, the ticket may be auto-closed if not reproducible in the next run.",
            "",
        ]
    )

    return lines

def _detect_test_type(case_type_name):
    if case_type_name == "Playwright Test":
        return "playwright"
    elif case_type_name == "Automated Functional Test":
        return "poshi"
    elif case_type_name == "Modules Integration Test":
        return "integration"
    elif case_type_name == "Modules Unit Test":
        return "unit"
    return None

def _create_investigation_task_for_subtask(
        acceptance_present,
        subtask_unique_failures,
        subtask_id,
        latest_build_id,
        epic,
        task_id,
        case_history_cache,
):
    """
    Creates an investigation task in Jira for a subtask with unique failures.
    Groups failures by error, outputs a Jira-friendly description with a table of
    test names, components, duration, and RCA details (once).
    """
    # Group by error
    error_to_cases = _group_errors_by_type(subtask_unique_failures)
    case_duration_lookup = _build_case_duration_lookup(
        subtask_unique_failures, latest_build_id
    )
    description_lines = []
    flow_intro = []

    description_lines.extend(
        [
            "*Unique Failures in Testray Subtask*",
            f"[Testray Subtask|https://testray.liferay.com/web/testray#/testflow/{task_id}/subtasks/{subtask_id}]",
            "",
        ]
    )

    first_error = None
    rca_included = False
    component_name = None
    test_type = None

    for error, subtask_case_pairs in error_to_cases.items():
        if not first_error:
            first_error = error[:200]

        description_lines.append("h3. Error")
        description_lines.append(f"{{code}}{error}{{code}}")

        sorted_cases = _sort_cases_by_duration(
            subtask_case_pairs, case_duration_lookup
        )

        (
            printed_rows,
            rca_info,
            batch_name,
            test_selector,
            github_compare,
            component_name,
            case_type_name,
        ) = _build_case_rows(
            sorted_cases,
            case_duration_lookup,
            latest_build_id,
            case_history_cache,
        )

        test_type=_detect_test_type(case_type_name)

        # 🧾 Build adaptive intro
        flow_intro = _build_investigation_intro(
            task_id=task_id,
            subtask_id=subtask_id,
            acceptance_present=acceptance_present,
            test_type=test_type,
        )

        description_lines.append("")
        description_lines.append("|| Test Name || Component || Duration ||")
        for name, duration, component in printed_rows:
            description_lines.append(f"| {name} | {component} | {duration} |")

        if not rca_included and batch_name and test_selector and github_compare:
            description_lines.extend(
                [
                    "",
                    "h3. RCA Details",
                    "",
                    f"*Batch:* {batch_name}",
                    f"*Test Selector:* {test_selector}",
                    f"*GitHub Compare:* {github_compare}",
                ]
            )
            rca_included = True

    summary_prefix = []

    if test_type == "poshi":
        summary_prefix.append("POSHI")

    if acceptance_present:
        summary_prefix.append("ACCEPTANCE")

    prefix = f"[{'/'.join(summary_prefix)}] " if summary_prefix else ""
    summary = f"{prefix}Investigate {first_error[:80]}..."

    description = "\n".join(description_lines + flow_intro)

    current_routine = _get_routine_config(_CURRENT_ROUTINE_ID)
    component_override = (
        current_routine.get("jira_component_override") if current_routine else None
    )
    if component_override:
        jira_components = [component_override]
    else:
        jira_components = [
            {
                "API Builder": "API Builder",
                "Commerce": "Product Information Management",
                "Connectors": "Data Integration > Connectors",
                "Data Migration Center": "Data Integration > Data Migration Center",
                "Export/Import": "Data Integration > Export/Import",
                "Headless Batch Engine API": "Headless Batch Engine API",
                "Headless Discovery Application": "Headless Discovery Application",
                "Job Scheduler": "Data Integration > Job Scheduler",
                "Object": "Objects > Object Entries REST APIs",
                "Object Entries REST APIs": "Objects > Object Entries REST APIs",
                "Order Management": "Order Management",
                "Product Info Management": "Product Information Management",
                "REST Builder": "REST Builder",
                "REST Infrastructure": "REST Infrastructure",
                "Shopping Experience": "Shopping Experience",
                "Site Templates": "Content Publishing > Site Templates",
                "Staging": "Data Integration > Staging",
                "Upgrades Staging": "Data Integration > Staging",
            }.get(c, c)
            for c in (component_name or "Unknown").split(",")
        ]

    label = "acceptance_failure" if acceptance_present else None

    today = date.today()

    if acceptance_present:
        due_date = _add_business_days(today, 2)
    else:
        due_date = _add_business_days(today, 4)

    due_date_str = due_date.strftime("%Y-%m-%d")

    max_days_failing = max(
        (
            _days_since_last_pass(f["case_id"], case_history_cache) or 0
            for f in subtask_unique_failures
        ),
        default=0,
    )
    priority = _priority_for_days(max_days_failing)

    existing_key = _find_existing_open_ticket_for_error(epic, first_error)
    if existing_key:
        print(
            f"♻ Reusing existing open ticket {existing_key} for subtask {subtask_id} "
            f"(matched on Jira summary similarity → skipping creation)"
        )
        return SimpleNamespace(key=existing_key)

    issue = create_jira_task(
        epic=epic,
        summary=summary,
        description=description,
        component=jira_components,
        label=label,
        due_date=due_date_str,
        priority=priority,
        routine_label=current_routine["routine_label"] if current_routine else None,
    )

    if not issue:
        return None

    print(f"✔ Created investigation task for subtask {subtask_id}: {issue.key}")
    return issue


def _add_business_days(start_date, business_days):
    """
    Adds business days (Mon–Fri) to a date.
    """
    current_date = start_date
    added_days = 0

    while added_days < business_days:
        current_date += timedelta(days=1)
        if current_date.weekday() < 5:  # 0 = Mon, 4 = Fri
            added_days += 1

    return current_date