#!/usr/bin/env -S uv run

from utils.testray_helpers import (
    analyze_testflow,
    print_closure_report,
    print_slack_summary,
    report_aft_ratio_for_latest,
    select_routines,
)
from utils.testray_api import get_routine_to_builds

import os

if __name__ == "__main__":
    if os.getenv("DRY_RUN", "false").lower() == "true":
        print("\n🚀 [DRY RUN MODE ACTIVE] No changes will be made to Jira or Testray.\n")

    routine_runs = []
    for routine in select_routines():
        print(f"\n=== Analyzing routine: {routine['name']} (id={routine['id']}) ===\n")
        builds = get_routine_to_builds(routine["id"])
        task_id, closure_summary = analyze_testflow(builds, routine["id"])
        report_aft_ratio_for_latest(builds)
        routine_runs.append((routine, task_id, closure_summary))

    print_slack_summary(routine_runs)
    print_closure_report(routine_runs)
