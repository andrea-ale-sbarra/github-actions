#!/usr/bin/env -S uv run
"""
Read-only evidence for triaging routine tickets and builds. Used by the
routine-triage skill (.claude/skills/routine-triage/), and by hand.

    triage_context.py ticket LPD-X [LPD-Y ...]
    triage_context.py build <routine> [<build id>]

`ticket` prints, for each ticket: status, labels, the error and the tests it
tracks, each test's status in the routine's latest build and its recent
history (from when it is red, whether it alternates), the other routine
tickets that track the same test or error, and the commits between the
"GitHub Compare" SHAs of the ticket (last green ... first red) that touch
the test's file or its app.

`build` prints how a build fails: its health against the previous builds,
the not-passed results grouped by error with how many tests each error hits,
and how many errors look like CI/harness failures. Many tests under a few CI
errors point to a broken CI; many different errors point to regressions.

Why a script and not ad-hoc API calls from the skill: the data is gathered
the same way on every run, and the skill only judges it. Nothing here
writes to Jira, Testray or GitHub.

Commits: the ticket's range can be large (707 commits between the routine
builds of 2026-11-03 and 2026-11-05) and GitHub's compare stops at 250
commits / 300 files, so the commits are listed per path instead: the test's
file (found with GitHub code search, needs GITHUB_TOKEN) and its app folder
(modules/apps/<app>), in the range's time window. Limit: a commit that
breaks the test through code outside the app folder is not listed.
"""

import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from functools import lru_cache

# testray_helpers first: it puts utils/ on sys.path, which the plain-name
# imports below (and build_health's own imports) rely on, same as the
# analyzer.
from utils.testray_helpers import (
    ANALYZED_ROUTINES,
    _JIRA_CODE_BLOCK_RE,
    _get_latest_done_build,
    _normalize_error_keep_locator,
    _tests_listed_in_ticket,
)
from build_health import assess_build, format_health
from github_api import (
    find_file_paths,
    get_commit_date,
    get_commits_for_path,
    get_compare_commits,
    is_ancestor,
)
from jira_helpers import _jira, get_all_issues, get_issue_text, jira_issue_url
from testray_api import (
    ACCEPTANCE_ROUTINE_ID,
    fetch_case_results,
    get_all_build_case_results,
    get_routine_to_builds,
    testray_build_url,
)

HISTORY_LENGTH = 12
MAX_ERROR_GROUPS = 12

# Errors that come from the CI or the test harness rather than from the
# product: the same texts the analyzer skips or treats as shard noise
# (_should_skip_result, Playwright shard rows), plus network failures
# against the CI host seen in the Playwright tests (LPD-114280).
_CI_ERROR_PATTERNS = (
    "Failed prior to running",
    "The build failed prior to running the test",
    "Unable to run on CI",
    "Unable to run test on CI",
    "Failed to run test on CI",
    "Failed for unknown reason",
    "stop-docker-containers",
    "timed out after",
    "net::ERR_",
    "ECONNREFUSED",
    # Console tail of a Poshi functional batch (docker run arguments): on
    # build 560288074 78 batch rows carried it, none a test failure.
    "[echo] ",
)

_COMPARE_RE = re.compile(
    r"github\.com/([^/\s]+)/([^/\s]+)/compare/([0-9a-f]{7,40})\.\.\.([0-9a-f]{7,40})"
)


def _routine_for_labels(labels):
    for routine in ANALYZED_ROUTINES:
        if routine["routine_label"] in labels:
            return routine
    return None


@lru_cache(maxsize=None)
def _latest_build_index(routine_id):
    """name -> (case_id, status, error, result) for the routine's latest
    DONE build, plus the build itself; the raw result is kept for its
    attachments (batch axis, see _batch_mates)."""
    build = _get_latest_done_build(get_routine_to_builds(routine_id), quiet=True)
    index = {}
    for result in get_all_build_case_results(build["id"], include_case=True):
        name = (result.get("r_caseToCaseResult_c_case") or {}).get("name")
        if name:
            index[name] = (
                result.get("r_caseToCaseResult_c_caseId"),
                (result.get("dueStatus") or {}).get("key") or "UNTESTED",
                result.get("errors") or "",
                result,
            )
    return build, index


def _history_line(case_id, routine_id):
    """Newest first, one letter per build: P passed, F failed, B blocked,
    U untested, T test fix; plus how many builds it has been red."""
    history = sorted(
        fetch_case_results(case_id, routine_id),
        key=lambda h: h.get("executionDate") or "",
        reverse=True,
    )[:HISTORY_LENGTH]
    letters = "".join((h.get("status") or "?")[0] for h in history)
    red = 0
    for h in history:
        if h.get("status") == "PASSED":
            break
        red += 1
    flips = sum(1 for a, b in zip(letters, letters[1:]) if (a == "P") != (b == "P"))
    return f"{letters}  (newest first; red for the last {red}, {flips} pass/fail flips)"


