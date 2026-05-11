import base64
import os
import time
import requests

from requests.exceptions import RequestException
from functools import lru_cache
from typing import Optional

BASE_URL = "https://testray.liferay.com/o/c"
TESTRAY_REST_URL = "https://testray.liferay.com/o/testray-rest/v1.0"
TESTRAY_UI_URL = "https://testray.liferay.com/web/testray"
COMMERCE_ROUTINE_ID = 35394
USER_MANAGEMENT_ROUTINE_ID = 1874638
ACCEPTANCE_ROUTINE_ID = 590307
# All three routines above live under the same Testray project (the one
# whose URL path is `#/project/35392/...`). Hardcoded because the routine
# entity doesn't expose its parent project id under any of the obvious
# field names; if a new routine moves to a different project, override
# this here.
LIFERAY_PORTAL_PROJECT_ID = 35392
STATUS_FAILED_BLOCKED_TESTFIX = "FAILED,TESTFIX,BLOCKED"


def testray_build_url(build_id):
    return f"{TESTRAY_UI_URL}#/builds/{build_id}"


def testray_task_url(task_id):
    return f"{TESTRAY_UI_URL}#/testflow/{task_id}"


def testray_subtask_url(subtask_id, task_id=None):
    if task_id:
        return f"{TESTRAY_UI_URL}#/testflow/{task_id}/subtasks/{subtask_id}"
    return f"{TESTRAY_UI_URL}#/subtasks/{subtask_id}"


def testray_case_result_url(case_result_id, project_id=None, routine_id=None, build_id=None):
    """Build a Testray UI URL for a case result.

    When `project_id`, `routine_id` and `build_id` are all provided, use
    the full path form
        #/project/<p>/routines/<r>/build/<b>/case-result/<cr>
    which the current Testray UI actually resolves. The legacy short form
    `#/caseresults/<id>` 404s on the modern UI, so the full form is
    required for any link the user is meant to click. The short form is
    kept as a fallback for callers (e.g. DRY-RUN logs) that don't have
    the surrounding build context handy.
    """
    if project_id and routine_id and build_id:
        return (
            f"{TESTRAY_UI_URL}#/project/{project_id}"
            f"/routines/{routine_id}/build/{build_id}"
            f"/case-result/{case_result_id}"
        )
    return f"{TESTRAY_UI_URL}#/caseresults/{case_result_id}"


def testray_build_filtered_by_case_name_url(project_id, routine_id, build_id, case_name):
    """Build a Testray UI URL that lands on `build_id` with its result
    list filtered to the rows matching `case_name`.

    This is the link form the user actually clicks: it shows the test in
    the context of the build (latest run, history, retries, errors), and
    it sidesteps the `case-result/<id>` path, which 404s on the legacy
    short form and is fragile against UI URL changes.

    The filter is the same JSON Testray's UI writes to the query string
    when you type into the search box:
        {"testrayCaseName": "<full.case.name>"}
    """
    import json
    from urllib.parse import quote

    filter_payload = json.dumps(
        {"testrayCaseName": case_name}, separators=(",", ":")
    )
    encoded = quote(filter_payload, safe="")
    return (
        f"{TESTRAY_UI_URL}#/project/{project_id}"
        f"/routines/{routine_id}/build/{build_id}"
        f"?filter={encoded}&filterSchema=buildResults&page=1"
    )


def assign_issue_to_case_result_batch(batch_updates):
    """Update a batch of case results with issues and due statuses."""
    if os.getenv("DRY_RUN", "false").lower() == "true":
        print(f"[DRY RUN] Would assign issues to {len(batch_updates)} case results:")
        for item in batch_updates:
            print(
                f"    - case result {item['id']} → issues={item.get('issues')} "
                f"({testray_case_result_url(item['id'])})"
            )
        return
    for item in batch_updates:
        case_result_id = item["id"]
        payload = {"dueStatus": item["dueStatus"], "issues": item["issues"]}
        url = f"{BASE_URL}/caseresults/{case_result_id}"
        _put_json(url, payload)


