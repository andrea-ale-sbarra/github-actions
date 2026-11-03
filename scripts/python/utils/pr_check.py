"""PR review check.

For every Jira ticket carrying the routine's `check_label` (`commerce_check_failures`
/ `um_check_failures`), this module:

  1. Scans the ticket's comments for the "View total diff" link the final
     reviewer leaves at merge time. The link has the form
     `github.com/<owner>/<repo>/compare/<base>...<head>` and gives us the
     post-merge HEAD SHA + the explicit list of commits in the range.
  2. Calls GitHub's `compare` endpoint to get the files that range
     touched.
  3. Finds a DONE Testray build whose `gitHash` has the merge HEAD SHA
     as an ancestor — verified via GitHub's compare API against
     `liferay/liferay-portal`. Both Acceptance and the per-routine
     pipelines (Commerce / UM) sample master at scheduled intervals, so
     an exact `gitHash == HEAD SHA` match would miss most merges; the
     ancestry check picks a build whose snapshot actually included the
     merge.

     Which routines we consult, in what order, and whether we pick the
     earliest or latest matching build is decided by the caller's mode:
       • `MODE_FULL` (called alongside the failure analysis): try the
         ticket's own routine first, fall back to Acceptance. Picks the
         earliest matching build — the first post-merge sample, useful
         for "did my merge break anything?".
       • `MODE_STANDALONE` (manual re-check after a fresh Acceptance
         build): try Acceptance first, fall back to the ticket's
         routine. Picks the latest matching build — the freshest
         verdict.
  4. For every test file in the diff, reports the status of ALL its
     tests that ran in that build (PASSED / FAILED / BLOCKED / ...), or
     NOT RUN when none of them did.

The output is purely read-only and Slack-ready. Used both by the main run
(`analyze_testray_results.py`) and the standalone command
(`check_pr_failures.py`).
"""

import os
import re
import sys

# Make sibling modules in this `utils/` package importable bare (the rest
# of the project pulls them this way; see testray_helpers.py for the same
# pattern). Doing it here lets pr_check be imported in any order without
# depending on testray_helpers having been loaded first.
sys.path.append(os.path.dirname(__file__))

from jira_helpers import get_all_issues, jira_issue_url
from testray_api import (
    find_build_case_results_by_case_name,
    get_routine_to_builds,
    testray_build_url,
    testray_case_result_url,
    ACCEPTANCE_ROUTINE_ID,
    LIFERAY_PORTAL_PROJECT_ID,
)
from github_api import get_compare, is_ancestor

# Modes for `check_pr_failures_for_routine`. They differ along two axes:
#
#   * routine preference — which routine to consult first for the build
#     that contains the merge SHA;
#   * build ordering — among the candidate builds containing the merge,
#     pick the earliest (first post-merge sample) or the latest (most
#     recent verdict).
#
# Full-analysis mode runs once per day alongside the failure analysis: the
# ticket's own routine (Commerce / UM) is the natural place to look, and
# the *earliest* sample post-merge isolates the verdict from later
# unrelated changes — useful for "did my merge break anything?".
#
# Standalone mode is typically launched manually after a fresh Acceptance
# build completes, to re-check tickets the morning analysis already saw:
# Acceptance is the authoritative gate, and the *latest* matching build
# gives the freshest verdict.
MODE_FULL = "full"
MODE_STANDALONE = "standalone"

# Acceptance runs on the upstream Liferay portal repo; ancestry checks
# against the build's `gitHash` therefore go through this owner/repo.
# Brian's fork and liferay-commerce fast-forward into upstream, so a
# `head_sha` taken from a fork's compare URL exists here under the same
# SHA once the merge has propagated.
_UPSTREAM_OWNER = "liferay"
_UPSTREAM_REPO = "liferay-portal"


# The final reviewer's "Merged. Thank you." message contains a hyperlinked
# `View total diff: <base>...<head>` snippet that points at GitHub's
# compare view on their fork. That URL is the single source of truth for
# this check — it identifies the merge, the HEAD SHA, and (via the
# compare endpoint) the files that landed.
_COMPARE_URL_RE = re.compile(
    r"https?://github\.com/([^/\s]+)/([^/\s]+)/compare/"
    r"([0-9a-f]{4,40})\.{2,3}([0-9a-f]{4,40})",
    re.IGNORECASE,
)