def _similar_tickets(key, routine, tests, errors):
    jql = (
        f'labels = "{routine["routine_label"]}" AND key != {key} AND '
        "(statusCategory != Done OR (resolution = Discarded AND resolved >= -30d))"
    )
    errors_norm = {_normalize_error_keep_locator(e) for e in errors}
    found = []
    for issue in get_all_issues(jql, fields=["description", "status", "summary"]):
        description = issue.fields.description or ""
        same_test = [t for t in tests if t in description]
        same_error = errors_norm & {
            _normalize_error_keep_locator(b) for b in _JIRA_CODE_BLOCK_RE.findall(description)
        }
        if same_test or same_error:
            why = "same test" if same_test else "same error"
            found.append(f"{issue.key} [{issue.fields.status.name}] {why}: {issue.fields.summary[:100]}")
    return found


def ticket_report(key):
    issue = get_all_issues(f"key = {key}", fields=["summary", "status", "labels", "created", "components"])[0]
    text = get_issue_text(key)
    labels = issue.fields.labels or []
    routine = _routine_for_labels(labels)

    print(f"=== {key} {jira_issue_url(key)}")
    print(f"Summary: {issue.fields.summary}")
    print(f"Status: {issue.fields.status.name} | created {issue.fields.created[:10]} | labels {', '.join(labels)}")
    if not routine:
        print("Not a routine ticket (no routine label): stopping here.\n")
        return

    _print_links_and_human_comments(key)

    errors = [e.strip() for e in _JIRA_CODE_BLOCK_RE.findall(text)]
    print("Errors in the ticket:")
    for e in dict.fromkeys(errors):
        print(f"  - {e[:400]}")

    tests = sorted(_tests_listed_in_ticket(text))
    build, index = _latest_build_index(routine["id"])
    acceptance = []
    print(f"Tests tracked ({len(tests)}), status in latest {routine['name']} build {build['id']} ({build.get('name')}):")
    for t in tests:
        case_id, status, error, result = index.get(t, (None, "NOT IN BUILD", "", None))
        print(f"  - {t}\n      now: {status}")
        if case_id:
            print(f"      history: {_history_line(case_id, routine['id'])}")
            if status != "PASSED" and error and error.strip() not in errors:
                print(f"      current error differs from the ticket: {error[:300]}")
            acceptance.append((t, case_id))
            if "TEST_SETUP_ERROR" in error and result:
                _print_batch_mates(build["id"], result, index)

    similar = _similar_tickets(key, routine, tests, errors)
    print(f"Other routine tickets with the same test or error ({len(similar)}):")
    for line in similar:
        print(f"  - {line}")

    match = _COMPARE_RE.search(text)
    if not match:
        print("No GitHub compare range in the ticket.\n")
        return
    owner, repo, base, head = match.groups()
    since, until = get_commit_date(owner, repo, base), get_commit_date(owner, repo, head)
    print(f"Range {base[:12]} (last green, {since}) ... {head[:12]} (first red, {until})")
    if not since or not until:
        print("  Could not date the range: no commit list.\n")
        return
    _print_acceptance_narrowing(owner, repo, base, head, acceptance, since, until)
    for scope, path in _scopes_for_tests(owner, repo, tests):
        commits = get_commits_for_path(owner, repo, path, head, since, until)
        print(f"  Commits touching the {scope} {path} ({len(commits)}):")
        for c in commits[:40]:
            print(f"    {c['sha'][:10]} {c['author']}: {c['message'][:110]}")
        if len(commits) > 40:
            print(f"    ... {len(commits) - 40} more")
    print()


# Comments the analyzer writes by itself: left out of the "human comments"
# list, which is there to show what people already know about the ticket.
_SCRIPT_COMMENT_PREFIXES = (
    "These tests are also failing in",
    "Closed. Not reproducible",
    "Reopened. Failing again",
    "Closing sub-task",
)


def _print_links_and_human_comments(key):
    """Links and the comments people wrote. On 2026-11-06 the first deep
    triage of LPD-110849 missed that the ticket owner had already linked a
    fix (LPD-113922, migration of the failing Poshi tests) and explained it
    in a comment: what people know comes before any new analysis."""
    issue = _jira().issue(key, fields="issuelinks,comment")
    links = [
        f"{l.type.outward if hasattr(l, 'outwardIssue') else l.type.inward} "
        f"{(getattr(l, 'outwardIssue', None) or getattr(l, 'inwardIssue', None)).key}"
        for l in issue.fields.issuelinks
    ]
    print(f"Links: {', '.join(links) if links else 'none'}")
    comments = [
        c for c in issue.fields.comment.comments
        if not (c.body or "").lstrip().startswith(_SCRIPT_COMMENT_PREFIXES)
    ]
    print(f"Comments by people or earlier triage ({len(comments)}):")
    for c in comments[-6:]:
        body = " ".join((c.body or "").split())
        print(f"  > {c.author.displayName} {c.created[:10]}: {body[:500]}")


