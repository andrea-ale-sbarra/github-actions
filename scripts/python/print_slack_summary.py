#!/usr/bin/env -S uv run

"""
Utility: re-prints the Slack-ready recap for the latest DONE build's task
of every analyzed routine, without re-running the analysis. Useful when
you want the summary message after a run has already completed.
"""

from utils.testray_api import get_build_tasks, get_routine_to_builds
from utils.testray_helpers import (
    _get_latest_done_build,
    print_slack_summary,
    select_routines,
)


def main():
    routine_runs = []
    for routine in select_routines():
        builds = get_routine_to_builds(routine["id"])
        latest_build = _get_latest_done_build(builds)
        if not latest_build:
            routine_runs.append((routine, None))
            continue

        tasks = get_build_tasks(latest_build["id"])
        if not tasks:
            print(
                f"✘ No task found for {routine['name']} build "
                f"'{latest_build.get('name')}'."
            )
            routine_runs.append((routine, None))
            continue

        routine_runs.append((routine, tasks[0]["id"]))

    print_slack_summary(routine_runs)


if __name__ == "__main__":
    main()