# Test files in the merge diff. Any change to one of these puts ALL of its
# tests in the check, whatever the change was (new test, edited test,
# renamed title): the reader opens the build and finds the test they
# care about, so we don't try to work out which test the diff touched.
#   • Java integration / unit:   *Test.java, *IT.java
#   • Playwright / Jest / Vitest: *.spec.{ts,tsx,js,jsx,mjs},
#                                 *.test.{ts,tsx,js,jsx,mjs}
#   • Poshi (Liferay):           *.testcase
# Helpers next to the tests (fixtures, page objects, *TestUtil.java) are
# not test files: they have no case of their own in Testray.
_TEST_FILENAME_RE = re.compile(
    r"(?:"
    r"(?:Test|IT)\.java"          # Java integration/unit
    r"|\.(?:spec|test)\.(?:ts|tsx|js|jsx|mjs)"   # JS/TS test runners
    r"|\.testcase"                # Poshi
    r")$"
)

# Java tests under the Maven/Gradle layout: the dotted FQN is the
# Testray case name.
_JAVA_FQN_PATH_RE = re.compile(
    r"/src/(?:test|testIntegration|main)/java/(.+)\.java$"
)

# Playwright case names start with the spec path relative to this
# folder, e.g. "commerce/commerce-product-definitions-web/main/
# commerceAdminProducts.spec.ts > <test title>".
_PLAYWRIGHT_TESTS_DIR = "/playwright/tests/"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_pr_failures_for_routine(routine, mode=MODE_FULL):
    """Run the PR review check for one routine.

    `mode` controls which routine is consulted first and whether the
    earliest or latest matching build is picked. See the `MODE_FULL` /
    `MODE_STANDALONE` constants for the rationale.

    Returns a list of per-ticket result dicts (see `_check_ticket` for the
    shape) — possibly empty if the routine has no `check_label` configured
    or no tickets are currently tagged with it.
    """
    label = routine.get("check_label")
    if not label:
        return []

    print(
        f"\n=== PR review check: routine '{routine['name']}' "
        f"(label={label}, mode={mode}) ===\n"
    )

    issues = _fetch_tagged_issues(label)
    if not issues:
        print(f"  (no Jira tickets carry the '{label}' label)")
        return []

    print(f"  Found {len(issues)} ticket(s) to check.\n")

    # Both the ticket's own routine and Acceptance can contain the
    # build we want; load both once and pass them down. The lookup
    # order (and whether to pick earliest vs latest) depends on `mode`.
    routine_lookup_order, prefer_latest = _resolve_routine_lookup(routine, mode)

    builds_by_routine = {}
    for rid, rname in routine_lookup_order:
        print(
            f"  Loading {rname} build list (routine {rid})...",
            flush=True,
        )
        builds_by_routine[rid] = get_routine_to_builds(rid) or []
        print(
            f"  → {len(builds_by_routine[rid])} {rname} build(s) loaded.",
            flush=True,
        )
    print()

    results = []
    for issue in issues:
        result = _check_ticket(
            issue, builds_by_routine, routine_lookup_order, prefer_latest
        )
        results.append(result)
        _log_ticket_result(result)
    return results


def _resolve_routine_lookup(routine, mode):
    """Return `(routine_lookup_order, prefer_latest)` for `mode`.

    `routine_lookup_order` is a list of `(routine_id, routine_name)`
    pairs in the order the PR check should consult them when searching
    for a build containing the merge SHA.
    """
    ticket_routine = (routine["id"], routine["name"])
    acceptance_routine = (ACCEPTANCE_ROUTINE_ID, "Acceptance")

    if mode == MODE_FULL:
        return [ticket_routine, acceptance_routine], False
    if mode == MODE_STANDALONE:
        return [acceptance_routine, ticket_routine], True
    raise ValueError(f"Unknown PR check mode: {mode!r}")


