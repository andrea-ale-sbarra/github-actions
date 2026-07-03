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


CLOSE_OK = "closed"
CLOSE_SKIPPED_SFD_NO_SUBTASKS = "sfd_no_subtasks"
CLOSE_SKIPPED_PARENT_ACTIVE = "parent_active"
CLOSE_SKIPPED_ACTIVE_SUBTASK = "active_subtask"
CLOSE_FAILED = "failed"


def _is_selected_for_development(status_name):
    """Locale-tolerant match for the 'Selected for Development' workflow state.

    Status names returned by Jira are localized to the requesting account's
    language, but this particular workflow step is configured without a
    translation in the Liferay instance, so a normalized name comparison is
    safe. Should that ever change, swap the comparison for a JQL-based check.
    """
    def norm(s):
        return s.lower().replace(" ", "").replace("-", "")

    return norm(status_name or "") == norm("Selected for Development")


def close_issue(issue_key, build_hash):
    """
    Close a routine investigation ticket as Discarded ("not reproducible").

    Flow:
    1. Load parent.
    2. If the parent's status category is 'indeterminate' (In Progress,
       In Review, …), someone is actively working on the ticket — leave
       it alone (return CLOSE_SKIPPED_PARENT_ACTIVE). The bot never
       auto-closes work humans are mid-flight on.
    3. Special case: if the parent is in 'Selected for Development'
       AND has no sub-tasks, leave it alone. Reaching SFD requires
       either a human action or a previous bot pass; either way,
       expected sub-tasks should already exist. Their absence means
       something is off — punt to a human via the run report (return
       CLOSE_SKIPPED_SFD_NO_SUBTASKS).
    4. Inspect sub-tasks by locale-independent status category:
       - 'done'          → already closed, skip
       - 'new'           → close as Discarded
       - 'indeterminate' → active work, abort (return CLOSE_SKIPPED_ACTIVE_SUBTASK)
       - anything else   → unknown state, abort (return CLOSE_SKIPPED_ACTIVE_SUBTASK)
    5. Close 'new' sub-tasks. If any sub-task close fails, abort
       (CLOSE_FAILED) so parent and sub-task never diverge.
    6. Close the parent directly: find a 'Closed' transition reachable
       from the current state (no detour through 'Selected for
       Development'). Skipping the SFD detour avoids triggering the
       Jira automation that creates skeleton sub-tasks asynchronously
       — historically a race that left orphan sub-tasks under a
       freshly Discarded parent.

    Returns one of:
      CLOSE_OK                          parent closed (or would be in DRY_RUN)
      CLOSE_SKIPPED_PARENT_ACTIVE       parent in an indeterminate status
      CLOSE_SKIPPED_SFD_NO_SUBTASKS     parent in SFD with no sub-tasks
      CLOSE_SKIPPED_ACTIVE_SUBTASK      sub-task active/unknown
      CLOSE_FAILED                      transition missing or exception raised
    """

    try:
        if os.getenv("DRY_RUN", "false").lower() == "true":
            print(f"[DRY RUN] Would close Jira issue: {issue_key} → {jira_issue_url(issue_key)}")
            return CLOSE_OK

        jira = _jira()

        # -----------------------------------------
        # STEP 0: Load parent issue
        # -----------------------------------------
        parent_issue = jira.issue(issue_key)
        current_status = parent_issue.fields.status.name
        current_category = getattr(
            getattr(parent_issue.fields.status, "statusCategory", None), "key", None
        )
        subtasks = getattr(parent_issue.fields, "subtasks", [])

        print(
            f"\nℹ Processing {issue_key} "
            f"(current status: {current_status}, sub-tasks: {len(subtasks)})"
        )

        # -----------------------------------------
        # STEP 1: Parent in an indeterminate status → don't touch
        # -----------------------------------------
        if current_category == "indeterminate":
            print(
                f"⚠ {issue_key} is in '{current_status}' (someone is working on it). "
                f"Leaving untouched."
            )
            return CLOSE_SKIPPED_PARENT_ACTIVE

        # -----------------------------------------
        # STEP 2: SFD without sub-tasks → leave for human review
        # -----------------------------------------
        if _is_selected_for_development(current_status) and not subtasks:
            print(
                f"⚠ {issue_key} is in '{current_status}' with no sub-tasks. "
                f"Leaving open for manual review."
            )
            return CLOSE_SKIPPED_SFD_NO_SUBTASKS

        # -----------------------------------------
        # STEP 2: Inspect sub-tasks by status category
        # -----------------------------------------
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
            return CLOSE_SKIPPED_ACTIVE_SUBTASK

        if subtasks_to_close:
            print(f"✔ Sub-tasks valid ({len(subtasks_to_close)} to close). Proceeding.")

        # -----------------------------------------
        # STEP 3: Close every safe sub-task
        # -----------------------------------------
        for subtask_key, original_status in subtasks_to_close:
            print(f"→ Closing child {subtask_key} (was '{original_status}')")
            if not _transition_to_closed(subtask_key, build_hash, original_status):
                print(
                    f"⛔ Could not close sub-task {subtask_key}. "
                    f"Aborting parent {issue_key} to keep parent/sub-task consistent."
                )
                return CLOSE_FAILED

        # -----------------------------------------
        # STEP 4: Close parent directly from current state
        # -----------------------------------------
        transitions = jira.transitions(issue_key)
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
            jira.transition_issue(
                issue_key,
                transition=close_transition["id"],
                resolution={"name": "Discarded"},
            )
            jira.add_comment(issue_key, f"Closed. Not reproducible in SHA {build_hash}")
            print(
                f"✔ {issue_key} → '{close_transition['name']}' with "
                f"'Discarded' (direct from '{current_status}')"
            )
            return CLOSE_OK
        else:
            print(
                f"✘ Could not find a 'Closed'/done transition for parent "
                f"{issue_key} from '{current_status}'."
            )
            return CLOSE_FAILED

    except Exception as e:
        print(f"✘ Failed to process issue {issue_key}: {e}")
        return CLOSE_FAILED


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
        update={"labels": [{"add": label_to_apply}, {"add": "unplanned_work"}]}
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


