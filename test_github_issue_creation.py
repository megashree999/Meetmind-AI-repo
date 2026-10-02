import os
import re
import sys
import requests
from dotenv import load_dotenv

load_dotenv(override=True)

from github_service import get_github_credentials, get_github_headers
from graph import graph

def run_test():
    print("=" * 60)
    print("MEETMIND AI - REAL GITHUB ISSUE CREATION TEST")
    print("=" * 60)

    token, owner, repo = get_github_credentials()

    print(f"GITHUB_OWNER : {owner or 'NOT SET'}")
    print(f"GITHUB_REPO  : {repo or 'NOT SET'}")
    print(f"GITHUB_TOKEN : {'[CONFIGURED]' if token else 'NOT SET'}")
    print("-" * 60)

    if not token or not owner or not repo:
        print("ERROR: Missing required GitHub credentials in .env")
        sys.exit(1)

    # 1. Execute LangGraph with GitHub issue creation request
    inputs = {
        "user_query": "Create a GitHub issue for MeetMind AI Integration Test. Description: Test issue created by the MeetMind AI LangGraph GitHub integration.",
        "analysis": None,
        "transcript": None,
        "ticket_result": [],
        "email_notifications": []
    }

    print("Executing LangGraph with GitHub ticket creation request...")
    result = graph.invoke(inputs)

    ticket_results = result.get("ticket_result", [])
    print("\n--- LangGraph Ticket Result ---")
    for tr in ticket_results:
        print(tr)
    print("-" * 60)

    # 2. Extract issue number, URL, and HTTP status code
    full_output = "\n".join(ticket_results)
    
    num_match = re.search(r"Issue Number:\s*#?(\d+)", full_output)
    url_match = re.search(r"Issue URL:\s*(https?://[^\s]+)", full_output)
    status_match = re.search(r"HTTP Status:\s*(\d+)", full_output)

    issue_number = int(num_match.group(1)) if num_match else None
    issue_url = url_match.group(1) if url_match else None
    http_status = int(status_match.group(1)) if status_match else None

    if not issue_number or not issue_url or not http_status:
        print("\n[ERROR] Failed to extract created GitHub issue details from LangGraph output.")
        sys.exit(1)

    print("\n--- Created Issue Details ---")
    print(f"Issue number : #{issue_number}")
    print(f"Issue URL    : {issue_url}")
    print(f"HTTP status  : {http_status}")

    # 3. Requirement 7: Verify created issue using GET
    print("\n--- Verifying Created Issue via GitHub REST API GET ---")
    get_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}"
    headers = get_github_headers(token)

    try:
        get_resp = requests.get(get_url, headers=headers, timeout=15)
        print(f"Retrieval HTTP Status: {get_resp.status_code}")
        if get_resp.status_code == 200:
            issue_data = get_resp.json()
            title = issue_data.get("title")
            body = issue_data.get("body")
            state = issue_data.get("state")
            user = issue_data.get("user", {}).get("login")

            print(f"[VERIFIED] GitHub issue verified successfully!")
            print(f"  Title   : {title}")
            print(f"  Body    : {body}")
            print(f"  State   : {state}")
            print(f"  Creator : {user}")
            print("=" * 60)
            print("GITHUB INTEGRATION TEST PASSED SUCCESSFULLY")
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
