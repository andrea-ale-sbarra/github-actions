#!/usr/bin/env -S uv run

from utils.testray_helpers import (
    analyze_testflow,
    print_slack_summary,
    report_aft_ratio_for_latest,
    select_routines,
)
from utils.testray_api import get_routine_to_builds
from utils.pr_check import check_pr_failures_for_routine

import os

if __name__ == "__main__":
    if os.getenv("DRY_RUN", "false").lower() == "true":
        print("\n[DRY RUN MODE ACTIVE] No changes will be made to Jira or Testray.\n")

    routine_runs = []
    pr_check_by_routine_id = {}
    for routine in select_routines():
        print(f"\n=== Analyzing routine: {routine['name']} (id={routine['id']}) ===\n")
        builds = get_routine_to_builds(routine["id"])
        task_id, closure_summary = analyze_testflow(builds, routine["id"])
        report_aft_ratio_for_latest(builds)
        routine_runs.append((routine, task_id, closure_summary))

        # PR review check: read-only, never blocks the main analysis if it
        # errors out (the analysis has already finished by this point and we
        # don't want a GitHub/Jira hiccup to lose the recap).
        try:
            pr_results = check_pr_failures_for_routine(routine)
            pr_check_by_routine_id[routine["id"]] = pr_results
        except Exception as e:
            print(f"PR review check failed for {routine['name']}: {e}")

    print_slack_summary(routine_runs, pr_check_by_routine_id)