_TOOLING_COMMIT_RE = re.compile(
    r"LRCI-|playwright|chromium|upgrade|bump|yarn|node\b|jdk|gradle", re.I
)


def _print_acceptance_narrowing(owner, repo, base, head, tests, since, until):
    """Narrow the ticket's range with Acceptance, which runs daily on most
    of the same tests: the newest Acceptance pass whose SHA lies between
    the routine's last green and first red SHA becomes the new green end.

    Case: LPD-114278/114279 (2026-11-06). The routine range b95e044c...
    c638e9f9 held 707 commits; the tests passed in Acceptance at 35de5e4d,
    which left ~10 hours and 390 commits, among them LRCI-8461 (Playwright
    1.63 / Chromium 153), the cause. Commits already in a passing build
    are ruled out. Tooling commits (Playwright, Chromium, upgrades, LRCI
    test-fix sweeps) are listed first: they break tests of every app, so a
    filter on the test's app never shows them."""
    # Only Acceptance runs dated inside the range can hold a SHA inside it
    # (with a day of margin for the build running after the commit): the
    # others would cost a GitHub compare each, and an old base against a
    # recent SHA can fail on GitHub's side (seen on LPD-111086).
    first_day = since[:10]
    last_day = (datetime.fromisoformat(until.replace("Z", "+00:00")) + timedelta(days=1)).date().isoformat()
    best = None
    for test, case_id in tests[:4]:
        history = sorted(
            fetch_case_results(case_id, ACCEPTANCE_ROUTINE_ID),
            key=lambda h: h.get("executionDate") or "",
            reverse=True,
        )[:HISTORY_LENGTH]
        letters = " ".join(f"{(h.get('executionDate') or '')[5:10]}:{(h.get('status') or '?')[0]}" for h in history)
        print(f"  Acceptance history of {test[:70]}: {letters or 'not in Acceptance'}")
        for h in history:
            sha = h.get("gitHash")
            day = (h.get("executionDate") or "")[:10]
            if h.get("status") != "PASSED" or not sha or sha == base or not first_day <= day <= last_day:
                continue
            try:
                inside = is_ancestor(owner, repo, base, sha) and is_ancestor(owner, repo, sha, head)
                if inside and (best is None or is_ancestor(owner, repo, best, sha)):
                    best = sha
            except Exception as e:
                print(f"  (could not place Acceptance SHA {sha[:12]} in the range: {e})")
                continue
            if inside:
                break
    if not best:
        print("  No Acceptance pass inside the range: it cannot be narrowed.")
        return
    compare = get_compare_commits(owner, repo, best, head)
    commits = compare["commits"]
    print(
        f"  Narrowed range (Acceptance pass): {best[:12]}...{head[:12]}, "
        f"{compare['total_commits']} commits"
        f"{' (GitHub listed only the first 250)' if compare['total_commits'] > len(commits) else ''}"
    )
    tooling = [c for c in commits if _TOOLING_COMMIT_RE.search(c["message"])]
    print(f"  Tooling / test-infra commits in it ({len(tooling)}):")
    for c in tooling[:30]:
        print(f"    {c['sha'][:10]} {c['author']}: {c['message'][:110]}")


_BATCH_AXIS_RE = re.compile(r"/\d+/((?:functional|modules-integration)[^/]*/\d+/\d+)/")


def _batch_axis(result):
    import json

    attachments = result.get("attachments")
    if isinstance(attachments, str):
        attachments = json.loads(attachments)
    for a in attachments or []:
        m = _BATCH_AXIS_RE.search(a.get("url") or a.get("value") or "")
        if m:
            return m.group(1)
    return None


def _print_batch_mates(build_id, result, index):
    """For a set-up error, the other results of the same batch axis and the
    one right before the failing test (result ids follow the run order of
    the axis). A set-up error is usually the victim of the previous test:
    on 2026-11-06, the 4 "Unable to delete the portal instances" failures
    of the last builds each followed the same test of their batch."""
    axis = _batch_axis(result)
    if not axis:
        return
    mates = sorted(
        (r for (_c, _s, _e, r) in index.values() if _batch_axis(r) == axis),
        key=lambda r: r["id"],
    )
    names = [(r.get("r_caseToCaseResult_c_case") or {}).get("name") for r in mates]
    position = next((i for i, r in enumerate(mates) if r["id"] == result["id"]), None)
    print(f"      batch {axis} of build {build_id}: {len(mates)} results")
    if position:
        previous = mates[position - 1]
        print(
            f"      ran right before: {names[position - 1]} "
            f"[{(previous.get('dueStatus') or {}).get('key')}]"
        )
    for r, n in zip(mates, names):
        status = (r.get("dueStatus") or {}).get("key")
        if status != "PASSED" and r["id"] != result["id"]:
            print(f"      also not passed in the batch: {n} [{status}] {(r.get('errors') or '')[:120]}")


