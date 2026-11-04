import os
import re
import urllib.parse
import time
from typing import Optional, Dict, Any, Tuple, List
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=False)

def get_jira_credentials() -> Tuple[str, str, str, str]:
    """
    Retrieve Jira credentials from environment variables safely.
    Returns (jira_base_url, jira_email, jira_token, project_key).
    Never logs or exposes the token.
    """
    raw_base = os.environ.get("JIRA_BASE_URL") or os.environ.get("JIRA_URL") or ""
    jira_base_url = raw_base.strip().strip("\"'").rstrip("/")
    jira_email = (os.environ.get("JIRA_EMAIL") or "").strip().strip("\"'")
    jira_token = (os.environ.get("JIRA_API_TOKEN") or "").strip().strip("\"'")
    project_key = (os.environ.get("JIRA_PROJECT_KEY") or "SCRUM").strip().strip("\"'").upper()

    return jira_base_url, jira_email, jira_token, project_key

def get_jira_headers() -> Dict[str, str]:
    """Generate standard Jira REST API v3 headers."""
    return {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

def get_jira_auth(email: str, token: str) -> HTTPBasicAuth:
    """Generate standard HTTP Basic Auth for Jira Cloud using email and API token."""
    return HTTPBasicAuth(email, token)

def extract_jira_error(resp: requests.Response) -> str:
    """Extract a descriptive error message from a Jira REST API response."""
    try:
        data = resp.json()
        messages = []
        if isinstance(data, dict):
            if data.get("errorMessages"):
                messages.extend(data["errorMessages"])
            if data.get("errors") and isinstance(data["errors"], dict):
                for field, msg in data["errors"].items():
                    messages.append(f"{field}: {msg}")
            if data.get("message"):
                messages.append(data["message"])
        if messages:
            return "; ".join(messages)
    except Exception:
        pass
    return resp.text[:300] if resp.text else f"HTTP {resp.status_code}"

def _is_transient_status(status_code: int) -> bool:
    """Check if HTTP status code is a transient error eligible for retry."""
    return status_code in (429, 500, 502, 503, 504)

def _is_permanent_error(status_code: int) -> bool:
    """Check if HTTP status code is a permanent client error that should NOT be retried."""
    return status_code in (400, 401, 403, 404, 405, 422)

def verify_jira_connection() -> Dict[str, Any]:
    """
    Verify Jira Cloud authentication and project access.
    DOES NOT create any tickets.
    Never exposes JIRA_API_TOKEN in logs or returned error messages.
    """
    jira_base_url, jira_email, jira_token, project_key = get_jira_credentials()

    result = {
        "auth": "FAILED",
        "project_access": "FAILED",
        "project_key": project_key or "NOT SET",
        "display_name": None,
        "project_name": None,
        "error": None
    }

    if not jira_base_url or not jira_email or not jira_token:
        missing = []
        if not jira_base_url: missing.append("JIRA_BASE_URL")
        if not jira_email: missing.append("JIRA_EMAIL")
        if not jira_token: missing.append("JIRA_API_TOKEN")
        result["error"] = f"Missing required Jira credentials in .env: {', '.join(missing)}"
        return result

    headers = {"Accept": "application/json"}
    auth = get_jira_auth(jira_email, jira_token)
    timeout = int(os.environ.get("JIRA_TIMEOUT", "20"))

    # 1. Test Authentication via /myself
    myself_url = f"{jira_base_url}/rest/api/3/myself"
    try:
        response = requests.get(myself_url, headers=headers, auth=auth, timeout=timeout)
        if response.status_code == 200:
            user_data = response.json()
            result["auth"] = "SUCCESS"
            result["display_name"] = user_data.get("displayName", "Unknown")
        elif response.status_code == 401:
            result["error"] = (
                "Jira authentication failed (HTTP 401 Unauthorized). "
                "Verify JIRA_EMAIL and JIRA_API_TOKEN in .env. "
                "Ensure the API token was generated from https://id.atlassian.com/manage-profile/security/api-tokens."
            )
            return result
        elif response.status_code == 403:
            result["error"] = f"Jira access forbidden (HTTP 403 Forbidden): {extract_jira_error(response)}"
            return result
        else:
            result["error"] = f"Authentication check failed with HTTP {response.status_code}: {extract_jira_error(response)}"
            return result
    except requests.exceptions.Timeout:
        result["error"] = f"Connection timed out after {timeout}s while verifying Jira credentials at {jira_base_url}."
        return result
    except requests.exceptions.ConnectionError as e:
        result["error"] = f"Network connection error to {jira_base_url}: {str(e)}"
        return result
    except Exception as e:
        result["error"] = f"Unexpected exception during Jira authentication check: {str(e)}"
        return result

    # 2. Test Project Access via /project/{project_key}
    if not project_key:
        result["error"] = "JIRA_PROJECT_KEY is not specified in .env"
        return result

    project_url = f"{jira_base_url}/rest/api/3/project/{project_key}"
    try:
        proj_response = requests.get(project_url, headers=headers, auth=auth, timeout=timeout)
        if proj_response.status_code == 200:
            proj_data = proj_response.json()
            result["project_access"] = "SUCCESS"
            result["project_name"] = proj_data.get("name", "Unknown")
        elif proj_response.status_code == 404:
            result["error"] = (
                f"Jira project '{project_key}' not found or access denied (HTTP 404). "
                f"Check JIRA_PROJECT_KEY in .env and verify your account has browse permissions for this project."
            )
        elif proj_response.status_code == 403:
            result["error"] = f"Access denied to project '{project_key}' (HTTP 403 Forbidden). Ensure user '{jira_email}' is assigned to the project."
        else:
            result["error"] = f"Project check failed with HTTP {proj_response.status_code}: {extract_jira_error(proj_response)}"
    except requests.exceptions.Timeout:
        result["error"] = f"Connection timed out after {timeout}s while verifying Jira project access at {jira_base_url}."
    except requests.exceptions.ConnectionError as e:
        result["error"] = f"Network connection error to {jira_base_url}: {str(e)}"
    except Exception as e:
        result["error"] = f"Unexpected exception during Jira project access check: {str(e)}"

    return result

def create_jira_issue_api(
    title: str,
    description: str,
    project_key: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a Jira issue via Jira Cloud REST API v3 with configurable timeout and exponential backoff retry.
    Never exposes JIRA_API_TOKEN.
    
    Returns structured dict with:
      - mode: 'real' | 'mock'
      - status: 'SUCCESS' | 'FAILED' | 'SIMULATED'
      - http_status: Optional[int]
      - issue_key: Optional[str]
      - issue_url: Optional[str]
      - error: Optional[str]
      - formatted_text: str
    """
    jira_base_url, jira_email, jira_token, default_project = get_jira_credentials()
    key_to_use = (project_key or default_project or "SCRUM").strip().upper()

    # Fallback to simulated mode if credentials are missing
    if not (jira_base_url and jira_email and jira_token):
        formatted_text = (
            f"SIMULATED JIRA ISSUE\n"
            f"Title: {title}\n"
            f"Description: {description}\n"
            f"Status: SIMULATED (Jira credentials not configured)"
        )
        return {
            "mode": "mock",
            "status": "SIMULATED",
            "title": title,
            "description": description,
            "formatted_text": formatted_text
        }

    endpoint = f"{jira_base_url}/rest/api/3/issue"
    headers = get_jira_headers()
    auth = get_jira_auth(jira_email, jira_token)

    payload = {
        "fields": {
            "project": {"key": key_to_use},
            "summary": title,
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": description or "No description provided"
                            }
                        ]
                    }
                ]
            },
            "issuetype": {"name": "Task"}
        }
    }

    timeout = int(os.environ.get("JIRA_TIMEOUT", "20"))
    max_retries = int(os.environ.get("JIRA_MAX_RETRIES", "2"))
    backoff_factor = float(os.environ.get("JIRA_BACKOFF_FACTOR", "1.5"))

    last_response: Optional[requests.Response] = None
    last_exception: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(
                endpoint,
                headers=headers,
                json=payload,
                auth=auth,
                timeout=timeout
            )
            last_response = resp

            # Successful creation (200, 201, 202)
            if resp.status_code in (200, 201, 202):
                data = resp.json()
                raw_key = data.get("key")
                if not raw_key or not re.match(r"^[A-Z][A-Z0-9]+-\d+$", str(raw_key).strip().upper()):
                    err_msg = f"Jira responded with HTTP {resp.status_code} but returned an invalid or missing issue key: {data}"
                    return {
                        "mode": "real",
                        "status": "FAILED",
                        "http_status": resp.status_code,
                        "issue_key": None,
                        "issue_url": None,
                        "error": err_msg,
                        "title": title,
                        "formatted_text": f"FAILED JIRA ISSUE\nTitle: {title}\nError: {err_msg}"
                    }
                issue_key = raw_key.strip().upper()
                issue_url = get_safe_jira_issue_url(issue_key) or f"{jira_base_url}/browse/{issue_key}"
                formatted_text = (
                    f"REAL JIRA ISSUE\n"
                    f"Issue Key: {issue_key}\n"
                    f"Issue URL: {issue_url}\n"
                    f"HTTP Status: {resp.status_code}\n"
                    f"Title: {title}\n"
                    f"Status: Created successfully"
                )
                return {
                    "mode": "real",
                    "status": "SUCCESS",
                    "http_status": resp.status_code,
                    "issue_key": issue_key,
                    "issue_url": issue_url,
                    "title": title,
                    "formatted_text": formatted_text
                }

            # Permanent error: DO NOT RETRY
            if _is_permanent_error(resp.status_code):
                break

            # Transient error: retry if attempts remaining
            if _is_transient_status(resp.status_code) and attempt < max_retries:
                time.sleep(backoff_factor * (2 ** attempt))
                continue
            else:
                break

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exception = e
            if attempt < max_retries:
                time.sleep(backoff_factor * (2 ** attempt))
                continue
            else:
                break
        except Exception as e:
            last_exception = e
            break

    # Determine failure explanation and HTTP status
    if last_response is not None:
        http_status = last_response.status_code
        err_details = extract_jira_error(last_response)

        if http_status == 400:
            err_msg = f"HTTP 400 Bad Request: {err_details}. Check project key '{key_to_use}', issue fields, or issue type."
        elif http_status == 401:
            err_msg = (
                "HTTP 401 Unauthorized: Jira authentication failed. "
                "Check JIRA_EMAIL and JIRA_API_TOKEN in .env. "
                "Ensure your token was generated from https://id.atlassian.com/manage-profile/security/api-tokens."
            )
        elif http_status == 403:
            err_msg = (
                f"HTTP 403 Forbidden: Permission denied. "
                f"User '{jira_email}' lacks permission to create issues in project '{key_to_use}'."
            )
        elif http_status == 404:
            err_msg = (
                f"HTTP 404 Not Found: Project '{key_to_use}' or Jira base URL '{jira_base_url}' not found. "
                f"Verify JIRA_BASE_URL and JIRA_PROJECT_KEY."
            )
        elif http_status == 429:
            err_msg = "HTTP 429 Too Many Requests: Jira Cloud rate limit exceeded."
        elif http_status in (500, 502, 503, 504):
            err_msg = f"HTTP {http_status} Server Error: Jira Cloud is temporarily unavailable ({err_details})."
        else:
            err_msg = f"HTTP {http_status} Error: {err_details}"
    elif last_exception is not None:
        http_status = None
        if isinstance(last_exception, requests.exceptions.Timeout):
            err_msg = f"HTTPSConnectionPool Read timed out after {timeout}s connecting to {jira_base_url}."
        elif isinstance(last_exception, requests.exceptions.ConnectionError):
            err_msg = f"Network connection error connecting to {jira_base_url}: {str(last_exception)}"
        else:
            err_msg = f"Request exception connecting to Jira: {str(last_exception)}"
    else:
        http_status = None
        err_msg = "Unknown error occurred while contacting Jira API."

    # Return clear FAILED status (do not mask as simulated success)
    status_code_str = str(http_status) if http_status is not None else "TIMEOUT/CONNECTION_ERROR"
    formatted_text = (
        f"FAILED JIRA ISSUE (API ERROR)\n"
        f"Title: {title}\n"
        f"HTTP Status: {status_code_str}\n"
        f"Error: {err_msg}\n"
        f"Status: FAILED"
    )

    return {
        "mode": "real",
        "status": "FAILED",
        "http_status": http_status,
        "error": err_msg,
        "title": title,
        "formatted_text": formatted_text
    }

# =====================================================================
# SPRINT COMPARISON READ-ONLY FUNCTIONS (PHASE 2)
# =====================================================================

def _extract_adf_text(content: Any) -> str:
    """Extract plain text from Atlassian Document Format (ADF), dict, list, or string."""
    if not content:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        text_parts = []
        if content.get("text"):
            text_parts.append(str(content["text"]))
        for child in content.get("content", []):
            child_text = _extract_adf_text(child)
            if child_text:
                text_parts.append(child_text)
        return " ".join(text_parts).strip()
    if isinstance(content, list):
        return " ".join([_extract_adf_text(c) for c in content if c]).strip()
    return str(content).strip()

def map_jira_status_to_sprint_status(status_name: Optional[str], status_category: Optional[str] = None) -> str:
    """
    Map Jira status name / category to Sprint Comparison status:
      - Done / resolved / closed -> COMPLETED
      - In Progress / indeterminate -> IN_PROGRESS
      - To Do / new -> PENDING
      - Other / blocked / on hold -> BLOCKED
    """
    cat_lower = (status_category or "").strip().lower()
    name_lower = (status_name or "").strip().lower()

    if cat_lower == "done" or name_lower in ("done", "closed", "resolved", "completed"):
        return "COMPLETED"
    if cat_lower in ("indeterminate", "in progress") or name_lower in ("in progress", "in review", "in development", "testing"):
        return "IN_PROGRESS"
    if cat_lower == "new" or name_lower in ("to do", "open", "backlog", "reopened"):
        return "PENDING"
    if name_lower in ("blocked", "impediment", "on hold"):
        return "BLOCKED"
    return "BLOCKED"

def _parse_jira_issue_dict(raw_issue: Dict[str, Any], jira_base_url: str) -> Dict[str, Any]:
    """Parse raw Jira Cloud v3 issue payload into standardized dictionary."""
    key = raw_issue.get("key", "UNKNOWN")
    fields = raw_issue.get("fields") or {}

    summary = fields.get("summary") or "Untitled Issue"
    description = _extract_adf_text(fields.get("description"))

    status_obj = fields.get("status") or {}
    status_name = status_obj.get("name", "Unknown")
    status_cat_obj = status_obj.get("statusCategory") or {}
    status_category = status_cat_obj.get("name", "Unknown")
    sprint_status = map_jira_status_to_sprint_status(status_name, status_category)

    assignee_obj = fields.get("assignee")
    assignee_name = assignee_obj.get("displayName") if assignee_obj else None
    assignee_account_id = assignee_obj.get("accountId") if assignee_obj else None

    due_date = fields.get("duedate")
    created_date = fields.get("created")
    updated_date = fields.get("updated")
    issue_url = f"{jira_base_url}/browse/{key}"

    return {
        "key": key,
        "summary": summary,
        "description": description,
        "status_name": status_name,
        "status_category": status_category,
        "sprint_status": sprint_status,
        "assignee_name": assignee_name,
        "assignee_account_id": assignee_account_id,
        "due_date": due_date,
        "created_date": created_date,
        "updated_date": updated_date,
        "issue_url": issue_url
    }

def get_jira_issues_for_project(
    project_key: str = "SCRUM",
    max_results: int = 50,
    start_at: int = 0,
    fetch_all: bool = True
) -> Dict[str, Any]:
    """
    Retrieve issues for a Jira project via Jira Cloud REST API v3 with pagination and retry handling.
    Never exposes credentials or API token.
    """
    jira_base_url, jira_email, jira_token, default_project = get_jira_credentials()
    clean_project = (project_key or default_project or "SCRUM").strip().upper()

    if not (jira_base_url and jira_email and jira_token):
        return {
            "status": "FAILED",
            "error": "Missing required Jira credentials in .env (JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN)",
            "project_key": clean_project,
            "issues": [],
            "total": 0
        }

    endpoint = f"{jira_base_url}/rest/api/3/search/jql"
    fallback_endpoint = f"{jira_base_url}/rest/api/3/search"
    headers = get_jira_headers()
    auth = get_jira_auth(jira_email, jira_token)
    timeout = int(os.environ.get("JIRA_TIMEOUT", "20"))
    max_retries = int(os.environ.get("JIRA_MAX_RETRIES", "2"))
    backoff_factor = float(os.environ.get("JIRA_BACKOFF_FACTOR", "1.5"))

    all_issues = []
    current_start = start_at
    total = 0
    next_page_token = None

    while True:
        params = {
            "jql": f'project = "{clean_project}" ORDER BY created DESC',
            "maxResults": max_results,
            "fields": "summary,description,status,assignee,duedate,created,updated"
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token
        elif current_start > 0:
            params["startAt"] = current_start

        last_response: Optional[requests.Response] = None
        last_exception: Optional[Exception] = None

        for attempt in range(max_retries + 1):
            try:
                resp = requests.get(endpoint, headers=headers, auth=auth, params=params, timeout=timeout)
                # If primary search/jql endpoint returns 404 or 410, try fallback endpoint
                if resp.status_code in (404, 410) and endpoint != fallback_endpoint:
                    endpoint = fallback_endpoint
                    resp = requests.get(endpoint, headers=headers, auth=auth, params=params, timeout=timeout)
                elif resp.status_code in (404, 410) and endpoint == fallback_endpoint:
                    endpoint = f"{jira_base_url}/rest/api/3/search/jql"
                    resp = requests.get(endpoint, headers=headers, auth=auth, params=params, timeout=timeout)

                last_response = resp

                if resp.status_code == 200:
                    break
                if _is_permanent_error(resp.status_code):
                    break
                if _is_transient_status(resp.status_code) and attempt < max_retries:
                    time.sleep(backoff_factor * (2 ** attempt))
                    continue
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_exception = e
                if attempt < max_retries:
                    time.sleep(backoff_factor * (2 ** attempt))
                    continue
                break
            except Exception as e:
                last_exception = e
                break

        if last_response is not None and last_response.status_code == 200:
            data = last_response.json()
            raw_issues = data.get("issues", [])
            total = data.get("total", len(all_issues) + len(raw_issues))
            next_page_token = data.get("nextPageToken")
            is_last = data.get("isLast", False)

            for raw in raw_issues:
                all_issues.append(_parse_jira_issue_dict(raw, jira_base_url))

            if not fetch_all or is_last or not raw_issues:
                break
            if not next_page_token and len(all_issues) >= total:
                break
            current_start += len(raw_issues)
        else:
            err_msg = "Unknown error occurred while contacting Jira API."
            if last_response is not None:
                err_msg = f"HTTP {last_response.status_code}: {extract_jira_error(last_response)}"
            elif last_exception is not None:
                err_msg = f"Network exception: {str(last_exception)}"

            return {
                "status": "FAILED",
                "error": err_msg,
                "project_key": clean_project,
                "issues": all_issues,
                "total": total
            }

    return {
        "status": "SUCCESS",
        "project_key": clean_project,
        "issues": all_issues,
        "total": total,
        "count": len(all_issues)
    }

def get_jira_issue_details(issue_key: str) -> Dict[str, Any]:
    """
    Retrieve full details for a single Jira issue.
    Never exposes credentials or API token.
    """
    jira_base_url, jira_email, jira_token, _ = get_jira_credentials()
    clean_key = issue_key.strip().upper()

    if not (jira_base_url and jira_email and jira_token):
        return {
            "status": "FAILED",
            "error": "Missing required Jira credentials in .env",
            "issue_key": clean_key,
            "issue": None
        }

    endpoint = f"{jira_base_url}/rest/api/3/issue/{clean_key}"
    headers = get_jira_headers()
    auth = get_jira_auth(jira_email, jira_token)
    timeout = int(os.environ.get("JIRA_TIMEOUT", "20"))

    try:
        resp = requests.get(
            endpoint,
            headers=headers,
            auth=auth,
            params={"fields": "summary,description,status,assignee,duedate,created,updated"},
            timeout=timeout
        )
        if resp.status_code == 200:
            data = resp.json()
            issue_dict = _parse_jira_issue_dict(data, jira_base_url)
            return {
                "status": "SUCCESS",
                "issue_key": clean_key,
                "issue": issue_dict
            }
        else:
            return {
                "status": "FAILED",
                "error": f"HTTP {resp.status_code}: {extract_jira_error(resp)}",
                "issue_key": clean_key,
                "issue": None
            }
    except Exception as e:
        return {
            "status": "FAILED",
            "error": f"Exception connecting to Jira: {str(e)}",
            "issue_key": clean_key,
            "issue": None
        }

def get_jira_issue_status(issue_key: str) -> Dict[str, Any]:
    """
    Retrieve live status and category for a single Jira issue.
    """
    res = get_jira_issue_details(issue_key)
    if res.get("status") == "SUCCESS" and res.get("issue"):
        iss = res["issue"]
        return {
            "status": "SUCCESS",
            "issue_key": iss["key"],
            "status_name": iss["status_name"],
            "status_category": iss["status_category"],
            "sprint_status": iss["sprint_status"],
            "error": None
        }
    return {
        "status": "FAILED",
        "issue_key": issue_key.strip().upper(),
        "status_name": None,
        "status_category": None,
        "sprint_status": None,
        "error": res.get("error", "Unable to retrieve issue status")
    }

def sync_jira_issues_to_sprint_db(
    project_key: str = "SCRUM",
    issues: Optional[List[Dict[str, Any]]] = None,
    meeting_id: str = "jira_sync",
    db_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Synchronize retrieved Jira issue information into sprint_comparison.db.
    Preserves stable task IDs, records history, and prevents duplicate task records.
    """
    from sprint_database import save_or_update_task, DEFAULT_DB as SPRINT_DEFAULT_DB

    target_db = db_path or SPRINT_DEFAULT_DB
    clean_project = project_key.strip().upper()

    # If issues are not pre-supplied, fetch them via Jira API
    if issues is None:
        fetch_res = get_jira_issues_for_project(project_key=clean_project)
        if fetch_res.get("status") != "SUCCESS":
            return {
                "status": "FAILED",
                "error": fetch_res.get("error", "Failed to fetch issues from Jira"),
                "project_key": clean_project,
                "synced_count": 0,
                "tasks": []
            }
        issues = fetch_res.get("issues", [])

    created_count = 0
    updated_count = 0
    synced_tasks = []

    for item in issues:
        key = item.get("key")
        summary = item.get("summary") or "Untitled Task"
        assignee = item.get("assignee_name")
        due_date = item.get("due_date")
        sprint_status = item.get("sprint_status") or map_jira_status_to_sprint_status(
            item.get("status_name"), item.get("status_category")
        )

        evidence = None
        if sprint_status == "COMPLETED":
            evidence = f"Verified completed in Jira ({key}, status: {item.get('status_name', 'Done')})"

        save_res = save_or_update_task(
            project_key=clean_project,
            task_name=summary,
            meeting_id=meeting_id,
            assignee=assignee,
            deadline=due_date,
            jira_issue_key=key,
            status=sprint_status,
            completion_evidence=evidence,
            task_id=key,
            change_reason=f"Synchronized from Jira issue {key} ({item.get('status_name', sprint_status)})",
            db_path=target_db
        )

        if save_res.get("action") == "CREATED":
            created_count += 1
        elif save_res.get("action") == "UPDATED":
            updated_count += 1

        synced_tasks.append(save_res)

    return {
        "status": "SUCCESS",
        "project_key": clean_project,
        "synced_count": len(synced_tasks),
        "created_count": created_count,
        "updated_count": updated_count,
        "tasks": synced_tasks
    }

def get_safe_jira_issue_url(issue_key: Optional[str]) -> Optional[str]:
    """
    Validate Jira issue key and construct a safe issue URL strictly against the configured Jira domain.
    Never accepts or constructs links from arbitrary or user-provided URLs.
    Key must match standard Jira format: ^[A-Z][A-Z0-9]+-\\d+$
    """
    if not issue_key or not isinstance(issue_key, str):
        return None
    clean_key = issue_key.strip().upper()
    if not re.match(r"^[A-Z][A-Z0-9]+-\d+$", clean_key):
        return None

    raw_base, _, _, _ = get_jira_credentials()
    if not raw_base:
        return None

    parsed = urllib.parse.urlparse(raw_base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None

    return f"{raw_base}/browse/{clean_key}"