def format_pr_check_block(routine, results):
    """Build the plain-text lines for one routine's PR check.

    Returns an empty list if there is nothing to report (no tagged tickets).
    """
    if not results:
        return []

    lines = [f"{routine['name']} - PR review check"]

    for r in results:
        ticket_line = (
            f"- {r['ticket_key']} ({r['ticket_url']}): {r['ticket_summary']}"
        )

        if r["status"] == "skipped":
            reason = _SKIP_REASON_LABELS.get(r["skip_reason"], r["skip_reason"])
            ticket_line += f" - skipped: {reason}"
            # Surface whatever we did collect before giving up; helps the
            # reader spot e.g. "wait, the SHA should be in Acceptance".
            diag = []
            if r.get("compare_url"):
                diag.append(f"merge diff: {r['compare_url']}")
            if r.get("commit_sha"):
                diag.append(f"SHA {r['commit_sha'][:12]}")
            if diag:
                ticket_line += " (" + " | ".join(diag) + ")"
            lines.append(ticket_line)
            continue

        sha_short = (r.get("commit_sha") or "")[:12] or "?"
        compare_url = r.get("compare_url") or ""
        build_url = r.get("build_url") or ""
        build_id = r.get("build_id")
        routine_name = r.get("routine_name") or "Acceptance"

        meta_parts = []
        if compare_url:
            meta_parts.append(f"merge diff: {compare_url}")
        meta_parts.append(f"SHA {sha_short}")
        if build_url:
            meta_parts.append(f"{routine_name} build {build_id}: {build_url}")
        lines.append(ticket_line + " - " + " | ".join(meta_parts))

        # One line per test file with its counts, then the tests that
        # did not pass. The passed ones are in the build link above.
        by_file = {}
        for tr in r["test_results"]:
            by_file.setdefault(tr["test_file"], []).append(tr)
        for test_file, rows in by_file.items():
            lines.append(f"    {test_file}: {_format_status_counts(rows)}")
            for tr in rows:
                if tr["case_name"] is None or tr["status"] == "PASSED":
                    continue
                label = f"[{tr['status']}]"
                if tr.get("caseresult_url"):
                    lines.append(
                        f"        {label} {tr['case_name']} ({tr['caseresult_url']})"
                    )
                else:
                    lines.append(f"        {label} {tr['case_name']}")

    lines.append("")
    return lines


def print_pr_check_standalone(pr_check_by_routine):
    """Print the PR check report as a plain-text block, on its own.

    Used by the `check_pr_failures.py` script when the user wants the
    report without re-running the full testflow analysis.
    """
    blocks = []
    for routine, results in pr_check_by_routine:
        blocks.extend(format_pr_check_block(routine, results))

    if not blocks:
        print("\n(no routine had any ticket tagged for PR review check)")
        return

    message = "\n".join(
        [
            "Hi all,",
            "",
            "PR review check - status of the tests touched by the merged PRs:",
            "",
            *blocks,
            "Thanks!",
        ]
    )

    separator = "-" * 70
    print()
    print(separator)
    print("Plain-text PR check (copy-paste below):")
    print(separator)
    print(message)
    print(separator)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

_SKIP_REASON_LABELS = {
    "no_merge_diff": "no 'View total diff' link found on the ticket",
    "no_test_files": "merge diff contains no test files",
    "build_not_found": "no DONE build (routine or Acceptance) after the merge yet",
    "github_error": "GitHub API error",
}


def _fetch_tagged_issues(label):
    jql = (
        f'project = LPD AND labels = "{label}" '
        f'AND statusCategory != Done '
        f"ORDER BY updated DESC"
    )
    return get_all_issues(jql, fields="summary,comment,labels,status")


