#!/usr/bin/env -S uv run
"""
Read-only evidence for triaging routine tickets and builds. Used by the
routine-triage skill (.claude/skills/routine-triage/), and by hand.

    triage_context.py ticket LPD-X [LPD-Y ...]
    triage_context.py build <routine> [<build id>]
    triage_context.py acceptance [<build id>]

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

`acceptance` prints, for every open routine ticket, the status of its tests
in the latest DONE Acceptance build and their recent Acceptance history: the
check to run when there is no new routine build (see acceptance_report).

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
    search_open_prs,
)
from jira_helpers import _jira, get_all_issues, get_issue_text, jira_issue_url
from testray_api import (
    ACCEPTANCE_ROUTINE_ID,
    fetch_case_results,
    get_abandoned_build_ids,
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
    U untested, T test fix, X not passed in a build that was ABANDONED (the
    CI broke it); plus how many builds it has been red.

    X is left out of the red count and of the flips: one broken build turns
    a stable "PPPP" into "PFPP", two flips for a test that never broke (the
    Commerce and UM builds of 2026-11-19, abandoned, red on most tests). A
    pass in an abandoned build stays a P: the test did run.
    """
    history = sorted(
        fetch_case_results(case_id, routine_id),
        key=lambda h: h.get("executionDate") or "",
        reverse=True,
    )[:HISTORY_LENGTH]
    abandoned = get_abandoned_build_ids()

    def letter(h):
        status = h.get("status") or "?"
        if status != "PASSED" and h.get("testrayBuildId") in abandoned:
            return "X"
        return status[0]

    letters = "".join(letter(h) for h in history)
    red = 0
    for c in letters:
        if c == "P":
            break
        if c != "X":
            red += 1
    counted = letters.replace("X", "")
    flips = sum(1 for a, b in zip(counted, counted[1:]) if (a == "P") != (b == "P"))
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
    _print_open_prs(key, sorted(_tests_listed_in_ticket(text)))

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


# The team opens its PRs on this fork before they go to the main repo.
_TEAM_PR_REPO = ("liferay-commerce", "liferay-portal")


def _test_search_term(test_name):
    """The part of a Testray test name a PR is likely to quote: the Poshi
    command (after '#'), the Java class's simple name, the Playwright test
    title (cut at a word, GitHub caps the whole query at 256 characters)."""
    if "#" in test_name:
        return test_name.split("#", 1)[1]
    if " > " in test_name:
        title = test_name.split(" > ", 1)[1]
        return title if len(title) <= 70 else title[:70].rsplit(" ", 1)[0]
    if re.fullmatch(r"com\.liferay(\.\w+)+", test_name):
        return test_name.rsplit(".", 1)[-1]
    return None


def _print_open_prs(key, tests):
    """Open PRs that name the ticket or one of its tests. A fix often comes
    under another ticket: on 2026-11-19 the fix of LPD-114527 was PR
    liferay-commerce#8503 (LPD-115317, migration of the test), which names
    the Poshi test and not the routine ticket; the triage found it only by
    searching the author's PRs by hand. Without it the triage would judge
    a ticket someone is already closing."""
    terms = [key] + [t for t in dict.fromkeys(_test_search_term(t) for t in tests) if t]
    prs = search_open_prs(*_TEAM_PR_REPO, terms)
    print(f"  Open PRs naming the ticket or a test ({len(prs)}):")
    for pr in prs:
        print(f"    #{pr['number']} {pr['author']}: {pr['title'][:100]} {pr['url']}")


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


ACCEPTANCE_HISTORY_LENGTH = 6
ACCEPTANCE_HISTORY_TESTS = 3


