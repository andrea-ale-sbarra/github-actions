"""Thin GitHub REST API wrapper used by the PR review check.

We only call one endpoint: `compare/{base}...{head}`. The PR review check
identifies the merge by parsing the "View total diff" link that the final
reviewer leaves on the Jira ticket (form `<base>...<head>`), and the
compare endpoint gives us back the list of files that range touched.

Auth: a `GITHUB_TOKEN` environment variable. The repos involved
(brianchandotcom/liferay-portal, liferay-commerce/liferay-portal,
liferay/liferay-portal) are public, so any valid PAT works — no scopes
needed; authenticating just lifts the rate limit from 60 to 5000 req/h.
"""

import os
import time
from functools import lru_cache

import requests
from requests.exceptions import RequestException

GITHUB_API = "https://api.github.com"


def get_compare(owner, repo, base, head):
    """Return the file diff and metadata for `base...head`.

    Shape (only the fields we care about):
        {
            "html_url": str,
            "status": "ahead" | "behind" | "identical" | "diverged",
            "total_commits": int,
            "files": [{"filename": str, "status": str}, ...],
            "truncated": bool,
            "head_commit_date": str | None,   # ISO 8601, committer date
        }

    GitHub caps the `files` list at 300 by default; this is plenty for the
    typical Liferay merge but we expose `truncated` so callers can warn
    when a merge is unusually large.

    `head_commit_date` is the committer date of the last commit in the
    `base...head` range. Callers use it to find the first Acceptance
    build whose snapshot was taken *after* the merge landed on master.
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/compare/{base}...{head}"
    payload = _get_json(url) or {}
    files = [
        {"filename": f.get("filename"), "status": f.get("status")}
        for f in (payload.get("files") or [])
    ]
    commits = payload.get("commits") or []
    head_commit_date = None
    if commits:
        head_commit_date = (
            (((commits[-1] or {}).get("commit") or {}).get("committer") or {})
            .get("date")
        )
    if not head_commit_date:
        head_commit_date = _get_commit_committer_date(owner, repo, head)
    return {
        "html_url": payload.get("html_url"),
        "status": payload.get("status"),
        "total_commits": payload.get("total_commits"),
        "files": files,
        "truncated": len(files) >= 300,
        "head_commit_date": head_commit_date,
    }


def _get_commit_committer_date(owner, repo, sha):
    """One-shot fallback for when the compare range carries no commits."""
    try:
        payload = _get_json(f"{GITHUB_API}/repos/{owner}/{repo}/commits/{sha}")
    except Exception:
        return None
    return (((payload or {}).get("commit") or {}).get("committer") or {}).get("date")


@lru_cache(maxsize=4096)
def is_ancestor(owner, repo, ancestor_sha, descendant_sha):
    """Return True iff `ancestor_sha` is reachable from `descendant_sha`
    in <owner>/<repo>'s commit history.

    Uses GitHub's compare endpoint, queried as `compare/{base}...{head}`
    with base=ancestor and head=descendant. The `status` field describes
    `head` relative to `base`:
      - "ahead"     → head has commits base doesn't (base IS reachable
                      from head) → True.
      - "identical" → same commit → True.
      - "behind"    → head is missing commits base has (the relationship
                      is the reverse of what we asked) → False.
      - "diverged"  → both have unique commits → False.

    A 404 (either SHA absent from the repo, or no common ancestor at all)
    is treated as "not an ancestor" and returned as False rather than
    propagated — the caller would handle it the same way either way.

    Cached: a (ancestor, descendant) verdict is immutable, and the PR
    review check tends to revisit the same pairs across tickets / runs.
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/compare/{ancestor_sha}...{descendant_sha}"
    try:
        payload = _get_json(url) or {}
    except requests.HTTPError as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status == 404:
            return False
        raise
    return payload.get("status") in ("identical", "ahead")


def _get_json(url, max_retries=3):
    last_exception = None
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=_get_headers(), timeout=30)
            if response.status_code == 403 and "rate limit" in response.text.lower():
                reset = response.headers.get("X-RateLimit-Reset")
                wait = 30
                if reset and reset.isdigit():
                    wait = max(5, int(reset) - int(time.time()))
                print(f"⚠ GitHub rate limit hit, sleeping {wait}s before retry...")
                time.sleep(min(wait, 120))
                continue
            response.raise_for_status()
            return response.json()
        except RequestException as e:
            last_exception = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status and 400 <= status < 500:
                raise
            if attempt < max_retries - 1:
                wait = (attempt + 1) * 5
                print(
                    f"GitHub GET attempt {attempt + 1} failed ({e}). "
                    f"Retrying in {wait}s..."
                )
                time.sleep(wait)
            else:
                print(f"All GitHub retries exhausted for {url}.")
    if last_exception:
        raise last_exception
    raise RuntimeError(
        f"Failed to fetch JSON from {url} after {max_retries} attempts."
    )


@lru_cache()
def _get_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        print(
            "⚠ GITHUB_TOKEN not set: GitHub API calls will be rate-limited "
            "to 60 req/h. Add a personal access token to .env to lift this."
        )
    return headers