def _check_ticket(issue, builds_by_routine, routine_lookup_order, prefer_latest):
    """Compute the PR-check verdict for a single Jira issue.

    Returned shape:
        {
            "ticket_key": str,
            "ticket_summary": str,
            "ticket_url": str,
            "status": "checked" | "skipped",
            "skip_reason": str | None,
            "compare_url": str | None,
            "commit_sha": str | None,  # HEAD of the merge diff
            "build_id": int | None,
            "build_url": str | None,
            "routine_id": int | None,    # routine the matched build lives in
            "routine_name": str | None,
            "test_results": [
                {"test_file": str, "case_name": str | None, "status": str,
                 "caseresult_url": str | None}
            ],
        }
    """
    key = issue.key
    summary = (getattr(issue.fields, "summary", "") or "").strip()
    base = {
        "ticket_key": key,
        "ticket_summary": summary,
        "ticket_url": jira_issue_url(key),
        "status": "skipped",
        "skip_reason": None,
        "compare_url": None,
        "commit_sha": None,
        "build_id": None,
        "build_url": None,
        "routine_id": None,
        "routine_name": None,
        "test_results": [],
    }

    print(f"\n  ▶ {key}: {summary}", flush=True)

    diff = _extract_merge_diff_from_issue(issue)
    if not diff:
        print("    · no 'View total diff' link in comments — skip", flush=True)
        base["skip_reason"] = "no_merge_diff"
        return base

    owner, repo, base_sha, head_sha = diff
    base["compare_url"] = (
        f"https://github.com/{owner}/{repo}/compare/{base_sha}...{head_sha}"
    )
    base["commit_sha"] = head_sha
    print(
        f"    · merge diff: {owner}/{repo} {base_sha[:7]}...{head_sha[:7]}",
        flush=True,
    )

    try:
        print("    · calling GitHub compare API for files + commit date...", flush=True)
        cmp_info = get_compare(owner, repo, base_sha, head_sha)
    except Exception as e:
        print(f"    ✘ GitHub error: {e}", flush=True)
        base["skip_reason"] = "github_error"
        return base

    if cmp_info.get("truncated"):
        print(
            "    ⚠ compare returned the max 300 files; some test files "
            "may be missing from the match.",
            flush=True,
        )

    test_files = _derive_test_files(cmp_info.get("files") or [])
    print(
        f"    · {len(cmp_info.get('files') or [])} file(s) in diff, "
        f"{len(test_files)} test file(s) detected",
        flush=True,
    )
    if not test_files:
        base["skip_reason"] = "no_test_files"
        return base

    # Neither Acceptance nor Commerce/UM runs on every commit — they
    # sample master at scheduled intervals. The relevant build for this
    # merge is a DONE build whose snapshot includes the merge commit,
    # which we verify by asking GitHub whether `head_sha` is reachable
    # from the build's `gitHash`. We try the routines in the order set
    # by the calling mode and pick earliest or latest accordingly.
    commit_date = cmp_info.get("head_commit_date")
    selection = "latest" if prefer_latest else "earliest"
    routines_blurb = " then ".join(name for _, name in routine_lookup_order)
    print(
        f"    · finding {selection} DONE build containing "
        f"{head_sha[:7]} (post-{commit_date}); order: {routines_blurb}...",
        flush=True,
    )
    match = _find_build_containing(
        head_sha,
        commit_date,
        builds_by_routine,
        routine_lookup_order,
        prefer_latest,
    )
    if not match:
        print(
            "    · no build whose gitHash contains the merge yet (in any "
            "of the consulted routines) — skip",
            flush=True,
        )
        base["skip_reason"] = "build_not_found"
        return base

    build_id, routine_id, routine_name = match
    base["build_id"] = build_id
    base["routine_id"] = routine_id
    base["routine_name"] = routine_name
    base["build_url"] = testray_build_url(build_id, routine_id=routine_id)
    print(
        f"    · matched {routine_name} build {build_id} → {base['build_url']}",
        flush=True,
    )

    print(
        f"    · looking up test cases in build {build_id} (targeted query)...",
        flush=True,
    )
    base["test_results"] = _collect_test_results(
        build_id, test_files, routine_id
    )
    base["status"] = "checked"
    base["skip_reason"] = None
    return base


def _extract_merge_diff_from_issue(issue):
    """Return `(owner, repo, base_sha, head_sha)` from the most recent
    Jira comment containing a GitHub `compare/<base>...<head>` URL, or
    `None`.
    """
    comments = []
    try:
        comments = list(getattr(issue.fields, "comment", None).comments or [])
    except AttributeError:
        comments = []

    # Newest first: if a ticket was merged in multiple rounds, we want the
    # latest "Merged. Thank you." message.
    comments.sort(key=lambda c: getattr(c, "created", "") or "", reverse=True)

    for c in comments:
        body = _comment_body_text(c)
        if not body:
            continue
        match = _COMPARE_URL_RE.search(body)
        if match:
            return (
                match.group(1),
                match.group(2),
                match.group(3),
                match.group(4),
            )
    return None


