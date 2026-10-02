import os
from typing import Optional, Dict, Any, Tuple
import requests
from dotenv import load_dotenv

load_dotenv()

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"

def get_github_credentials() -> Tuple[str, str, str]:
    """
    Retrieve GitHub credentials from environment variables safely.
    Returns (token, owner, repo).
    Never logs or exposes the token.
    """
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    owner = (os.environ.get("GITHUB_OWNER") or "").strip()
    repo = (os.environ.get("GITHUB_REPO") or "").strip()

    # If owner is not explicitly set but repo is in 'owner/repo' format, parse it
    if not owner and "/" in repo:
        parts = repo.split("/", 1)
        owner = parts[0].strip()
        repo = parts[1].strip()

    return token, owner, repo

def get_github_headers(token: str) -> Dict[str, str]:
    """Generate standard GitHub API headers."""
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION
    }

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

    # 1. Verify Authentication via GET /user
    try:
        user_resp = requests.get(
            f"{GITHUB_API_BASE}/user",
            headers=headers,
            timeout=15
        )
        if user_resp.status_code == 200:
            user_data = user_resp.json()
            result["auth_status"] = "SUCCESS"
            result["authenticated_user"] = user_data.get("login")
        else:
            result["error"] = f"Authentication failed with HTTP {user_resp.status_code}: {user_resp.text[:200]}"
            return result
    except Exception as e:
        result["error"] = f"Authentication request exception: {str(e)}"
        return result

    # 2. Verify Repository Access via GET /repos/{owner}/{repo}
    if not owner or not repo:
        result["error"] = "Missing GITHUB_OWNER or GITHUB_REPO in .env"
        return result

    try:
        repo_resp = requests.get(
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}",
            headers=headers,
            timeout=15
        )
        if repo_resp.status_code == 200:
            repo_data = repo_resp.json()
            result["repo_status"] = "SUCCESS"
            result["repo_full_name"] = repo_data.get("full_name")
            result["repo_private"] = repo_data.get("private", False)
            result["has_issues"] = repo_data.get("has_issues", True)
        elif repo_resp.status_code == 404:
            result["error"] = f"Repository '{owner}/{repo}' not found (HTTP 404). Check GITHUB_OWNER and GITHUB_REPO, or ensure the token has permission to access it."
        else:
            result["error"] = f"Repository access failed with HTTP {repo_resp.status_code}: {repo_resp.text[:200]}"
    except Exception as e:
        result["error"] = f"Repository access request exception: {str(e)}"

    return result

def create_github_issue_api(title: str, body: str) -> Dict[str, Any]:
    """
    Create a real GitHub issue via GitHub REST API, or return mock fallback.
    Never exposes GITHUB_TOKEN.
    """
    token, owner, repo = get_github_credentials()

    # Mock mode fallback if credentials are missing
    if not (token and owner and repo):
        formatted_text = (
            f"MOCK GITHUB ISSUE\n"
            f"Title: {title}\n"
            f"Description: {body}\n"
            f"Status: Simulated successfully (GitHub credentials not configured)"
        )
        return {
            "mode": "mock",
            "status": "simulated",
            "title": title,
            "description": body,
            "formatted_text": formatted_text
        }

    # Real GitHub Issue Creation via POST /repos/{owner}/{repo}/issues
    headers = get_github_headers(token)
    payload = {
        "title": title,
        "body": body
    }

    try:
        resp = requests.post(
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues",
            headers=headers,
            json=payload,
            timeout=20
        )
        if resp.status_code in (200, 201):
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
                "status": "created",
                "http_status": resp.status_code,
                "issue_number": issue_number,
                "issue_url": issue_url,
                "title": title,
                "formatted_text": formatted_text
            }
        else:
            # Fallback on API failure
            err_msg = resp.text[:200]
            formatted_text = (
                f"MOCK GITHUB ISSUE (FALLBACK)\n"
                f"Title: {title}\n"
                f"Description: {body}\n"
                f"HTTP Status: {resp.status_code}\n"
                f"Error: {err_msg}\n"
                f"Status: Fallback simulation"
            )
            return {
                "mode": "fallback",
                "status": "failed",
                "http_status": resp.status_code,
                "error": err_msg,
                "formatted_text": formatted_text
            }
    except Exception as e:
        formatted_text = (
            f"MOCK GITHUB ISSUE (FALLBACK)\n"
            f"Title: {title}\n"
            f"Description: {body}\n"
            f"Error: {str(e)}\n"
            f"Status: Fallback simulation"
        )
        return {
            "mode": "fallback",
            "status": "exception",
            "error": str(e),
            "formatted_text": formatted_text
        }
