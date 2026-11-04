import os
import time
from typing import Optional, Dict, Any, Tuple
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=False)

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"

def get_github_credentials() -> Tuple[str, str, str]:
    """
    Retrieve GitHub credentials from environment variables safely.
    Returns (token, owner, repo).
    Never logs or exposes the token.
    Strips surrounding whitespace, quotes, and accidental prefixes.
    """
    raw_token = os.environ.get("GITHUB_TOKEN") or ""
    token = raw_token.strip().strip("\"'")

    # Clean accidental prefix if present in env var
    if token.startswith("Bearer "):
        token = token[7:].strip()
    elif token.startswith("token "):
        token = token[6:].strip()

    raw_owner = os.environ.get("GITHUB_OWNER") or ""
    owner = raw_owner.strip().strip("\"'")

    raw_repo = os.environ.get("GITHUB_REPO") or ""
    repo = raw_repo.strip().strip("\"'")

    # If owner is not explicitly set but repo is in 'owner/repo' format, parse it
    if not owner and "/" in repo:
        parts = repo.split("/", 1)
        owner = parts[0].strip()
        repo = parts[1].strip()

    return token, owner, repo

def get_github_headers(token: str) -> Dict[str, str]:
    """Generate standard GitHub API headers with Authorization: Bearer <token>."""
    cleaned_token = token.strip().strip("\"'")
    if cleaned_token.startswith("Bearer "):
        cleaned_token = cleaned_token[7:].strip()
    elif cleaned_token.startswith("token "):
        cleaned_token = cleaned_token[6:].strip()

    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {cleaned_token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION
    }

def extract_github_error(resp: requests.Response) -> str:
    """Extract descriptive error message from GitHub REST API response."""
    try:
        data = resp.json()
        messages = []
        if isinstance(data, dict):
            if data.get("message"):
                messages.append(data["message"])
            errors = data.get("errors")
            if isinstance(errors, list):
                for err in errors:
                    if isinstance(err, dict):
                        field = err.get("field")
                        code = err.get("code")
                        msg = err.get("message")
                        desc = msg or f"field '{field}' error ({code})"
                        messages.append(desc)
                    elif isinstance(err, str):
                        messages.append(err)
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
    return status_code in (400, 401, 403, 404, 422)

def verify_github_connection() -> Dict[str, Any]:
    """
    Verify GitHub authentication and repository access.
    DOES NOT create any issues.
    Never exposes GITHUB_TOKEN in logs or returned error messages.
    """
    token, owner, repo = get_github_credentials()

    result = {
        "auth_status": "FAILED",
        "repo_status": "FAILED",
        "owner": owner or "NOT SET",
        "repo": repo or "NOT SET",
        "authenticated_user": None,
        "error": None
    }

    if not token:
        result["error"] = "Missing GITHUB_TOKEN in .env"
        return result

    headers = get_github_headers(token)
    timeout = int(os.environ.get("GITHUB_TIMEOUT", "20"))

    # 1. Verify Authentication via GET /user
    try:
        user_resp = requests.get(
            f"{GITHUB_API_BASE}/user",
            headers=headers,
            timeout=timeout
        )
        if user_resp.status_code == 200:
            user_data = user_resp.json()
            result["auth_status"] = "SUCCESS"
            result["authenticated_user"] = user_data.get("login")
        elif user_resp.status_code == 401:
            result["error"] = (
                "Authentication failed (HTTP 401 Unauthorized: Bad credentials). "
                "The GitHub Personal Access Token is invalid, expired, revoked, or incorrectly configured. "
                "Please verify or regenerate GITHUB_TOKEN in your .env file."
            )
            return result
        elif user_resp.status_code == 403:
            result["error"] = f"GitHub API access forbidden (HTTP 403): {extract_github_error(user_resp)}"
            return result
        else:
            result["error"] = f"Authentication check failed with HTTP {user_resp.status_code}: {extract_github_error(user_resp)}"
            return result
    except requests.exceptions.Timeout:
        result["error"] = f"Authentication check timed out after {timeout}s connecting to GitHub API."
        return result
    except requests.exceptions.ConnectionError as e:
        result["error"] = f"Network connection error to GitHub API: {str(e)}"
        return result
    except Exception as e:
        result["error"] = f"Unexpected exception during GitHub authentication check: {str(e)}"
        return result

    # 2. Verify Repository Access via GET /repos/{owner}/{repo}
    if not owner or not repo:
        result["error"] = "Missing GITHUB_OWNER or GITHUB_REPO in .env"
        return result

    try:
        repo_resp = requests.get(
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}",
            headers=headers,
            timeout=timeout
        )
        if repo_resp.status_code == 200:
            repo_data = repo_resp.json()
            result["repo_status"] = "SUCCESS"
            result["repo_full_name"] = repo_data.get("full_name")
            result["repo_private"] = repo_data.get("private", False)
            result["has_issues"] = repo_data.get("has_issues", True)
            if not result["has_issues"]:
                result["error"] = f"Repository '{owner}/{repo}' has Issues disabled in repository settings."
        elif repo_resp.status_code == 404:
            result["error"] = (
                f"Repository '{owner}/{repo}' not found (HTTP 404). "
                f"Check GITHUB_OWNER and GITHUB_REPO in .env, or verify your token has permission to access private repositories."
            )
        elif repo_resp.status_code == 403:
            result["error"] = (
                f"Access denied to repository '{owner}/{repo}' (HTTP 403 Forbidden). "
                f"Verify that your token has repository access and 'Issues: Read and write' permissions."
            )
        else:
            result["error"] = f"Repository access check failed with HTTP {repo_resp.status_code}: {extract_github_error(repo_resp)}"
    except requests.exceptions.Timeout:
        result["error"] = f"Repository check timed out after {timeout}s connecting to GitHub API."
    except requests.exceptions.ConnectionError as e:
        result["error"] = f"Network connection error to GitHub API: {str(e)}"
    except Exception as e:
        result["error"] = f"Unexpected exception during GitHub repository check: {str(e)}"

    return result

