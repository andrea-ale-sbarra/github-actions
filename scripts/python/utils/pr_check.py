"""PR review check.

For every Jira ticket carrying the routine's `check_label` (`commerce_check_failures`
/ `um_check_failures`), this module:

  1. Scans the ticket's comments for the "View total diff" link the final
     reviewer leaves at merge time. The link has the form
     `github.com/<owner>/<repo>/compare/<base>...<head>` and gives us the
     post-merge HEAD SHA + the explicit list of commits in the range.
  2. Calls GitHub's `compare` endpoint to get the files that range
     touched.
  3. Finds the earliest DONE Acceptance build whose `gitHash` has the
     merge HEAD SHA as an ancestor — verified via GitHub's compare API
     against `liferay/liferay-portal`. Acceptance samples master rather
     than running on every commit, so an exact `gitHash == HEAD SHA`
     match would miss most merges; the ancestry check picks the first
     build whose snapshot actually included the merge.
  4. Matches the test files in the diff against the case names that ran
     in that build and reports each one's status (PASSED / FAILED /
     BLOCKED / NOT FOUND).

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
    find_case_by_exact_name,
    find_case_result_in_build_via_history,
    find_cases_by_name_substring,
    get_routine_to_builds,
    testray_build_url,
    testray_case_result_url,
    ACCEPTANCE_ROUTINE_ID,
    LIFERAY_PORTAL_PROJECT_ID,
)
from github_api import get_compare, is_ancestor

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

# Heuristics that pick test files out of the merge diff. Covers:
#   • Java integration / unit:   *Test.java, *IT.java, or any file under
#                                /src/test/ or /src/testIntegration/
#   • Playwright / Jest / Vitest: *.spec.{ts,tsx,js,jsx,mjs},
#                                 *.test.{ts,tsx,js,jsx,mjs},
#                                 or any file under /playwright/ /e2e/ /tests/
#   • Poshi (Liferay):           *.testcase
_TEST_FILENAME_RE = re.compile(
    r"(?:"
    r"(?:Test|IT)\.java"          # Java integration/unit
    r"|\.(?:spec|test)\.(?:ts|tsx|js|jsx|mjs)"   # JS/TS test runners
    r"|\.testcase"                # Poshi
    r")$"
)
_TEST_PATH_HINTS = (
    "/src/test/", "/src/testIntegration/", "/test/integration/",
    "/playwright/", "/e2e/", "/tests/e2e/",
)

# Extensions stripped from a non-Java test filename to build the Testray
# case-name substring. Order matters: longest first so
# `productEditor.spec.ts` loses only `.ts` and the remaining
# `productEditor.spec` is what we query.
_STRIPPABLE_EXTENSIONS = (
    ".java", ".testcase", ".tsx", ".ts", ".jsx", ".js", ".mjs",
)

# Java tests under the Maven/Gradle layout: capture the dotted FQN out
# of the path so we can ask Testray for an *exact* case-name match.
_JAVA_FQN_PATH_RE = re.compile(
    r"/src/(?:test|testIntegration|main)/java/(.+)\.java$"
)

# Playwright / Jest / Vitest spec files. For these we don't want to match
# every test in the file — we want only the test() / it() / describe()
# blocks referenced in the diff, because Testray names each case after
# the test description (e.g. "productDetails.spec.ts > LPD-39598 ...").
_JS_TEST_FILENAME_RE = re.compile(
    r"\.(?:spec|test)\.(?:ts|tsx|js|jsx|mjs)$"
)

# test('name', …) / it('name', …) / test.describe('name', …) / it.each(…)
# with single, double, or back-tick quotes. Captures the description so
# the caller can use it as the Testray case-name substring.
_JS_TEST_BLOCK_RE = re.compile(
    r"""\b(?:test|it)(?:\.[A-Za-z_][A-Za-z_0-9]*)?\s*\(\s*"""
    r"""(['"`])(.+?)\1""",
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_pr_failures_for_routine(routine):
    """Run the PR review check for one routine.

    Returns a list of per-ticket result dicts (see `_check_ticket` for the
    shape) — possibly empty if the routine has no `check_label` configured
    or no tickets are currently tagged with it.
    """
    label = routine.get("check_label")
    if not label:
        return []

    print(
        f"\n=== PR review check: routine '{routine['name']}' "
        f"(label={label}) ===\n"
    )

    issues = _fetch_tagged_issues(label)
    if not issues:
        print(f"  (no Jira tickets carry the '{label}' label)")
        return []

    print(f"  Found {len(issues)} ticket(s) to check.\n")

    # Acceptance returns the same list of builds for every ticket; fetch
    # it once and reuse it (the call paginates through hundreds of builds
    # and takes a few seconds).
    print("  Loading Acceptance build list (one-shot for all tickets)...", flush=True)
    builds_cache = get_routine_to_builds(ACCEPTANCE_ROUTINE_ID) or []
    print(f"  → {len(builds_cache)} Acceptance build(s) loaded.\n", flush=True)

    results = []
    for issue in issues:
        result = _check_ticket(issue, builds_cache)
        results.append(result)
        _log_ticket_result(result)
    return results


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

        meta_parts = []
        if compare_url:
            meta_parts.append(f"merge diff: {compare_url}")
        meta_parts.append(f"SHA {sha_short}")
        if build_url:
            meta_parts.append(f"Acceptance build {build_id}: {build_url}")
        lines.append(ticket_line + " - " + " | ".join(meta_parts))

        for tr in r["test_results"]:
            status = tr["status"]
            label = f"[{status}]"
            if tr.get("caseresult_url"):
                lines.append(
                    f"    {label} {tr['case_name']} ({tr['caseresult_url']})"
                )
            else:
                lines.append(f"    {label} {tr['case_name']}")

        if not r["test_results"]:
            lines.append("    (no matching test cases found in the build)")

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
    "build_not_found": "no DONE Acceptance build after the merge yet",
    "github_error": "GitHub API error",
}


def _fetch_tagged_issues(label):
    jql = (
        f'project = LPD AND labels = "{label}" '
        f'AND statusCategory != Done '
        f"ORDER BY updated DESC"
    )
    return get_all_issues(jql, fields="summary,comment,labels,status")


def _check_ticket(issue, builds_cache):
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
            "test_results": [
                {"case_name": str, "status": str, "caseresult_url": str | None}
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

    test_search_names = _derive_test_search_names(cmp_info.get("files") or [])
    print(
        f"    · {len(cmp_info.get('files') or [])} file(s) in diff, "
        f"{len(test_search_names)} test file(s) detected",
        flush=True,
    )
    if not test_search_names:
        base["skip_reason"] = "no_test_files"
        return base

    # Acceptance doesn't run on every commit — it samples master at
    # scheduled intervals. The relevant build for this merge is the
    # earliest DONE build whose snapshot includes the merge commit, which
    # we verify by asking GitHub whether `head_sha` is reachable from the
    # build's `gitHash`.
    commit_date = cmp_info.get("head_commit_date")
    print(
        f"    · finding earliest DONE Acceptance build containing "
        f"{head_sha[:7]} (post-{commit_date})...",
        flush=True,
    )
    build_id = _find_first_acceptance_build_containing(
        head_sha, commit_date, builds_cache
    )
    if not build_id:
        print(
            "    · no Acceptance build whose gitHash contains the merge yet "
            "— skip",
            flush=True,
        )
        base["skip_reason"] = "build_not_found"
        return base

    base["build_id"] = build_id
    base["build_url"] = testray_build_url(build_id)
    print(f"    · matched Acceptance build {build_id} → {base['build_url']}", flush=True)

    print(
        f"    · looking up test cases in build {build_id} (targeted query)...",
        flush=True,
    )
    base["test_results"] = _collect_test_results(build_id, test_search_names)
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


def _derive_test_search_names(changed_files):
    """Return the set of `(name, is_exact)` pairs to look up in Testray.

    The rule depends on framework:

      • Java tests in Maven/Gradle layout (`/src/test/java/...` or
        `/src/testIntegration/java/...`) yield the dotted FQN with
        `is_exact=True`. Testray names these cases by the FQN, so we
        can match exactly without ambiguity.
      • Playwright / Jest / Vitest spec files yield ONE entry per
        `test()` / `it()` / `describe()` block referenced in the patch
        (added, removed, or context). Testray names these cases as
        `<spec_path> > <test description>`, so searching by the test
        description finds the specific case in the diff instead of
        every test that happens to live in the same spec file. Spec
        files whose patch surfaces no `test()` block are skipped — we
        intentionally avoid the broader filename-stem fallback so the
        report stays scoped to the PR's actual diff.
      • Anything else (Poshi `.testcase`, etc.) yields the filename
        stem (extension stripped) with `is_exact=False`.
    """
    names = set()
    for f in changed_files:
        filename = (f.get("filename") or "").strip()
        if not filename:
            continue

        looks_like_test = bool(_TEST_FILENAME_RE.search(filename)) or any(
            hint in filename for hint in _TEST_PATH_HINTS
        )
        if not looks_like_test:
            continue

        fqn_match = _JAVA_FQN_PATH_RE.search(filename)
        if fqn_match:
            fqn = fqn_match.group(1).replace("/", ".")
            names.add((fqn, True))
            continue

        if _JS_TEST_FILENAME_RE.search(filename):
            for desc in _extract_js_test_descriptions(f.get("patch") or ""):
                names.add((desc, False))
            continue

        base = filename.rsplit("/", 1)[-1]
        for ext in _STRIPPABLE_EXTENSIONS:
            if base.endswith(ext):
                base = base[: -len(ext)]
                break
        if base:
            names.add((base, False))
    return names


def _extract_js_test_descriptions(patch_text):
    """Return the set of `test()` / `it()` descriptions present in the patch.

    We look at:
      - every patch line whose first character is `+`, `-`, or a space
        (i.e. added, removed, and context lines), skipping the file
        headers `+++ b/...` and `--- a/...`;
      - hunk header lines `@@ … @@ <context>`, because GitHub records
        the enclosing function declaration there and a body-only edit
        may surface its `test('…', () => {` only through that header.

    Mentioning the description on any side of the diff is enough.

    Returns descriptions verbatim. The caller does a substring match in
    Testray, so we don't need to compose the full `<path> > <desc>` name.
    """
    if not patch_text:
        return set()

    descriptions = set()
    for line in patch_text.split("\n"):
        if not line:
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("@@"):
            # Hunk header: strip the `@@ -a,b +c,d @@` prefix, scan the rest.
            tail = re.split(r"@@\s?", line, maxsplit=2)
            body = tail[-1] if tail else ""
        else:
            prefix = line[0]
            if prefix not in ("+", "-", " "):
                continue
            body = line[1:]
        for match in _JS_TEST_BLOCK_RE.finditer(body):
            desc = (match.group(2) or "").strip()
            if desc:
                descriptions.add(desc)
    return descriptions


def _find_first_acceptance_build_containing(head_sha, commit_date, builds):
    """Return the id of the earliest DONE Acceptance build whose `gitHash`
    has `head_sha` as an ancestor — i.e. the first build whose master
    snapshot actually contains the merge.

    Two-step selection to keep the GitHub call count bounded:

      1. Pre-filter the build list to DONE entries with `dueDate >=
         commit_date` and a non-empty `gitHash`. Anything earlier can't
         possibly contain the merge, and there's no point asking GitHub.
      2. Sort ascending by `dueDate` and, for each candidate, ask
         GitHub's compare endpoint whether `head_sha` is reachable from
         `gitHash`. Return the first build for which it is.

    Why ancestry instead of `dueDate >= commit_date` alone: `dueDate` is
    the *scheduled* run time, not the moment master was pulled. If the
    scheduler ran late, or the committer date is antedated, a date-only
    match can pick a build whose snapshot predates the merge — and the
    report would then show PASSED for tests that haven't actually run on
    the fix yet.

    `builds` is the cached list of Acceptance builds passed by the caller
    so we don't paginate through hundreds of builds once per ticket.
    """
    if not commit_date:
        return None

    candidates = [
        b for b in builds
        if (b.get("dueDate") or "") >= commit_date
        and (b.get("importStatus") or {}).get("key") == "DONE"
        and b.get("gitHash")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda b: b.get("dueDate") or "")

    for b in candidates:
        git_hash = b["gitHash"]
        build_id = b.get("id")
        try:
            contains = is_ancestor(
                _UPSTREAM_OWNER, _UPSTREAM_REPO, head_sha, git_hash
            )
        except Exception as e:
            print(
                f"    · ancestry check failed for build {build_id} "
                f"(gitHash {git_hash[:7]}): {e}; trying next",
                flush=True,
            )
            continue
        if contains:
            return build_id
        print(
            f"    · build {build_id} (gitHash {git_hash[:7]}, "
            f"dueDate {b.get('dueDate')}) does not yet contain "
            f"{head_sha[:7]}; trying next",
            flush=True,
        )

    return None


def _collect_test_results(build_id, test_search_names):
    """Resolve each `(name, is_exact)` pair to a Testray case-result.

    Two lookups per name:

      1. Find the Testray case. Exact-name match first (the natural fit
         for Java FQNs derived from the diff); fall back to substring
         only when no exact case is found.
      2. Walk the case's history in this build via the testray-rest
         `case-result-history` endpoint to get the case-result row + its
         id and status.

    Dedupes by `case_name` because Testray may carry several `case_id`s
    with the same name (one per method, or stale entries); the report
    keeps one row per unique name and the click-through link drills into
    the case-result entity directly.

    Surfaces NOT FOUND rows for tests we couldn't map to any Testray
    case, so silence in the report isn't read as green-by-default.
    """
    if not test_search_names:
        return []

    out_by_name = {}    # case_name → row dict
    matched_inputs = set()

    for name, is_exact in sorted(test_search_names):
        print(
            f"    · resolving Testray case for '{name}' (exact={is_exact})...",
            flush=True,
        )
        cases = []
        if is_exact:
            case = find_case_by_exact_name(name)
            if case:
                cases = [case]
        if not cases:
            # No exact hit (or non-Java input): fall back to substring on
            # the simple class name / file stem.
            substring = name.rsplit(".", 1)[-1] if is_exact else name
            cases = find_cases_by_name_substring(substring)
        if not cases:
            continue

        for case in cases:
            case_id = case.get("id")
            case_name = case.get("name") or ""
            if not case_id or not case_name:
                continue
            if case_name in out_by_name:
                matched_inputs.add((name, is_exact))
                continue

            cr = find_case_result_in_build_via_history(build_id, case_id)
            if not cr:
                # Case exists but didn't run in this build. Still count
                # the input as matched — we don't also want a NOT FOUND
                # row for it below.
                matched_inputs.add((name, is_exact))
                continue

            status = ((cr.get("dueStatus") or {}).get("name") or "UNTESTED").upper()
            case_result_id = cr.get("id")
            caseresult_url = None
            if case_result_id:
                caseresult_url = testray_case_result_url(
                    case_result_id,
                    project_id=LIFERAY_PORTAL_PROJECT_ID,
                    routine_id=ACCEPTANCE_ROUTINE_ID,
                    build_id=build_id,
                )
            out_by_name[case_name] = {
                "case_name": case_name,
                "status": status,
                "caseresult_url": caseresult_url,
            }
            matched_inputs.add((name, is_exact))

    out = list(out_by_name.values())
    for name, _is_exact in sorted(test_search_names - matched_inputs):
        out.append(
            {
                "case_name": name,
                "status": "NOT FOUND",
                "caseresult_url": None,
            }
        )

    out.sort(key=lambda e: (e["status"] != "FAILED", e["case_name"]))
    return out


def _log_ticket_result(r):
    key = r["ticket_key"]
    if r["status"] == "skipped":
        reason = _SKIP_REASON_LABELS.get(r["skip_reason"], r["skip_reason"])
        print(f"  - {key}: skipped ({reason})")
        return

    counts = {}
    for tr in r["test_results"]:
        counts[tr["status"]] = counts.get(tr["status"], 0) + 1
    pretty = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "no matching cases"
    print(
        f"  - {key}: build {r['build_id']} (SHA {(r['commit_sha'] or '')[:12]}) "
        f"→ {pretty}"
    )