def _comment_body_text(comment):
    """Return a flat-text version of a Jira comment body.

    Jira returns the body either as a plain string or as an ADF (Atlassian
    Document Format) dict, depending on the API version. We flatten both
    forms — and crucially, we extract `href` from `link` marks so the
    "View total diff: <SHA>...<SHA>" hyperlink contributes its URL even
    when the visible text only shows the short SHA range.
    """
    body = getattr(comment, "body", None)
    if body is None:
        return ""
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        return _adf_to_text(body)
    return str(body)


def _adf_to_text(node):
    """Flatten an Atlassian Document Format node into plain text + URLs."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""

    parts = []
    if node.get("type") == "text":
        parts.append(node.get("text", ""))
    for mark in node.get("marks") or []:
        if mark.get("type") == "link":
            href = (mark.get("attrs") or {}).get("href")
            if href:
                parts.append(href)
    for child in node.get("content") or []:
        parts.append(_adf_to_text(child))
    return " ".join(p for p in parts if p)


def _derive_test_files(changed_files):
    """Return `{display_name: case_name_key}` for the test files in the diff.

    `case_name_key` is the piece every Testray case of that file has in
    its name, so a "contains" search returns all of the file's tests:

      • Java: the dotted FQN ("com.liferay....ShipmentResourceTest").
      • Poshi: "LocalFile.<Name>#" ("LocalFile.CommerceSmoke#Publish...").
      • Playwright: the spec path under `playwright/tests/`.
      • Other JS test files: the file name.
    """
    files = {}
    for f in changed_files:
        filename = (f.get("filename") or "").strip()
        if not filename or not _TEST_FILENAME_RE.search(filename):
            continue
        base = filename.rsplit("/", 1)[-1]

        fqn_match = _JAVA_FQN_PATH_RE.search(filename)
        if fqn_match:
            fqn = fqn_match.group(1).replace("/", ".")
            files[fqn] = fqn
        elif base.endswith(".testcase"):
            files[base] = f"LocalFile.{base[:-len('.testcase')]}#"
        elif _PLAYWRIGHT_TESTS_DIR in filename:
            spec = filename.split(_PLAYWRIGHT_TESTS_DIR, 1)[1]
            files[spec] = spec
        else:
            files[base] = base
    return files


def _matches_key(case_name, key):
    """True when `case_name` belongs to the file `key` and not to a
    longer name that merely contains it (FooTest vs FooTestCase)."""
    if not case_name.startswith(key):
        return False
    rest = case_name[len(key):]
    return not rest or key.endswith("#") or not (rest[0].isalnum() or rest[0] == "_")


def _find_build_containing(
    head_sha, commit_date, builds_by_routine, routine_lookup_order, prefer_latest
):
    """Return `(build_id, routine_id, routine_name)` for the first
    DONE build whose `gitHash` has `head_sha` as an ancestor — i.e. a
    build whose master snapshot actually contains the merge — searched
    in the routine order set by the caller.

    For each routine in `routine_lookup_order`:

      1. Pre-filter that routine's build list to DONE entries with
         `dueDate >= commit_date` and a non-empty `gitHash`. Anything
         earlier can't possibly contain the merge.
      2. Sort by `dueDate` ascending (then `prefer_latest=False` picks
         the earliest match) or descending (`prefer_latest=True` picks
         the latest), and for each candidate ask GitHub's compare
         endpoint whether `head_sha` is reachable from `gitHash`.
      3. The first build for which it is wins. Move on to the next
         routine only when this routine has no match.

    Why ancestry instead of `dueDate >= commit_date` alone: `dueDate` is
    the *scheduled* run time, not the moment master was pulled. If the
    scheduler ran late, or the committer date is antedated, a date-only
    match can pick a build whose snapshot predates the merge — and the
    report would then show PASSED for tests that haven't actually run on
    the fix yet.

    `builds_by_routine` is keyed by routine id and holds the cached lists
    passed by the caller so we don't paginate through hundreds of builds
    once per ticket.
    """
    if not commit_date:
        return None

    for routine_id, routine_name in routine_lookup_order:
        builds = builds_by_routine.get(routine_id) or []
        candidates = [
            b for b in builds
            if (b.get("dueDate") or "") >= commit_date
            and (b.get("importStatus") or {}).get("key") == "DONE"
            and b.get("gitHash")
        ]
        if not candidates:
            print(
                f"    · no DONE {routine_name} build dated after "
                f"{commit_date}; trying next routine",
                flush=True,
            )
            continue

        candidates.sort(
            key=lambda b: b.get("dueDate") or "", reverse=prefer_latest
        )

        matched = None
        for b in candidates:
            git_hash = b["gitHash"]
            build_id = b.get("id")
            try:
                contains = is_ancestor(
                    _UPSTREAM_OWNER, _UPSTREAM_REPO, head_sha, git_hash
                )
            except Exception as e:
                print(
                    f"    · ancestry check failed for {routine_name} build "
                    f"{build_id} (gitHash {git_hash[:7]}): {e}; trying next",
                    flush=True,
                )
                continue
            if contains:
                matched = build_id
                break
            print(
                f"    · {routine_name} build {build_id} "
                f"(gitHash {git_hash[:7]}, dueDate {b.get('dueDate')}) "
                f"does not yet contain {head_sha[:7]}; trying next",
                flush=True,
            )

        if matched is not None:
            return matched, routine_id, routine_name

        print(
            f"    · no {routine_name} build whose gitHash contains the "
            f"merge yet; trying next routine",
            flush=True,
        )

    return None


def _collect_test_results(build_id, test_files, routine_id):
    """Return one row per test of each changed test file that ran in
    `build_id`, plus a NOT RUN row for a file with no test in the build
    (deleted file, test not part of this routine, case not yet created).

    Row shape: {"test_file", "case_name", "status", "caseresult_url"}.
    One row per case name: a case that ran more than once keeps the
    worst status, so a FAILED retry is never hidden by a PASSED one.
    """
    out = []
    for display_name, key in sorted(test_files.items()):
        print(f"    · tests of {display_name} in build {build_id}...", flush=True)
        by_name = {}
        for cr in find_build_case_results_by_case_name(build_id, key):
            name = cr["case_name"]
            if not _matches_key(name, key):
                continue
            current = by_name.get(name)
            if current and current["status"] != "PASSED":
                continue
            by_name[name] = {
                "test_file": display_name,
                "case_name": name,
                "status": cr["status"],
                "caseresult_url": testray_case_result_url(
                    cr["id"],
                    project_id=LIFERAY_PORTAL_PROJECT_ID,
                    routine_id=routine_id,
                    build_id=build_id,
                ) if cr.get("id") else None,
            }
        if not by_name:
            out.append({
                "test_file": display_name,
                "case_name": None,
                "status": "NOT RUN",
                "caseresult_url": None,
            })
        out.extend(sorted(by_name.values(), key=lambda e: e["case_name"]))
    return out


def _log_ticket_result(r):
    key = r["ticket_key"]
    if r["status"] == "skipped":
        reason = _SKIP_REASON_LABELS.get(r["skip_reason"], r["skip_reason"])
        print(f"  - {key}: skipped ({reason})")
        return

    pretty = _format_status_counts(r["test_results"])
    routine_name = r.get("routine_name") or "Acceptance"
    print(
        f"  - {key}: {routine_name} build {r['build_id']} "
        f"(SHA {(r['commit_sha'] or '')[:12]}) → {pretty}"
    )


def _format_status_counts(rows):
    """"1 FAILED, 10 PASSED" for a set of result rows; failures first."""
    if rows and all(tr["case_name"] is None for tr in rows):
        return "NOT RUN in this build"
    counts = {}
    for tr in rows:
        counts[tr["status"]] = counts.get(tr["status"], 0) + 1
    order = sorted(counts, key=lambda k: (k == "PASSED", k))
    return ", ".join(f"{counts[k]} {k}" for k in order) or "no tests"