def close_orphan_subtasks_of_discarded(parent_key):
    """
    For a parent that is already Closed/Discarded, close any sub-task
    still left open. This is the retroactive cleanup counterpart to
    `close_issue`: when a parent was discarded in the past, its
    sub-tasks must also be closed for parent/sub-task consistency.

    Sub-task handling mirrors `close_issue`:
      - statusCategory == 'done'          → skip silently
      - statusCategory == 'new'           → close with 'Discarded'
      - statusCategory == 'indeterminate' → skip with warning (active work)
      - anything else                     → skip with warning (unknown state)

    The closing comment is a fixed pointer to the parent — no SHA, since
    no test run is driving this closure.

    Returns a dict:
        {
            "parent":                 parent_key,
            "closed":                 [subtask_key, ...],
            "skipped_active":         [subtask_key, ...],
            "skipped_unknown":        [subtask_key, ...],
            "failed":                 [subtask_key, ...],
        }
    """
    summary = {
        "parent": parent_key,
        "closed": [],
        "skipped_active": [],
        "skipped_unknown": [],
        "failed": [],
    }

    try:
        jira = _jira()
        parent_issue = jira.issue(parent_key)
        subtasks = getattr(parent_issue.fields, "subtasks", [])

        if not subtasks:
            return summary

        dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
        comment = (
            f"Closing sub-task: parent {parent_key} was already "
            "closed as Discarded"
        )

        for subtask in subtasks:
            subtask_key = subtask.key
            try:
                sub = jira.issue(subtask_key)
            except Exception as e:
                print(f"✘ Could not load sub-task {subtask_key}: {e}")
                summary["failed"].append(subtask_key)
                continue

            status_name = sub.fields.status.name
            category_key = getattr(
                getattr(sub.fields.status, "statusCategory", None), "key", None
            )

            if category_key == "done":
                continue
            if category_key == "new":
                if dry_run:
                    print(
                        f"[DRY RUN] Would close sub-task {subtask_key} "
                        f"(was '{status_name}') for parent {parent_key} → "
                        f"{jira_issue_url(subtask_key)}"
                    )
                    summary["closed"].append(subtask_key)
                    continue

                print(
                    f"→ Closing orphan sub-task {subtask_key} "
                    f"(was '{status_name}') — parent {parent_key} is Discarded"
                )
                if _transition_to_closed(
                    subtask_key,
                    build_hash=None,
                    original_status=status_name,
                    comment_override=comment,
                ):
                    summary["closed"].append(subtask_key)
                else:
                    summary["failed"].append(subtask_key)
            elif category_key == "indeterminate":
                print(
                    f"⚠ Sub-task {subtask_key} is '{status_name}' (active) "
                    f"under Discarded parent {parent_key}. Skipping — close manually."
                )
                summary["skipped_active"].append(subtask_key)
            else:
                print(
                    f"⚠ Sub-task {subtask_key} is '{status_name}' "
                    f"(unknown category '{category_key}') under Discarded "
                    f"parent {parent_key}. Skipping."
                )
                summary["skipped_unknown"].append(subtask_key)

        return summary

    except Exception as e:
        print(f"✘ Failed to inspect parent {parent_key}: {e}")
        return summary


def _transition_to_closed(
    issue_key, build_hash, original_status=None, comment_override=None
):
    """
    Closes a single sub-task (directly to 'Closed' with 'Discarded').

    Looks for a transition literally named 'Closed' first; falls back to
    any transition that lands in the 'done' status category, so the
    routine still works when the sub-task starts from a state like
    'To Do' or 'Selected for Development' where the workflow names the
    close transition differently.

    When `comment_override` is provided, it replaces the default
    "Not reproducible in SHA …" comment. Used by the retroactive cleanup
    pass that closes subtasks of parents already Discarded — there is no
    test run behind that closure, so the SHA reference would be
    misleading.

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
            if comment_override:
                comment = comment_override
            else:
                origin_clause = f" (was '{original_status}')" if original_status else ""
                comment = (
                    f"Closing sub-task{origin_clause}. "
                    f"Not reproducible in current SHA {build_hash}"
                )
            _jira().add_comment(issue_key, comment)
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
