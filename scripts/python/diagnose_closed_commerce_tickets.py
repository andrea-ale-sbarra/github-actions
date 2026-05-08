#!/usr/bin/env -S uv run

"""
Diagnostic: for every Commerce-tagged Jira ticket closed today, print the
build SHA cited in its closing comment. Cross-reference against the SHA of
the most recent Commerce/UM build that ran today, so you can tell at a glance
which tickets were closed legitimately (by a Commerce run) vs by mistake
(by a UM run that fired stale-closure before the bug was fixed).

Closing comment format set by close_issue() in utils/jira_helpers.py:
    "Closed. Not reproducible in SHA {build_hash}"
"""

import os
import re
import sys
from datetime import date

from utils.jira_helpers import _jira, get_all_issues
from utils.testray_api import (
    COMMERCE_ROUTINE_ID,
    USER_MANAGEMENT_ROUTINE_ID,
    get_routine_to_builds,
)

CLOSED_SHA_RE = re.compile(r"Closed\.\s+Not reproducible in SHA\s+(\S+)")


def _shas_by_routine(routine_id, limit=20):
    """Return a list of (build_id, gitHash, name) for the most recent N builds."""
    builds = get_routine_to_builds(routine_id)[:limit]
    return [
        (b["id"], b.get("gitHash"), b.get("name"))
        for b in builds
        if b.get("gitHash")
    ]


def main():
    today = date.today().strftime("%Y-%m-%d")
    routine_label = os.getenv("ROUTINE_LABEL", "commerce_routine_tasks")
    jql = (
        f'labels = "{routine_label}" '
        f'AND status = "Closed" '
        f'AND updated >= "{today}"'
    )
    print(f"JQL: {jql}\n")

    issues = get_all_issues(jql, fields=["summary"])
    if not issues:
        print(
            f"✔ No tickets with label '{routine_label}' closed today. "
            "Nothing to investigate."
        )
        return

    print(f"Fetching reference SHAs for both routines (last 20 builds)...")
    commerce_builds = _shas_by_routine(COMMERCE_ROUTINE_ID)
    um_builds = _shas_by_routine(USER_MANAGEMENT_ROUTINE_ID)
    commerce_shas = {sha: (bid, name) for bid, sha, name in commerce_builds}
    um_shas = {sha: (bid, name) for bid, sha, name in um_builds}

    print(f"  Commerce: {len(commerce_shas)} SHAs")
    print(f"  UM      : {len(um_shas)} SHAs\n")

    print(f"Found {len(issues)} ticket(s) with label '{routine_label}' closed today.\n")

    suspect_um = []     # SHA found in UM builds but NOT in Commerce builds
    likely_commerce = []  # SHA found in Commerce builds but NOT in UM builds
    ambiguous = []      # SHA found in both routines: cannot tell from SHA alone
    unknown = []        # SHA not found in either, or no closing comment at all

    for issue in issues:
        comments = _jira().comments(issue.key)
        sha = None
        closed_at = None
        for c in reversed(comments):
            m = CLOSED_SHA_RE.search(c.body or "")
            if m:
                sha = m.group(1)
                closed_at = getattr(c, "created", None)
                break

        if not sha:
            print(f"  {issue.key:15s} <no closing-SHA comment found>")
            unknown.append(issue.key)
            continue

        sha_short = sha[:12]
        ts = f" @ {closed_at}" if closed_at else ""
        if sha in um_shas and sha not in commerce_shas:
            bid, bname = um_shas[sha]
            print(
                f"  {issue.key:15s} SHA {sha_short} → UM-only "
                f"(build {bid}){ts}  ⚠ likely closed by UM run"
            )
            suspect_um.append((issue.key, sha, bid, bname, closed_at))
        elif sha in commerce_shas and sha not in um_shas:
            bid, bname = commerce_shas[sha]
            print(
                f"  {issue.key:15s} SHA {sha_short} → Commerce-only "
                f"(build {bid}){ts}  → likely closed by Commerce run"
            )
            likely_commerce.append((issue.key, sha, bid, bname, closed_at))
        elif sha in um_shas and sha in commerce_shas:
            print(
                f"  {issue.key:15s} SHA {sha_short} → present in BOTH routines "
                f"(can't tell from SHA){ts}"
            )
            ambiguous.append((issue.key, sha, closed_at))
        else:
            print(
                f"  {issue.key:15s} SHA {sha_short} → not in last 20 builds "
                f"of either routine{ts}"
            )
            unknown.append(issue.key)

    print()
    print("=" * 70)
    print(f"SHA seen only on Commerce builds (likely Commerce closure) : {len(likely_commerce)}")
    print(f"SHA seen only on UM builds (likely UM closure — suspect)   : {len(suspect_um)}")
    print(f"SHA on both routines (ambiguous, see closure timestamp)    : {len(ambiguous)}")
    print(f"Other (no SHA, or SHA out of recent build window)          : {len(unknown)}")
    print()
    print("Note: classification is based on SHA cross-referencing only.")
    print("It is a strong hint, not proof — if Commerce and UM happened to test")
    print("the exact same git commit, those rows fall into 'ambiguous'.")
    print("For ambiguous rows, compare the closure timestamp against when you")
    print("ran each routine to figure out the actual culprit.")

    if suspect_um:
        print()
        print("Candidates to reopen (SHA exclusive to UM builds):")
        for key, sha, bid, bname, closed_at in suspect_um:
            print(f"  {key}  closed @ {closed_at}  citing UM build {bid} — {bname}")


if __name__ == "__main__":
    sys.exit(main() or 0)
