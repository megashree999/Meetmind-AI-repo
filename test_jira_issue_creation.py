import os
import re
import sys
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

from graph import graph

def run_test():
    print("=" * 60)
    print("MEETMIND AI - REAL JIRA ISSUE CREATION TEST")
    print("=" * 60)

    jira_base_url = (os.environ.get("JIRA_BASE_URL") or os.environ.get("JIRA_URL") or "").rstrip("/")
    jira_email = (os.environ.get("JIRA_EMAIL") or "").strip()
    jira_token = (os.environ.get("JIRA_API_TOKEN") or "").strip()
    project_key = (os.environ.get("JIRA_PROJECT_KEY") or "SCRUM").strip()

    print(f"Jira Base URL : {jira_base_url}")
    print(f"Jira Email    : {jira_email}")
    print(f"Project Key   : {project_key}")
    print(f"API Token     : {'[CONFIGURED]' if jira_token else 'NOT SET'}")
    print("-" * 60)

    if not jira_base_url or not jira_email or not jira_token:
        print("ERROR: Missing required Jira credentials in .env")
        sys.exit(1)

    # Prepare input state to test through LangGraph workflow
    inputs = {
        "user_query": "Create a Jira ticket for MeetMind AI Integration Test. Description: Test issue created by the MeetMind AI LangGraph Jira integration.",
        "analysis": None,
        "transcript": None,
        "ticket_result": [],
        "email_notifications": []
    }

    print("Executing LangGraph with Jira ticket creation request...")
    result = graph.invoke(inputs)

    ticket_results = result.get("ticket_result", [])
    print("\n--- LangGraph Ticket Result ---")
    for tr in ticket_results:
        print(tr)
    print("-" * 60)

    # Extract issue key, URL, status code
    full_output = "\n".join(ticket_results)
    
    key_match = re.search(r"Issue Key:\s*([A-Z0-9]+-\d+)", full_output)
    url_match = re.search(r"Issue URL:\s*(https?://[^\s]+)", full_output)
    status_match = re.search(r"HTTP Status:\s*(\d+)", full_output)

    issue_key = key_match.group(1) if key_match else None
    issue_url = url_match.group(1) if url_match else None
    http_status = int(status_match.group(1)) if status_match else None

    if not issue_key or not issue_url or not http_status:
        print("\n[ERROR] Failed to create or parse Jira issue details from output.")
        sys.exit(1)

    print("\n--- Created Issue Details ---")
    print(f"Jira issue key : {issue_key}")
    print(f"Jira issue URL : {issue_url}")
    print(f"HTTP status    : {http_status}")

    # Requirement 13: Verify that the created issue can be retrieved from Jira
    print("\n--- Verifying Created Issue via Jira REST API GET ---")
    get_url = f"{jira_base_url}/rest/api/3/issue/{issue_key}"
    headers = {"Accept": "application/json"}
    auth = HTTPBasicAuth(jira_email, jira_token)

    try:
        get_resp = requests.get(get_url, headers=headers, auth=auth, timeout=15)
        print(f"Retrieval HTTP Status: {get_resp.status_code}")
        if get_resp.status_code == 200:
            issue_data = get_resp.json()
            fields = issue_data.get("fields", {})
            summary = fields.get("summary")
            issuetype = fields.get("issuetype", {}).get("name")
            proj = fields.get("project", {}).get("key")
            status_name = fields.get("status", {}).get("name")

            print(f"[VERIFIED] Issue retrieved successfully!")
            print(f"  Summary    : {summary}")
            print(f"  Issue Type : {issuetype}")
            print(f"  Project    : {proj}")
            print(f"  Status     : {status_name}")
            print("=" * 60)
            print("JIRA INTEGRATION TEST PASSED SUCCESSFULLY")
            print("=" * 60)
        else:
            print(f"[FAIL] Could not retrieve issue: HTTP {get_resp.status_code}")
            print(get_resp.text[:300])
            sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Exception while retrieving issue: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    run_test()
