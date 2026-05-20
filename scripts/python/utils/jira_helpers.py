from jira import JIRA
import os
import re
from functools import lru_cache


JIRA_BASE_URL = "https://liferay.atlassian.net"

_JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


def jira_issue_url(issue_key):
    return f"{JIRA_BASE_URL}/browse/{issue_key}"


def is_valid_jira_key(issue_key):
    """Return True only for strings shaped like LPD-12345."""
    return bool(issue_key and _JIRA_KEY_RE.match(issue_key.strip()))


def close_issue(issue_key, build_hash):
    """
    Final optimized flow:
    1. Load issue
    2. Inspect subtasks via status category (locale-independent):
       - 'done'          → already closed, skip
       - 'new'           → safe to close (Open / To Do / Backlog / Selected for Development / …)
       - 'indeterminate' → ABORT parent: a sub-task is actively being worked on
       - anything else   → ABORT parent: unknown state, play it safe
    3. Close every 'new'-category subtask, recording its original status
       in the closing comment. If any close fails, ABORT the parent so
       parent and sub-task never diverge.
    4. Move parent to 'Selected for Development'
    5. Close parent with resolution 'Discarded'

    Returns True if the parent issue was actually closed (or would be in
    DRY_RUN mode), False otherwise (active/unknown subtask, failed
    sub-task close, missing transition, exception). The boolean lets
    callers report which tickets were closed by the current run.
    """

    try:
        if os.getenv("DRY_RUN", "false").lower() == "true":
            print(f"[DRY RUN] Would close Jira issue: {issue_key} → {jira_issue_url(issue_key)}")
            return True

        jira = _jira()

        # -----------------------------------------
        # STEP 0: Load parent issue
        # -----------------------------------------
        parent_issue = jira.issue(issue_key)
        current_status = parent_issue.fields.status.name

        print(f"\nℹ Processing {issue_key} (current status: {current_status})")

        # -----------------------------------------
        # STEP 1: Inspect subtasks by status category
        # -----------------------------------------
        subtasks = getattr(parent_issue.fields, "subtasks", [])
        subtasks_to_close = []  # list of (key, original_status_name)
        blocked = False

        for subtask in subtasks:
            subtask_key = subtask.key
            sub = jira.issue(subtask_key)
            status_name = sub.fields.status.name
            category_key = getattr(
                getattr(sub.fields.status, "statusCategory", None), "key", None
            )

            if category_key == "done":
                print(f"✔ Sub-task {subtask_key} is '{status_name}' (already done). Skipping.")
            elif category_key == "new":
                print(f"→ Sub-task {subtask_key} is '{status_name}' (not started). Will close.")
                subtasks_to_close.append((subtask_key, status_name))
            elif category_key == "indeterminate":
                print(f"⛔ Sub-task {subtask_key} is '{status_name}' (active). Aborting.")
                blocked = True
            else:
                print(
                    f"⛔ Sub-task {subtask_key} is '{status_name}' "
                    f"(unknown status category '{category_key}'). Aborting."
                )
                blocked = True

        if blocked:
            print(f"⛔ {issue_key} will NOT be touched due to active/unknown subtasks.")
            return False

        print(f"✔ Subtasks valid ({len(subtasks_to_close)} to close). Proceeding.")

        # -----------------------------------------
        # STEP 2: Close every safe subtask
        # -----------------------------------------
        for subtask_key, original_status in subtasks_to_close:
            print(f"→ Closing child {subtask_key} (was '{original_status}')")
            if not _transition_to_closed(subtask_key, build_hash, original_status):
                print(
                    f"⛔ Could not close sub-task {subtask_key}. "
                    f"Aborting parent {issue_key} to keep parent/sub-task consistent."
                )
                return False

        # -----------------------------------------
        # STEP 3: Move parent to "Selected for Development"
        # -----------------------------------------
        if current_status != "Selected for Development":
            transitions = jira.transitions(issue_key)

            def norm(s):
                return s.lower().replace(" ", "").replace("-", "")

            target = norm("Selected for Development")

            selected_dev_transition = next(
                (
                    t
                    for t in transitions
                    if norm(t.get("to", {}).get("name", "")) == target
                ),
                None,
            ) or next(
                (t for t in transitions if target in norm(t["name"])),
                None,
            )

            if selected_dev_transition:
                jira.transition_issue(issue_key, selected_dev_transition["id"])
                print(f"✔ {issue_key} → '{selected_dev_transition['name']}'")
                parent_issue = jira.issue(issue_key)
            else:
                print("⚠ No transition to 'Selected for Development' found.")
        else:
            print(f"✔ {issue_key} already in 'Selected for Development'")

        # -----------------------------------------
        # STEP 4: Close parent issue
        # -----------------------------------------
        transitions = jira.transitions(issue_key)
        close_transition = next((t for t in transitions if t["name"] == "Closed"), None)

        if close_transition:
            jira.transition_issue(
                issue_key,
                transition=close_transition["id"],
                resolution={"name": "Discarded"},
            )
            jira.add_comment(issue_key, f"Closed. Not reproducible in SHA {build_hash}")
            print(f"✔ {issue_key} → Closed with resolution 'Discarded'")
            return True
        else:
            print("✘ Could not find 'Closed' transition for parent.")
            return False

    except Exception as e:
        print(f"✘ Failed to process issue {issue_key}: {e}")
        return False


