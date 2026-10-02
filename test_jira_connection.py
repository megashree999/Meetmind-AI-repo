import os
import sys
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

def verify_jira_connection():
    jira_base_url = (os.environ.get("JIRA_BASE_URL") or os.environ.get("JIRA_URL") or "").rstrip("/")
    jira_email = (os.environ.get("JIRA_EMAIL") or "").strip()
    jira_token = (os.environ.get("JIRA_API_TOKEN") or "").strip()
    project_key = (os.environ.get("JIRA_PROJECT_KEY") or "").strip()

    print("=" * 60)
    print("VERIFYING JIRA CONNECTION")
    print("=" * 60)
    print(f"Jira Base URL : {jira_base_url or 'NOT SET'}")
    print(f"Jira Email    : {jira_email or 'NOT SET'}")
    print(f"Project Key   : {project_key or 'NOT SET'}")
    print(f"API Token     : {'[CONFIGURED]' if jira_token else 'NOT SET'}")
    print("=" * 60)

    if not jira_base_url or not jira_email or not jira_token:
        print("\nERROR: Missing required Jira credentials in .env")
        print("Required: JIRA_BASE_URL (or JIRA_URL), JIRA_EMAIL, JIRA_API_TOKEN")
        return {
            "auth": "FAILED",
            "project_access": "FAILED",
            "project_key": project_key,
            "error": "Missing JIRA_BASE_URL, JIRA_EMAIL, or JIRA_API_TOKEN in .env"
        }

    auth = HTTPBasicAuth(jira_email, jira_token)
    headers = {
        "Accept": "application/json"
    }

    # 1. Test Authentication via /myself
    auth_status = "FAILED"
    project_access_status = "FAILED"
    error_message = None

    myself_url = f"{jira_base_url}/rest/api/3/myself"
    try:
        response = requests.get(myself_url, headers=headers, auth=auth, timeout=15)
        if response.status_code == 200:
            user_data = response.json()
            display_name = user_data.get("displayName", "Unknown")
            print(f"\n[PASS] Jira Authentication: SUCCESS (Authenticated as: {display_name})")
            auth_status = "SUCCESS"
        else:
            err = f"HTTP {response.status_code}: {response.text}"
            print(f"\n[FAIL] Jira Authentication: FAILED ({err})")
            error_message = f"Auth failed with status {response.status_code}"
            return {
                "auth": "FAILED",
                "project_access": "FAILED",
                "project_key": project_key,
                "error": error_message
            }
    except Exception as e:
        error_message = f"Connection error during authentication: {e}"
        print(f"\n[FAIL] Jira Authentication: FAILED ({error_message})")
        return {
            "auth": "FAILED",
            "project_access": "FAILED",
            "project_key": project_key,
            "error": error_message
        }

    # 2. Test Project Access via /project/{project_key}
    if not project_key:
        print("\n[FAIL] Project Access: FAILED (JIRA_PROJECT_KEY not specified in .env)")
        return {
            "auth": auth_status,
            "project_access": "FAILED",
            "project_key": "NOT SET",
            "error": "JIRA_PROJECT_KEY is missing in .env"
        }

    project_url = f"{jira_base_url}/rest/api/3/project/{project_key}"
    try:
        proj_response = requests.get(project_url, headers=headers, auth=auth, timeout=15)
        if proj_response.status_code == 200:
            proj_data = proj_response.json()
            proj_name = proj_data.get("name", "Unknown")
            proj_id = proj_data.get("id", "Unknown")
            print(f"[PASS] Project Access: SUCCESS (Project Name: '{proj_name}', ID: {proj_id})")
            project_access_status = "SUCCESS"
        elif proj_response.status_code == 404:
            error_message = f"Project with key '{project_key}' not found or access denied (HTTP 404)"
            print(f"[FAIL] Project Access: FAILED ({error_message})")
        else:
            error_message = f"HTTP {proj_response.status_code}: {proj_response.text}"
            print(f"[FAIL] Project Access: FAILED ({error_message})")
    except Exception as e:
        error_message = f"Connection error during project check: {e}"
        print(f"[FAIL] Project Access: FAILED ({error_message})")

    print("\n" + "=" * 60)
    print(f"Jira authentication: {auth_status}")
    print(f"Project access: {project_access_status}")
    print(f"Project key: {project_key}")
    print(f"Any error: {error_message or 'None'}")
    print("=" * 60)

    return {
        "auth": auth_status,
        "project_access": project_access_status,
        "project_key": project_key,
        "error": error_message
    }

if __name__ == "__main__":
    res = verify_jira_connection()
    sys.exit(0 if (res["auth"] == "SUCCESS" and res["project_access"] == "SUCCESS") else 1)
