#!/usr/bin/env -S uv run

"""
Utility: lists all Jira priorities in the instance, plus the subset that
Jira reports as allowed for LPD / Task (if any). Run this once to find the
valid priority names for your project's scheme, then align _PRIORITY_LADDER
in utils/testray_helpers.py accordingly.
"""

from utils.jira_helpers import _jira


def main():
    jira = _jira()

    print("\n=== All priorities in this Jira instance ===")
    for p in jira.priorities():
        print(f"  - {p.name} (id={p.id})")

    print("\n=== Priorities allowed on LPD / Task (per createmeta) ===")
    try:
        meta = jira.createmeta(
            projectKeys="LPD",
            issuetypeNames="Task",
            expand="projects.issuetypes.fields",
        )
    except Exception as e:
        print(f"  (createmeta call failed: {e})")
        return

    projects = meta.get("projects", [])
    if not projects:
        print("  (project LPD not found or inaccessible)")
        return

    issue_types = projects[0].get("issuetypes", [])
    if not issue_types:
        print("  (no Task issue type accessible in LPD)")
        return

    for itype in issue_types:
        fields = itype.get("fields", {})
        priority = fields.get("priority", {})
        allowed = priority.get("allowedValues", [])
        if allowed:
            for v in allowed:
                print(f"  - {v.get('name')} (id={v.get('id')})")
        else:
            print(
                "  (createmeta did not return allowedValues for priority — "
                "the project may accept any instance-wide priority)"
            )


if __name__ == "__main__":
    main()