def acceptance_report(build_id=None):
    """The open routine tickets read against the latest Acceptance build.

    The routines run every other day, Acceptance every day, and they share
    most tests. On a day with no new routine build the triage used to stop;
    on 2026-11-19 this check, done by hand on Acceptance 568532076 (464
    commits ahead of the routines), showed in a few minutes: the fix of
    LPD-116295 already green (LPD-116296 passed), the returns bug of
    LPD-116292 pinned on LPD-46122 (11 passes in Acceptance before it, red
    after), and LPD-116293 green in every Acceptance run (fragile in the
    routine only).

    What it does not do: close or comment. A green test in Acceptance is
    not a green test in the routine (different batches, timings, and
    LPD-114527 has been red in Commerce and green in Acceptance on the same
    SHA); closures stay with the analyzer on the routine's own build. A
    test missing from Acceptance (Poshi tests run only in the routine, AI
    Hub tests skipped there) says nothing either way."""
    builds = get_routine_to_builds(ACCEPTANCE_ROUTINE_ID)
    if build_id is None:
        # The newest Acceptance build is often still importing: take the
        # newest DONE one rather than giving up like the analyzer does.
        build = next(b for b in builds if (b.get("importStatus") or {}).get("key") == "DONE")
    else:
        build = next(b for b in builds if b["id"] == build_id)
    acceptance_sha = build.get("gitHash") or ""
    print(f"=== Acceptance build {build['id']} ({build.get('name')}) {testray_build_url(build['id'])}")
    print(f"SHA {acceptance_sha[:12]}")

    for routine in ANALYZED_ROUTINES:
        latest = _get_latest_done_build(get_routine_to_builds(routine["id"]), quiet=True)
        if not latest:
            continue
        routine_sha = latest.get("gitHash") or ""
        try:
            if is_ancestor("liferay", "liferay-portal", routine_sha, acceptance_sha):
                ahead = get_compare_commits("liferay", "liferay-portal", routine_sha, acceptance_sha)["total_commits"]
                where = f"Acceptance is {ahead} commits ahead"
            else:
                where = "Acceptance does NOT contain it: what follows says little about newer commits"
        except Exception as e:
            where = f"could not compare ({e})"
        print(f"{routine['name']} latest build {latest['id']} SHA {routine_sha[:12]}: {where}")

    index = {}
    for result in get_all_build_case_results(build["id"], include_case=True):
        name = (result.get("r_caseToCaseResult_c_case") or {}).get("name")
        if name:
            index[name] = result

    labels = ", ".join(f'"{r["routine_label"]}"' for r in ANALYZED_ROUTINES)
    issues = get_all_issues(
        f"labels in ({labels}) AND statusCategory != Done ORDER BY key DESC",
        fields=["summary", "status", "assignee"],
    )
    print(f"Open routine tickets: {len(issues)}\n")
    for issue in issues:
        text = get_issue_text(issue.key)
        ticket_errors = {e.strip() for e in _JIRA_CODE_BLOCK_RE.findall(text)}
        tests = sorted(_tests_listed_in_ticket(text))
        found = {t: index[t] for t in tests if t in index}
        statuses = Counter((r.get("dueStatus") or {}).get("key") or "UNTESTED" for r in found.values())
        ran = {s for s in statuses if s != "UNTESTED"}
        if not ran:
            # LPD-99531: AI Hub tests are skipped in Acceptance too; an
            # UNTESTED row is not a red test.
            verdict = "not run in Acceptance: nothing to say"
        elif ran == {"PASSED"} and len(found) == len(tests):
            verdict = "GREEN in Acceptance: fix already on master, or red in the routine only"
        elif ran == {"PASSED"}:
            # LPD-114526: 1 of its 3 tests is in Acceptance; a green victim
            # of a dirty instance does not say the instance is clean.
            verdict = "green where present, the other tests are not in Acceptance"
        elif "PASSED" in ran:
            verdict = "partly green"
        else:
            verdict = "still red"
        assignee = getattr(issue.fields.assignee, "displayName", None) or "unassigned"
        print(f"{issue.key} [{issue.fields.status.name}, {assignee}] {issue.fields.summary[:110]}")
        print(
            f"  {verdict}; {len(found)} of {len(tests)} tracked tests in Acceptance"
            f"{' (' + ', '.join(f'{k} {v}' for k, v in statuses.most_common()) + ')' if statuses else ''}"
        )
        for t, r in list(found.items())[:ACCEPTANCE_HISTORY_TESTS]:
            status = (r.get("dueStatus") or {}).get("key") or "UNTESTED"
            history = sorted(
                fetch_case_results(r.get("r_caseToCaseResult_c_caseId"), ACCEPTANCE_ROUTINE_ID),
                key=lambda h: h.get("executionDate") or "",
                reverse=True,
            )[:ACCEPTANCE_HISTORY_LENGTH]
            # Dated letters, newest first: where a P turns into an F is the
            # boundary to place against the suspect commit (LPD-116292).
            line = " ".join(f"{(h.get('executionDate') or '')[5:10]}:{(h.get('status') or '?')[0]}" for h in history)
            print(f"    {t[:100]}\n      {status}; Acceptance history {line}")
            error = (r.get("errors") or "").strip()
            if status != "PASSED" and error and error not in ticket_errors:
                print(f"      error: {' '.join(error.split())[:200]}")
        _print_open_prs(issue.key, tests)
        print()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "ticket":
        for k in sys.argv[2:]:
            ticket_report(k)
    elif len(sys.argv) >= 3 and sys.argv[1] == "build":
        build_report(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else None)
    elif len(sys.argv) >= 2 and sys.argv[1] == "acceptance":
        acceptance_report(int(sys.argv[2]) if len(sys.argv) > 2 else None)
    else:
        sys.exit(__doc__)