def create_jira_task(epic, summary, description, component, label, due_date=None, priority=None, routine_label=None):
    """
    Creates a Jira investigation task for unique failures.
    `component` can be:
      - a single string
      - a list of strings
      - a list of dicts
      - None (defaults to no components)
    `priority` is an optional Jira priority name (e.g. "Minor", "Medium", "Major", "Critical").
    `routine_label` is the per-routine identifying label. When None, falls back
    to the legacy `ROUTINE_LABEL` env var.
    Returns None if the epic is missing (in live mode).
    """

    if os.getenv("DRY_RUN", "false").lower() == "true":
        epic_info = f" under epic {epic.key} ({jira_issue_url(epic.key)})" if epic else " (no epic)"
        print(f"[DRY RUN] Would create Jira task: {summary} (priority={priority}){epic_info}")

        class MockIssue:
            key = "DRY-RUN-KEY"

        return MockIssue()

    if epic is None:
        print(
            f"✘ Cannot create Jira task '{summary}': no testing epic found. "
            "Check the epic JQL in testray_helpers.py."
        )
        return None

    # Normalize components into the correct format
    if isinstance(component, str):
        components_list = [{"name": component}]
    elif isinstance(component, list):
        if all(isinstance(c, dict) for c in component):
            components_list = component
        else:
            components_list = [{"name": str(c)} for c in component]
    elif component is None:
        components_list = []
    else:
        raise TypeError(
            f"Invalid type for component: {type(component).__name__}. "
            "Must be str, list, or None."
        )

    issue_dict = {
        "project": {"key": "LPD"},
        "summary": summary,
        "description": description,
        "parent": {"id": epic.id},
        "issuetype": {"name": "Task"},
        "components": components_list,
    }

    if due_date:
        issue_dict["duedate"] = due_date

    try:
        new_issue = _jira().create_issue(fields=issue_dict)
    except Exception as e:
        # Jira rejects the create when the detected components don't exist in LPD
        # and the caller can't create new ones. LPD also requires at least one
        # component, so we must fall back to a valid existing component.
        if "component" in str(e).lower():
            attempted = [
                c.get("name") if isinstance(c, dict) else c
                for c in issue_dict.get("components", [])
            ]
            fallback = os.getenv("FALLBACK_COMPONENT", "").strip()
            if not fallback:
                print(
                    f"✘ Cannot create '{summary}': components {attempted} "
                    "are not valid LPD components and FALLBACK_COMPONENT is not set. "
                    "Add FALLBACK_COMPONENT=<valid LPD component name> to your .env."
                )
                return None
            print(
                f"⚠ Components {attempted} not found in LPD for '{summary}'. "
                f"Retrying with FALLBACK_COMPONENT='{fallback}'."
            )
            issue_dict["components"] = [{"name": fallback}]
            new_issue = _jira().create_issue(fields=issue_dict)
        else:
            raise

    label_to_apply = routine_label or os.getenv("ROUTINE_LABEL", "routine_tasks")
    _jira().issue(new_issue.key).update(
        update={"labels": [{"add": label_to_apply}]}
    )
    if label:
        _jira().issue(new_issue.key).update(update={"labels": [{"add": label}]})
    if priority:
        try:
            _jira().issue(new_issue.key).update(fields={"priority": {"name": priority}})
        except Exception as e_dict:
            # Some Jira setups (team-managed / custom schemas) expect the
            # priority field as a plain string rather than {"name": "..."}.
            try:
                _jira().issue(new_issue.key).update(fields={"priority": priority})
            except Exception as e_str:
                print(
                    f"⚠ Could not set priority '{priority}' on {new_issue.key}. "
                    f"Tried dict form ({e_dict}) and string form ({e_str}). "
                    "Ticket was created but priority is left at default."
                )

    return new_issue