def create_github_issue_api(title: str, body: str) -> Dict[str, Any]:
    """
    Create a real GitHub issue via GitHub REST API with configurable timeout and exponential backoff.
    Never exposes GITHUB_TOKEN.
    
    Returns structured dict with:
      - mode: 'real' | 'mock'
      - status: 'SUCCESS' | 'FAILED' | 'SIMULATED'
      - http_status: Optional[int]
      - issue_number: Optional[int]
      - issue_url: Optional[str]
      - error: Optional[str]
      - formatted_text: str
    """
    token, owner, repo = get_github_credentials()

    # Mock mode fallback if credentials are not configured
    if not (token and owner and repo):
        formatted_text = (
            f"SIMULATED GITHUB ISSUE\n"
            f"Title: {title}\n"
            f"Description: {body}\n"
            f"Status: SIMULATED (GitHub credentials not configured)"
        )
        return {
            "mode": "mock",
            "status": "SIMULATED",
            "title": title,
            "description": body,
            "formatted_text": formatted_text
        }

    headers = get_github_headers(token)
    payload = {
        "title": title,
        "body": body or "No description provided"
    }

    endpoint = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues"
    timeout = int(os.environ.get("GITHUB_TIMEOUT", "20"))
    max_retries = int(os.environ.get("GITHUB_MAX_RETRIES", "2"))
    backoff_factor = float(os.environ.get("GITHUB_BACKOFF_FACTOR", "1.5"))

    last_response: Optional[requests.Response] = None
    last_exception: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=timeout
            )
            last_response = resp

            # Successful issue creation (200, 201, 202)
            if resp.status_code in (200, 201, 202):
                issue_data = resp.json()
                issue_number = issue_data.get("number")
                issue_url = issue_data.get("html_url")
                formatted_text = (
                    f"REAL GITHUB ISSUE\n"
                    f"Issue Number: #{issue_number}\n"
                    f"Issue URL: {issue_url}\n"
                    f"HTTP Status: {resp.status_code}\n"
                    f"Title: {title}\n"
                    f"Status: Created successfully"
                )
                return {
                    "mode": "real",
                    "status": "SUCCESS",
                    "http_status": resp.status_code,
                    "issue_number": issue_number,
                    "issue_url": issue_url,
                    "title": title,
                    "formatted_text": formatted_text
                }

            # Permanent client error: DO NOT RETRY
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
        err_details = extract_github_error(last_response)

        if http_status == 401:
            err_msg = (
                "HTTP 401 Unauthorized: Bad credentials. "
                "The GitHub Personal Access Token is invalid, expired, revoked, or incorrectly configured. "
                "Please verify or regenerate GITHUB_TOKEN in your .env file."
            )
        elif http_status == 403:
            err_msg = (
                f"HTTP 403 Forbidden: Insufficient permissions or rate limit exceeded. "
                f"Verify that your token has 'Issues: Read and write' permissions for '{owner}/{repo}' ({err_details})."
            )
        elif http_status == 404:
            err_msg = (
                f"HTTP 404 Not Found: Repository '{owner}/{repo}' not found. "
                f"Verify GITHUB_OWNER and GITHUB_REPO, and ensure your token has permission to access it."
            )
        elif http_status == 422:
            err_msg = f"HTTP 422 Unprocessable Entity: Validation failed ({err_details}). Check issue title and body."
        elif http_status == 429:
            err_msg = "HTTP 429 Too Many Requests: GitHub API rate limit reached."
        elif http_status in (500, 502, 503, 504):
            err_msg = f"HTTP {http_status} Server Error: GitHub API is temporarily unavailable ({err_details})."
        else:
            err_msg = f"HTTP {http_status} Error: {err_details}"
    elif last_exception is not None:
        http_status = None
        if isinstance(last_exception, requests.exceptions.Timeout):
            err_msg = f"HTTPSConnectionPool Read timed out after {timeout}s connecting to GitHub API."
        elif isinstance(last_exception, requests.exceptions.ConnectionError):
            err_msg = f"Network connection error connecting to GitHub API: {str(last_exception)}"
        else:
            err_msg = f"Request exception connecting to GitHub: {str(last_exception)}"
    else:
        http_status = None
        err_msg = "Unknown error occurred while contacting GitHub API."

    # Return clear FAILED status (do not mask as simulated success)
    status_code_str = str(http_status) if http_status is not None else "TIMEOUT/CONNECTION_ERROR"
    formatted_text = (
        f"FAILED GITHUB ISSUE (API ERROR)\n"
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
