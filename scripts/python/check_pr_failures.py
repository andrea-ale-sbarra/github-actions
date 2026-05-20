#!/usr/bin/env -S uv run

"""
Standalone PR review check.

For every Jira ticket carrying a routine's `check_label` (the developer's
"is this PR done?" cover marker on the Jira board), this script:

  1. Pulls the merged PR URL from the most recent "Enterprise Release HU"
     bot comment on the ticket.
  2. Resolves the PR's merge commit SHA + the files it touched, via the
     GitHub API.
  3. Looks up a Testray build that ran on that SHA — preferring the
     latest Acceptance build, falling back to the latest routine build
     (Commerce / UM) if Acceptance hasn't seen the merge yet.
  4. Matches the test files the PR added/modified against the cases that
     ran in that build and prints each one's status (PASSED, FAILED,
     BLOCKED, NOT FOUND, etc.).

The output is a Slack-ready block, ready to be copy-pasted. Nothing is
written to Jira, GitHub, or Testray. Use it during the day when the main
testflow analysis has already finished but a fresh Acceptance build has
just completed, to re-check the merged tickets without re-running the
full analysis.
"""

from utils.pr_check import (
    MODE_STANDALONE,
    check_pr_failures_for_routine,
    print_pr_check_standalone,
)
from utils.testray_helpers import select_routines


def main():
    pr_check_by_routine = []
    for routine in select_routines():
        try:
            results = check_pr_failures_for_routine(routine, mode=MODE_STANDALONE)
        except Exception as e:
            print(f"⚠ PR review check failed for {routine['name']}: {e}")
            results = []
        pr_check_by_routine.append((routine, results))

    print_pr_check_standalone(pr_check_by_routine)


if __name__ == "__main__":
    main()