def get_all_issues(jql_str, fields):
    issues = []
    i = 0
    chunk_size = 50
    while True:
        chunk = _jira().search_issues(
            jql_str, startAt=i, maxResults=chunk_size, fields=fields
        )
        i += chunk_size
        issues += chunk.iterable
        if i >= chunk.total:
            break
    return issues


def get_issue_type_by_key(issue_key):
    try:
        issue = _jira().issue(issue_key, fields="issuetype")
        return issue.fields.issuetype.name
    except Exception as e:
        print(f"Error retrieving issue {issue_key}: {e}")
        return None


def get_issue_status_by_key(issue_key):
    """
    Retrieves the issue by key and returns its status name plus the
    status-category key.

    The category key (``"new"`` / ``"indeterminate"`` / ``"done"``) is
    locale-independent — unlike ``status.name``, which Jira renders in the
    requesting account's language and would return e.g. ``"Chiusa"`` for an
    Italian-localized bot. Callers that need to detect "this ticket is in a
    terminal state" should compare against the category, not the name.

    :param issue_key: The key of the issue (e.g., "LPD-12345").
    :return: Tuple (issue, status_name, status_category_key) if found,
             else (None, None, None)
    """
    try:
        issue = _jira().issue(issue_key, fields="status")
        status = issue.fields.status
        category_key = getattr(getattr(status, "statusCategory", None), "key", None)
        return issue, status.name, category_key
    except Exception as e:
        print(f"Error retrieving issue {issue_key}: {str(e)}")
        return None, None, None


def _transition_to_closed(issue_key, build_hash, original_status=None):
    """
    Closes a single sub-task (directly to 'Closed' with 'Discarded').

    Looks for a transition literally named 'Closed' first; falls back to
    any transition that lands in the 'done' status category, so the
    routine still works when the sub-task starts from a state like
    'To Do' or 'Selected for Development' where the workflow names the
    close transition differently.

    Returns True on success, False otherwise. The caller uses this to
    decide whether to proceed with the parent close.
    """
    try:
        if os.getenv("DRY_RUN", "false").lower() == "true":
            print(f"[DRY RUN] Would transition sub-task {issue_key} to 'Closed' → {jira_issue_url(issue_key)}")
            return True

        transitions = _jira().transitions(issue_key)
        close_transition = next(
            (t for t in transitions if t.get("name") == "Closed"),
            None,
        ) or next(
            (
                t
                for t in transitions
                if t.get("to", {}).get("statusCategory", {}).get("key") == "done"
            ),
            None,
        )

        if close_transition:
            _jira().transition_issue(
                issue_key,
                transition=close_transition["id"],
                resolution={"name": "Discarded"},
            )
            origin_clause = f" (was '{original_status}')" if original_status else ""
            _jira().add_comment(
                issue_key,
                f"Closing sub-task{origin_clause}. Not reproducible in current SHA {build_hash}",
            )
            print(f"✔ {issue_key} → '{close_transition['name']}' with 'Discarded'")
            return True
        else:
            print(f"✘ No transition to a 'done' status found for sub-task {issue_key}")
            return False

    except Exception as e:
        print(f"✘ Failed to close sub-task {issue_key}: {e}")
        return False


@lru_cache()
def _jira():
    url = "https://liferay.atlassian.net"
    user = os.getenv("JIRA_API_USER") or (_ for _ in ()).throw(
        EnvironmentError("JIRA_API_USER environment variable is not set.")
    )
    token = os.getenv("JIRA_API_TOKEN") or (_ for _ in ()).throw(
        EnvironmentError("JIRA_API_TOKEN environment variable is not set.")
    )
    print(f"Connecting to Jira in URL {url} with user {user}")
    jira = JIRA(url, basic_auth=(user, token))
    print("Connected to Jira")
    return jira
