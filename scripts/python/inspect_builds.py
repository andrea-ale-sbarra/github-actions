#!/usr/bin/env -S uv run

"""
Diagnostic: print the most recent builds of every selected routine as
Testray's API returns them. Useful when the analyzer picks an unexpected
build — this tool exposes the dateCreated / importStatus / id fields the
picker uses under the hood, which the Testray UI doesn't show.

Selection follows the same ROUTINE env var as analyze_testray_results.py
(set via `--routine` in run_local.sh). With no filter, prints all routines.

Args:
- No args                  → list per routine, default limit 20
- A small integer (≤ 1000) → list per routine with that limit
- One or more big integers → look up those specific build IDs directly.
                             Useful when a build appears in the Testray UI
                             but not in the routine's API response.
"""

import json
import os
import sys

from utils.testray_api import get_build_info, get_routine_to_builds
from utils.testray_helpers import select_routines


_BUILD_ID_THRESHOLD = 1_000  # anything larger than this is treated as a build id


def _looks_like_build_ids(args):
    return all(arg.isdigit() and int(arg) > _BUILD_ID_THRESHOLD for arg in args)


def _inspect_specific_builds(build_ids):
    interesting_keys = (
        "id",
        "name",
        "importStatus",
        "dateCreated",
        "dueDate",
        "gitHash",
        "r_routineToBuilds_c_routineId",
    )
    for bid in build_ids:
        print(f"=== Build {bid} ===")
        try:
            info = get_build_info(int(bid))
        except Exception as e:
            print(f"  ✘ Failed to fetch: {e}\n")
            continue
        for k in interesting_keys:
            print(f"  {k:<35}: {info.get(k)!r}")
        # also dump anything non-trivial that wasn't in the curated list
        extras = {k: v for k, v in info.items() if k not in interesting_keys and not k.startswith("_") and v not in (None, "", [])}
        if extras:
            print(f"  (other fields)")
            for k, v in extras.items():
                print(f"    {k}: {v!r}")
        print()


def main():
    args = sys.argv[1:]

    if args and _looks_like_build_ids(args):
        _inspect_specific_builds(args)
        return

    limit = int(args[0]) if args else 20

    for routine in select_routines():
        builds = get_routine_to_builds(routine["id"])
        print(
            f"=== {routine['name']} (id={routine['id']}) — "
            f"{len(builds)} builds total, showing first "
            f"{min(limit, len(builds))} sorted by dateCreated DESC ===\n"
        )

        header = (
            f"{'idx':>3}  {'id':>10}  {'importStatus':<12}  "
            f"{'dateCreated':<24}  {'dueDate':<24}  {'gitHash':<12}  name"
        )
        print(header)
        print("-" * len(header))

        for i, b in enumerate(builds[:limit]):
            bid = b.get("id", "")
            status = (b.get("importStatus") or {}).get("key", "?")
            date_created = b.get("dateCreated", "")
            due_date = b.get("dueDate", "") or ""
            git_hash = (b.get("gitHash") or "")[:12]
            name = b.get("name", "")
            print(
                f"{i:>3}  {bid:>10}  {status:<12}  "
                f"{date_created:<24}  {due_date:<24}  {git_hash:<12}  {name}"
            )

        print()


if __name__ == "__main__":
    main()