def _test_file_name(test_name):
    """File that defines a test, from its Testray name:
    Java "com.liferay.x.FooTest" -> FooTest.java (+ package path),
    Poshi "LocalFile.Foo#Bar" -> Foo.testcase,
    Playwright "dir/foo.spec.ts > title" -> foo.spec.ts (+ dir).
    Returns (file name, path suffix to pick among homonyms) or None
    (shard rows, "Top Level Build")."""
    if " > " in test_name or test_name.endswith(".spec.ts"):
        spec = test_name.split(" > ")[0].strip()
        return spec.rsplit("/", 1)[-1], spec
    if test_name.startswith("LocalFile."):
        return test_name[len("LocalFile."):].split("#")[0] + ".testcase", None
    if re.fullmatch(r"com\.liferay(\.\w+)+", test_name):
        return test_name.rsplit(".", 1)[-1] + ".java", test_name.replace(".", "/") + ".java"
    return None


def _scopes_for_tests(owner, repo, tests):
    """(scope, path) pairs to list commits for: each test file, then the app
    folder holding it (modules/apps/<app>), where a product change that
    breaks the test most likely lands. Deduplicated, at most 4 tests."""
    scopes = []
    for test in tests[:4]:
        found = _test_file_name(test)
        if not found:
            continue
        name, suffix = found
        paths = find_file_paths(owner, repo, name)
        if suffix:
            paths = [p for p in paths if p.endswith(suffix)] or paths
        if not paths:
            print(f"  (file of {test[:80]} not found on GitHub)")
            continue
        path = paths[0]
        scopes.append(("test file", path))
        parts = path.split("/")
        if len(parts) > 3 and parts[0] == "modules" and parts[1] == "apps":
            scopes.append(("app", "/".join(parts[:3])))
    return list(dict.fromkeys(scopes))


def build_report(routine_arg, build_id=None):
    needle = routine_arg.lower().replace("_", " ")
    routine = next(
        (r for r in ANALYZED_ROUTINES if needle == str(r["id"]) or needle in r["name"].lower()),
        None,
    )
    if not routine:
        sys.exit(f"Unknown routine '{routine_arg}'")
    if build_id is None:
        build_id = _get_latest_done_build(get_routine_to_builds(routine["id"]), quiet=True)["id"]

    print(f"=== {routine['name']} build {build_id} {testray_build_url(build_id, routine['id'])}")
    health = assess_build(routine["id"], build_id)
    print(f"Health: {format_health(health) if health else 'no measure'}"
          f"{' — OUT OF SCALE' if health and health['out_of_scale'] else ''}")

    groups = defaultdict(set)
    statuses = Counter()
    for result in get_all_build_case_results(build_id, include_case=True):
        status = (result.get("dueStatus") or {}).get("key") or "UNTESTED"
        if status == "PASSED":
            continue
        statuses[status] += 1
        name = (result.get("r_caseToCaseResult_c_case") or {}).get("name") or "?"
        error = (result.get("errors") or "").strip() or f"<no error, {status}>"
        groups[_normalize_error_keep_locator(error)[:160]].add(name)

    total = sum(statuses.values())
    ci = sum(len(n) for e, n in groups.items() if any(p.lower() in e.lower() for p in _CI_ERROR_PATTERNS))
    print(f"Not passed: {total} ({', '.join(f'{k} {v}' for k, v in statuses.most_common())})")
    print(f"Distinct errors: {len(groups)}; results with a CI/harness error: {ci}")
    sizes = sorted((len(n) for n in groups.values()), reverse=True)
    if sizes:
        print(f"Tests per error: top {sizes[:5]}, median {statistics.median(sizes):g}")
    print(f"Top {MAX_ERROR_GROUPS} errors by tests hit:")
    for error, names in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:MAX_ERROR_GROUPS]:
        sample = sorted(names)[0]
        print(f"  {len(names):4d}  {error}\n        e.g. {sample[:120]}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "ticket":
        for k in sys.argv[2:]:
            ticket_report(k)
    elif len(sys.argv) >= 3 and sys.argv[1] == "build":
        build_report(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else None)
    else:
        sys.exit(__doc__)