def autofill_build(testray_build_id_1, testray_build_id_2):
    """Trigger autofill between two Testray builds."""
    if os.getenv("DRY_RUN", "false").lower() == "true":
        print(
            f"[DRY RUN] Would autofill build {testray_build_id_2} "
            f"({testray_build_url(testray_build_id_2)}) from {testray_build_id_1} "
            f"({testray_build_url(testray_build_id_1)})"
        )
        return {}
    url = f"{TESTRAY_REST_URL}/testray-build-autofill/{testray_build_id_1}/{testray_build_id_2}"
    response = _request_with_auth_retry("POST", url, data="")
    _raise_for_status(response, f"autofilling build {testray_build_id_2} from {testray_build_id_1}")
    return response.json()


def complete_task(task_id):
    if os.getenv("DRY_RUN", "false").lower() == "true":
        print(f"[DRY RUN] Would complete task {task_id} → {testray_task_url(task_id)}")
        return {}
    url = f"{BASE_URL}/tasks/{task_id}"
    payload = {"dueStatus": {"key": "COMPLETE", "name": "Complete"}}
    response = _request_with_auth_retry(
        "PATCH", url, json=payload, headers={"Content-Type": "application/json"}
    )
    _raise_for_status(response, f"completing task {task_id}")
    return response.json()


def create_task(build):
    """Create a task for a build."""
    if os.getenv("DRY_RUN", "false").lower() == "true":
        print(
            f"[DRY RUN] Would create task for build {build['id']} "
            f"→ {testray_build_url(build['id'])}"
        )
        return {"id": "DRY-RUN-TASK-ID"}
    payload = {
        "name": build["name"],
        "r_buildToTasks_c_buildId": build["id"],
        "dueStatus": {"key": "INANALYSIS", "name": "In Analysis"},
    }
    response = _request_with_auth_retry(
        "POST",
        f"{BASE_URL}/tasks/",
        json=payload,
        headers={"Content-Type": "application/json"},
    )
    _raise_for_status(response, f"creating task for build {build['id']}")
    return response.json()


def create_testflow(task_id):
    """Create testflow for a task."""
    if os.getenv("DRY_RUN", "false").lower() == "true":
        print(
            f"[DRY RUN] Would create testflow for task {task_id} "
            f"→ {testray_task_url(task_id)}"
        )
        return {}
    url = f"{TESTRAY_REST_URL}/testray-testflow/{task_id}"
    response = _request_with_auth_retry("POST", url, data="")
    _raise_for_status(response, f"creating testflow for task {task_id}")
    return response.json()


def fetch_case_results(case_id, routine_id, status=None, page_size=500):
    base_url = f"{TESTRAY_REST_URL}/testray-case-result-history/{case_id}"
    page = 1
    all_items = []

    while True:
        params = (
            f"testrayRoutineIds={routine_id}"
            + (f"&status={status}" if status else "")
            + f"&page={page}&pageSize={page_size}"
        )
        url = f"{base_url}?{params}"
        result = _get_json(url)
        items = result.get("items", [])
        all_items.extend(items)

        if len(items) < page_size:
            break
        page += 1

    return all_items


def get_all_build_case_results(build_id, include_case=False, verbose=False):
    """Fetch all case results for a given build (paginated).

    Set `include_case=True` to inline the nested `r_caseToCaseResult_c_case`
    object on each item — saves one `get_case_info` round-trip per case when
    the caller needs the case `name`.

    Set `verbose=True` to print per-page progress; useful for slow builds
    (Acceptance routinely returns thousands of case results).
    """
    page = 1
    all_items = []
    nested = "&nestedFields=r_caseToCaseResult_c_case" if include_case else ""

    while True:
        if verbose:
            print(
                f"    · fetching case results page {page} "
                f"({len(all_items)} so far)...",
                flush=True,
            )
        url = (
            f"{BASE_URL}/builds/{build_id}/buildToCaseResult"
            f"?pageSize=500&page={page}{nested}"
        )
        data = _get_json(url)
        items = data.get("items", [])
        all_items.extend(items)

        if len(items) < 500:
            break
        page += 1

    if verbose:
        print(f"    · fetched {len(all_items)} case result(s) total", flush=True)

    return all_items


@lru_cache(maxsize=None)
def get_build_info(build_id):
    """Get build metadata, including routine ID and due date."""
    url = f"{BASE_URL}/builds/{build_id}?fields=dueDate,gitHash,name,id,importStatus,r_routineToBuilds_c_routineId&nestedFields=buildToTasks"
    return _get_json(url)


