#!/usr/bin/env -S uv run

"""
Utility: lists all Jira priorities in the instance, plus the subset that
LPD allows on a task. Run this once to find the valid priority names for
your project's scheme, then align _PRIORITY_LADDER in
utils/testray_helpers.py accordingly.

The allowed subset is read from the editmeta of an existing LPD task, not
from createmeta: on LPD the priority field is not on the create screen, so
createmeta returns no allowedValues and this script used to answer "the
project may accept any instance-wide priority". It did not: "Normal" is an
instance priority that LPD rejects (2026-11-23, LPD-116994..117002 stayed
at Low). editmeta lists the scheme: Critico, High, Medium, Low.
"""

from utils.jira_helpers import _jira


def main():
    jira = _jira()

    print("\n=== All priorities in this Jira instance ===")
    for p in jira.priorities():
        print(f"  - {p.name} (id={p.id})")

    print("\n=== Priorities allowed on LPD / Task (per editmeta) ===")
    try:
        tasks = jira.search_issues(
            "project = LPD AND issuetype = Task ORDER BY created DESC",
            maxResults=1,
            fields="key",
        )
    except Exception as e:
        print(f"  (search for an LPD task failed: {e})")
        return
    if not tasks:
        print("  (no LPD task found to read editmeta from)")
        return

    try:
        meta = jira.editmeta(tasks[0].key)
    except Exception as e:
        print(f"  (editmeta call on {tasks[0].key} failed: {e})")
        return

    allowed = meta.get("fields", {}).get("priority", {}).get("allowedValues", [])
    if not allowed:
        print(f"  (editmeta of {tasks[0].key} returned no allowedValues for priority)")
        return
    for v in allowed:
        print(f"  - {v.get('name')} (id={v.get('id')})")


if __name__ == "__main__":
    main()
