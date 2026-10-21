#!/usr/bin/env -S uv run

"""
Poshi conversion burndown — Phase 1 (file-based count).

The "how many Poshi tests are left to convert" denominator is computed by
scanning the Poshi `.testcase` source files directly, NOT Testray: Testray
only counts what actually ran in a build, while the migration target is every
test still living in the source tree. (Testray is used elsewhere — the
conversion queue — only to PRIORITIZE what to convert, never to count.)

For every `.testcase` file in the Poshi tests directory it reports, per file:
  - total `test {}` blocks,
  - how many are live (real coverage still riding on Poshi),
  - how many are dead `@ignore` stubs (already inert — free to delete).

The live count is the real burndown number. Dead stubs are split out because
deleting them moves the file count without converting anything.

Directory resolution (first hit wins):
  1. CLI arg:                ./poshi_burndown.py /path/to/tests
  2. env POSHI_TESTS_DIR
  3. DEFAULT_POSHI_TESTS_DIR below

Read-only: never touches Jira, Testray, or the source files.
"""

import os
import re
import sys

# Default location of the Commerce Poshi functional tests. All three Jira
# components (PIM, Order Management, Shopping Experience) live in this one
# folder, so a single scan covers the whole routine. Override with the
# POSHI_TESTS_DIR env var or a CLI arg when the checkout lives elsewhere.
DEFAULT_POSHI_TESTS_DIR = (
    "/opt/work/src/lportal/liferay-portal/modules/apps/commerce/"
    "commerce-product-test/src/testFunctional/tests"
)

# A `test Name {` block opener. Mirrors the grep used to establish the
# baseline count so the two never drift.
_TEST_OPEN_RE = re.compile(r"^\s*test\s+([A-Za-z0-9_]+)\s*\{")

# A Poshi annotation line, e.g. `@ignore = "Test Stub"` or `@priority = "5"`.
_ANNOTATION_RE = re.compile(r"^\s*@(\w+)")

# The `definition {` opener — file-level annotations (incl. a file-wide
# `@ignore`) sit directly above it.
_DEFINITION_OPEN_RE = re.compile(r"^\s*definition\s*\{")


def resolve_tests_dir(argv):
    """Pick the Poshi tests directory: CLI arg, then env, then default."""
    if len(argv) > 1 and argv[1].strip():
        return argv[1].strip()
    return os.getenv("POSHI_TESTS_DIR", "").strip() or DEFAULT_POSHI_TESTS_DIR


def _annotation_group_has_ignore(lines, opener_index):
    """Return True if an `@ignore` annotation sits in the contiguous
    annotation/blank run directly above `opener_index`.

    Walks upward over lines that are annotations (`@...`) or blank and stops
    at the first other line (e.g. a closing `}` of the previous block), so it
    only ever inspects the annotation group bound to this opener.
    """
    i = opener_index - 1
    while i >= 0:
        line = lines[i]
        if _ANNOTATION_RE.match(line):
            if line.lstrip().startswith("@ignore"):
                return True
            i -= 1
            continue
        if line.strip() == "":
            i -= 1
            continue
        break
    return False


def scan_file(path):
    """Scan one `.testcase` file.

    Returns (total, live, ignored). A test is counted as ignored when either
    the whole file is `@ignore`d (annotation above `definition {`) or the
    individual test carries its own `@ignore`.
    """
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    file_ignored = any(
        _DEFINITION_OPEN_RE.match(line)
        and _annotation_group_has_ignore(lines, idx)
        for idx, line in enumerate(lines)
    )

    total = live = ignored = 0
    for idx, line in enumerate(lines):
        if not _TEST_OPEN_RE.match(line):
            continue
        total += 1
        if file_ignored or _annotation_group_has_ignore(lines, idx):
            ignored += 1
        else:
            live += 1
    return total, live, ignored


def collect(tests_dir):
    """Scan every top-level `.testcase` file in `tests_dir`.

    Returns (rows, totals) where rows is a list of
    (filename, total, live, ignored) sorted by live count desc, and totals is
    the same triple summed. `dependencies/` and other subdirs are ignored —
    only real test files at the top level are counted.
    """
    try:
        names = sorted(
            n for n in os.listdir(tests_dir) if n.endswith(".testcase")
        )
    except FileNotFoundError:
        raise SystemExit(
            f"✘ Poshi tests directory not found: {tests_dir}\n"
            f"  Set POSHI_TESTS_DIR or pass the path as an argument."
        )

    rows = []
    tot = tot_live = tot_ignored = 0
    for name in names:
        total, live, ignored = scan_file(os.path.join(tests_dir, name))
        rows.append((name, total, live, ignored))
        tot += total
        tot_live += live
        tot_ignored += ignored

    rows.sort(key=lambda r: (r[2], r[1]), reverse=True)
    return rows, (tot, tot_live, tot_ignored)


def format_burndown_lines(tests_dir=None):
    """Build the plain-text burndown block (list of lines).

    Reused by both the standalone run and the live recap. Never raises on a
    missing directory in the recap path — callers there wrap it in try/except,
    but we keep the standalone `main` strict via collect()'s SystemExit.
    """
    tests_dir = tests_dir or resolve_tests_dir(sys.argv)
    rows, (tot, tot_live, tot_ignored) = collect(tests_dir)

    lines = [
        "Poshi conversion burndown (source files):",
        "",
        f"  {tot_live} live Poshi tests left to convert "
        f"across {len(rows)} files "
        f"({tot_ignored} dead @ignore stubs not counted; {tot} blocks total).",
        "",
    ]

    name_w = max((len(r[0]) for r in rows), default=20)
    header = f"  {'file':<{name_w}}  {'live':>5}  {'stub':>5}  {'total':>5}"
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for name, total, live, ignored in rows:
        if total == 0:
            continue
        lines.append(
            f"  {name:<{name_w}}  {live:>5}  {ignored:>5}  {total:>5}"
        )
    return lines


def main():
    tests_dir = resolve_tests_dir(sys.argv)
    print(f"Scanning Poshi tests in: {tests_dir}\n")
    for line in format_burndown_lines(tests_dir):
        print(line)


if __name__ == "__main__":
    main()