@lru_cache(maxsize=None)
def get_routine_info(routine_id):
    """Get routine metadata (project association, name, etc.).

    Cached: a routine's owning project doesn't change during a run, and
    the URL builders need it once per routine.
    """
    url = f"{BASE_URL}/routines/{routine_id}"
    return _get_json(url)


def find_cases_by_name_substring(name_substring, limit=20):
    """Return Testray cases whose `name` contains `name_substring`.

    Used by the PR review check to map a Java test class name (taken
    from a merge diff) to the Testray case(s) that exercise it, without
    paginating through every case result of a build. `limit` caps the
    response in the rare event that several packages contain a class
    with the same simple name.
    """
    from urllib.parse import quote_plus

    encoded = quote_plus(f"contains(name,'{name_substring}')")
    url = (
        f"{BASE_URL}/cases?filter={encoded}"
        f"&fields=id,name&pageSize={limit}"
    )
    return _get_json(url).get("items", []) or []


def find_case_by_exact_name(name):
    """Look up the case whose `name` equals `name` exactly. Returns one
    case dict ({id, name}) or None.

    Faster and more accurate than the substring matcher when the caller
    already has the Java FQN of the test (derived from the file path).
    """
    from urllib.parse import quote

    encoded = quote(f"name eq '{name}'")
    url = f"{BASE_URL}/cases?filter={encoded}&fields=id,name&pageSize=2"
    items = _get_json(url).get("items", []) or []
    for item in items:
        if item.get("name") == name:
            return item
    return None


def find_case_result_in_build_via_history(build_id, case_id):
    """Find the case result for `case_id` within `build_id`.

    Walks the testray-rest `case-result-history` endpoint for the case,
    filtered server-side to the Acceptance routine and (when honored)
    to the specific build. The endpoint is already used elsewhere in
    this script for AFT history and is the reliable way to navigate
    from (case, build) to a specific case-result row.

    Returns a dict shaped as:
        {"id": <case_result_id>, "dueStatus": {"name": "<STATUS>"}}
    or None when the case did not run in this build.
    """
    expected_build = int(build_id)
    base_url = f"{TESTRAY_REST_URL}/testray-case-result-history/{case_id}"

    page = 1
    page_size = 500
    while True:
        params = (
            f"testrayRoutineIds={ACCEPTANCE_ROUTINE_ID}"
            f"&testrayBuildIds={expected_build}"
            f"&page={page}&pageSize={page_size}"
        )
        url = f"{base_url}?{params}"
        result = _get_json(url) or {}
        items = result.get("items", []) or []

        for entry in items:
            # Defensive: even if `testrayBuildIds` isn't honored
            # server-side, only accept the entry that's actually for
            # the build we asked about.
            if int(entry.get("testrayBuildId") or 0) != expected_build:
                continue
            cr_id = (
                entry.get("testrayCaseResultId")
                or entry.get("caseResultId")
                or entry.get("id")
            )
            return {
                "id": cr_id,
                "dueStatus": {"name": entry.get("status")},
            }

        if len(items) < page_size:
            break
        page += 1

    return None


def get_build_tasks(build_id):
    """Get tasks associated with a build."""
    url = f"{BASE_URL}/builds/{build_id}/buildToTasks?fields=id,dueStatus"
    return _get_json(url).get("items", [])


@lru_cache(maxsize=None)
def get_case_info(case_id):
    """Get the name and priority of a test case."""
    url = f"{BASE_URL}/cases/{case_id}"
    return _get_json(url)


def get_case_result(case_result_id):
    url = f"{BASE_URL}/caseresults/{case_result_id}"
    return _get_json(url)


def get_case_count_by_type_in_build(build_id, case_type_id):
    """Get the count of unique cases of a specific type that have results in a given build."""
    if not case_type_id:
        return 0

    all_items = []
    page = 1
    page_size = 500

    while True:
        url = (
            f"{BASE_URL}/builds/{build_id}/buildToCaseResult"
            f"?nestedFields=r_caseToCaseResult_c_case&pageSize={page_size}&page={page}"
        )
        data = _get_json(url)
        items = data.get("items", [])
        all_items.extend(items)

        if len(items) < page_size:
            break
        page += 1

    matching_case_ids = {
        item.get("r_caseToCaseResult_c_caseId")
        for item in all_items
        if item.get("r_caseToCaseResult_c_case", {}).get(
            "r_caseTypeToCases_c_caseTypeId"
        )
        == case_type_id
    }

    return len(matching_case_ids)


