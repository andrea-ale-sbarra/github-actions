#!/usr/bin/env -S uv run

from utils.testray_helpers import (
    analyze_testflow,
    print_slack_summary,
    report_aft_ratio_for_latest,
    select_routines,
)
from utils.testray_api import get_routine_to_builds
from utils.pr_check import MODE_FULL, check_pr_failures_for_routine
from poshi_burndown import format_burndown_lines

import os

if __name__ == "__main__":
    if os.getenv("DRY_RUN", "false").lower() == "true":
        print("\n[DRY RUN MODE ACTIVE] No changes will be made to Jira or Testray.\n")

    routine_runs = []
    pr_check_by_routine_id = {}
    for routine in select_routines():
        print(f"\n=== Analyzing routine: {routine['name']} (id={routine['id']}) ===\n")
        builds = get_routine_to_builds(routine["id"])
        task_id, closure_summary, cleanup_summary = analyze_testflow(
            builds, routine["id"]
        )
        report_aft_ratio_for_latest(builds)
        routine_runs.append((routine, task_id, closure_summary, cleanup_summary))

        # PR review check: read-only, never blocks the main analysis if it
        # errors out (the analysis has already finished by this point and we
        # don't want a GitHub/Jira hiccup to lose the recap).
        try:
            pr_results = check_pr_failures_for_routine(routine, mode=MODE_FULL)
            pr_check_by_routine_id[routine["id"]] = pr_results
        except Exception as e:
            print(f"PR review check failed for {routine['name']}: {e}")

    # Poshi conversion burndown: read-only file scan, folded into the recap.
    # Wrapped so a missing/relocated tests dir never loses the analysis recap.
    try:
        burndown_lines = ["", *format_burndown_lines()]
    except Exception as e:
        print(f"Poshi burndown skipped: {e}")
        burndown_lines = None

    # Routines of other teams our tickets can break (TRIAGE_ROUTINES, the
    # CMS since 2026-11-25), gathered once here: a few lines go into the
    # recap (asked on 2026-11-26, the CMS check was being added by hand),
    # the full section is printed after the Acceptance one from the same
    # data. Read-only and wrapped: a Testray or GitHub hiccup must not lose
    # the recap.
    triage_data = []
    triage_recap_lines = []
    try:
        from triage_context import (
            format_triage_routine_recap_lines,
            triage_routine_data,
        )
        from utils.testray_helpers import TRIAGE_ROUTINES

        for routine in TRIAGE_ROUTINES:
            data = triage_routine_data(routine["key"])
            triage_data.append(data)
            triage_recap_lines += format_triage_routine_recap_lines(data)
    except Exception as e:
        print(f"Triage routines check skipped: {e}")

    print_slack_summary(
        routine_runs,
        pr_check_by_routine_id,
        burndown_lines,
        extra_section_lines=triage_recap_lines,
    )

    # Routines stopped by the build check (utils/build_health.py) are not
    # analyzed: a human has to decide first. The recap lists them with their
    # open tickets and "waiting for a decision"; the decision is repeated
    # here, after the recap, so it is not lost in the scroll.
    for routine, _task_id, summary, _cleanup in routine_runs:
        if summary and summary.get("blocked"):
            key = routine["name"].lower().replace(" ", "_")
            print(
                f"\n⛔ {routine['name']}: build {summary['build_id']} NOT analyzed "
                f"(out of scale). Rerun with --routine {key} --abandon or "
                f"--routine {key} --force."
            )

    # Open routine tickets against the latest Acceptance build, with the open
    # PRs that name them (triage_context.acceptance_report). Read-only, after
    # the recap so it never sits inside the Slack block, and on every run:
    # on 2026-11-19, with no new routine build, it was what showed the
    # LPD-116295 fix already green and the fix PRs of LPD-114527 and
    # LPD-116292; run only on request (a --triage flag) it was skipped.
    # Wrapped like the PR check: a Testray or GitHub hiccup here must not
    # turn a finished analysis into a failed run.
    try:
        from triage_context import acceptance_report

        print("\n=== Open routine tickets in the latest Acceptance build ===\n")
        acceptance_report()
    except Exception as e:
        print(f"Acceptance check skipped: {e}")

    # Their latest build against our tickets merged where each test went
    # red, in full. After the Acceptance section and for the same reason:
    # read on every run, not only when asked.
    try:
        from triage_context import triage_routine_report

        for data in triage_data:
            print(f"\n=== {data['routine']['name']} tests against our tickets ===\n")
            triage_routine_report(data["routine"]["key"], data=data)
    except Exception as e:
        print(f"Triage routines check skipped: {e}")