@lru_cache(maxsize=None)
def get_case_type_id_by_name(case_type_name):
    """Get the ID of a case type by its name."""
    url = f"{BASE_URL}/casetypes?filter=name eq '{case_type_name}'&fields=id"
    result = _get_json(url)
    items = result.get("items", [])
    if items:
        return items[0].get("id")
    return None


@lru_cache(maxsize=None)
def get_case_type_name(case_type_id):
    """Get name of a case type by ID."""
    url = f"{BASE_URL}/casetypes/{case_type_id}?fields=name"
    return _get_json(url).get("name", "Unknown")


@lru_cache(maxsize=None)
def get_component_name(component_id):
    """Get name of a component by ID."""
    url = f"{BASE_URL}/components/{component_id}?fields=name"
    return _get_json(url).get("name", "Unknown")


def get_routine_to_builds(routine_id):
    """Fetch all builds for a routine, sorted by dueDate descending.

    Uses explicit pagination instead of pageSize=-1: the relation endpoint
    has been observed silently capping the response at the default page when
    pageSize=-1 is passed, which would let recently-imported builds slip past
    the analyzer.

    Sorting by dueDate (the build's scheduled run time) is more reliable than
    dateCreated, which Testray sometimes leaves null on freshly imported
    builds (sorting by null pushes the newest build to the bottom of the list).
    """
    page = 1
    page_size = 200
    all_items = []
    while True:
        url = (
            f"{BASE_URL}/routines/{routine_id}/routineToBuilds"
            f"?fields=dueDate,name,id,importStatus,gitHash,r_routineToBuilds_c_routineId,dateCreated"
            f"&pageSize={page_size}&page={page}"
        )
        items = _get_json(url).get("items", [])
        all_items.extend(items)
        if len(items) < page_size:
            break
        page += 1
    return sorted(all_items, key=lambda b: b.get("dueDate") or "", reverse=True)


def get_build_sha(build_id):
    """Get gitHash of a specific build."""
    url = f"{BASE_URL}/builds/{build_id}?fields=gitHash"
    return _get_json(url).get("gitHash")


def get_build_metrics(routine_id):
    """
    Fetch all acceptance builds with their metrics from Testray REST API.
    Returns a list of items with fields like:
      - testrayBuildGitHash
      - testrayBuildId
      - testrayBuildName
      - testrayStatusMetric (dict of counts)
    """
    url = f"{TESTRAY_REST_URL}/testray-status-metrics/by-testray-routineId/{routine_id}/testray-builds-metrics"
    data = _get_json(url)
    return data.get("items", [])


def get_subtask_case_results(subtask_id):
    """Get case results under a subtask."""
    url = f"{BASE_URL}/subtasks/{subtask_id}/subtaskToCaseResults?fields=id,executionDate,errors,issues,r_caseToCaseResult_c_caseId,r_componentToCaseResult_c_componentId&pageSize=-1"
    return _get_json(url).get("items", [])


def get_task_status(task_id):
    """Get the status of a task."""
    url = f"{BASE_URL}/tasks/{task_id}?fields=dueStatus"
    return _get_json(url)


def get_task_subtasks(task_id):
    """Get subtasks associated with a task."""
    url = f"{BASE_URL}/tasks/{task_id}/taskToSubtasks?pageSize=-1"
    return _get_json(url).get("items", [])


def update_subtask_status(subtask_id: str, issues: Optional[str] = None) -> None:
    if os.getenv("DRY_RUN", "false").lower() == "true":
        print(
            f"[DRY RUN] Would update subtask {subtask_id} status to COMPLETE "
            f"(issues: {issues}) → {testray_subtask_url(subtask_id)}"
        )
        return
    """Mark a subtask as complete."""
    url = f"{BASE_URL}/subtasks/{subtask_id}"
    payload = {"dueStatus": {"key": "COMPLETE", "name": "Complete"}}
    if issues:
        payload["issues"] = issues
    _put_json(url, payload)
    print(f"Subtask {subtask_id} marked as COMPLETE.")

def _get_json(url, max_retries=3):
    last_exception = None

    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=_get_headers(), timeout=30)

            if response.status_code == 401:
                _get_headers.cache_clear()
                response = requests.get(url, headers=_get_headers(), timeout=30)

            _raise_for_status(response, f"GET {url}")

            return response.json()

        except RequestException as e:
            last_exception = e  # Save the error
            wait_time = (attempt + 1) * 5

            if attempt < max_retries - 1:
                print(f"Attempt {attempt + 1} failed ({e}). Retrying in {wait_time}s...")
                time.sleep(wait_time)
            else:
                print("All retry attempts exhausted.")

    if last_exception:
        raise last_exception

    raise RuntimeError(f"Failed to fetch JSON from {url} after {max_retries} attempts.")


def _request_with_auth_retry(method, url, **kwargs):
    """Issue an HTTP request, refreshing the cached OAuth token on 401 and retrying once.

    The token from _get_headers() is lru_cached but has a finite server-side TTL,
    so long-running scripts eventually see a 401 mid-run on the first write.
    """
    extra_headers = kwargs.pop("headers", {}) or {}
    response = requests.request(
        method, url, headers={**_get_headers(), **extra_headers}, **kwargs
    )
    if response.status_code == 401:
        _get_headers.cache_clear()
        response = requests.request(
            method, url, headers={**_get_headers(), **extra_headers}, **kwargs
        )
    return response


def _put_json(url, payload):
    """Send PUT request with JSON payload."""
    response = _request_with_auth_retry(
        "PUT", url, json=payload, headers={"Content-Type": "application/json"}
    )
    _raise_for_status(response, f"PUT {url}")
    return response.json()


def _raise_for_status(response, action):
    """Raise HTTPError with clearer guidance for auth-related failures.

    401 → token wrong/expired. 403 → token OK but client lacks permission.
    Both are easy to confuse with bugs in the script, so make them obvious.
    """
    if response.status_code == 403:
        raise requests.HTTPError(
            f"403 Forbidden while {action}.\n"
            f"  The OAuth2 client authenticated, but is not authorized for this endpoint.\n"
            f"  Ask whoever provisioned TESTRAY_CLIENT_ID / TESTRAY_CLIENT_SECRET to grant\n"
            f"  the required Testray role (typically 'Testray Users' membership or\n"
            f"  equivalent permission on the testray-rest application).",
            response=response,
        )
    if response.status_code == 401:
        raise requests.HTTPError(
            f"401 Unauthorized while {action}.\n"
            f"  Verify TESTRAY_CLIENT_ID / TESTRAY_CLIENT_SECRET are correct and active.",
            response=response,
        )
    response.raise_for_status()


@lru_cache()
def _get_headers():
    TESTRAY_CLIENT_ID = os.getenv("TESTRAY_CLIENT_ID") or (_ for _ in ()).throw(
        EnvironmentError("TESTRAY_CLIENT_ID environment variable is not set.")
    )
    TESTRAY_CLIENT_SECRET = os.getenv("TESTRAY_CLIENT_SECRET") or (_ for _ in ()).throw(
        EnvironmentError("TESTRAY_CLIENT_SECRET environment variable is not set.")
    )
    response = requests.post(
        "https://testray.liferay.com/o/oauth2/token",
        headers={
            "Authorization": f"Basic {base64.b64encode(f'{TESTRAY_CLIENT_ID}:{TESTRAY_CLIENT_SECRET}'.encode()).decode()}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials"},
    )
    if response.status_code in (400, 401):
        raise requests.HTTPError(
            f"{response.status_code} from OAuth2 token endpoint.\n"
            f"  TESTRAY_CLIENT_ID / TESTRAY_CLIENT_SECRET appear to be invalid or revoked.\n"
            f"  Server response: {response.text.strip()}",
            response=response,
        )
    response.raise_for_status()
    return {
        "Authorization": f"Bearer {response.json()['access_token']}",
        "Accept": "application/json",
    }
