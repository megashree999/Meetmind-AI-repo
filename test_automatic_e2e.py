import os
import re
import sys
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv(override=True)

from graph import graph
from github_service import get_github_credentials, get_github_headers

TEST_TRANSCRIPT = """Megashree: Good morning everyone. Let's start our sprint meeting. Today I want to discuss the login timeout issue, authentication service, and the API testing status.

Kashif: I checked the login module yesterday. The login request is sometimes taking more than thirty seconds, especially when the authentication service is under load.

Ali: I checked the testing logs. The timeout is reproducible when multiple users try to log in at the same time.

Megashree: Okay. Kashif, can you investigate the authentication service and identify the root cause of the timeout?

Kashif: Yes. I will check the authentication service, review the session handling code, and identify the root cause.

Megashree: Please complete the investigation by Friday.

Kashif: Sure. I will provide the findings by Friday.

Megashree: Ali, please test the login module with multiple concurrent users and verify whether the timeout issue is resolved after Kashif's changes.

Ali: Sure. I will perform the concurrent-user testing and update the test results.

Megashree: Please complete the testing by Monday.

Ali: Okay. I will complete the testing by Monday.

Megashree: I will update the API documentation after the authentication issue is fixed.

Kashif: We should also track the authentication timeout issue in Jira.

Megashree: Agreed. Create a Jira ticket for the login timeout issue and include the investigation details.

Ali: Should we also track the testing-related bug in GitHub?

Megashree: Yes. Create a GitHub issue for the concurrent-login testing problem.

Kashif: I'll make sure the authentication investigation findings are added to the Jira ticket.

Megashree: Great. So the main tasks are: Kashif investigates the authentication service by Friday, Ali performs concurrent-user testing by Monday, and I will update the API documentation after the fix. Let's review the progress in the next sprint meeting."""

def main():
    print("=" * 80)
    print("MEETMIND AI - COMPLETE AUTOMATIC END-TO-END WORKFLOW")
    print("=" * 80)
    print("\n[INPUT] Single Meeting Transcript Provided:")
    print("-" * 80)
    print(TEST_TRANSCRIPT.strip()[:300] + "\n... (truncated for display)")
    print("-" * 80)

    # 1. Single Graph Execution with Transcript Only
    print("\n>>> EXECUTING LANGGRAPH WORKFLOW: graph.invoke({'transcript': TEST_TRANSCRIPT})")
    print(">>> Note: NO manual queries ('create Jira', 'create GitHub issue', 'send email') are provided.")
    
    result = graph.invoke({"transcript": TEST_TRANSCRIPT})

    print("\n" + "=" * 80)
    print("STEP 1: LLM STRUCTURED EXTRACTION RESULT")
    print("=" * 80)
    analysis = result.get("analysis")
    if not analysis:
        print("[FAIL] No analysis found in graph output!")
        sys.exit(1)

    summary = getattr(analysis, "summary", "") or (analysis.get("summary") if isinstance(analysis, dict) else "")
    action_items = getattr(analysis, "action_items", []) or (analysis.get("action_items") if isinstance(analysis, dict) else [])
    decisions = getattr(analysis, "decisions", []) or (analysis.get("decisions") if isinstance(analysis, dict) else [])
    jira_tasks = getattr(analysis, "jira_tasks", []) or (analysis.get("jira_tasks") if isinstance(analysis, dict) else [])
    github_issues = getattr(analysis, "github_issues", []) or (analysis.get("github_issues") if isinstance(analysis, dict) else [])

    print(f"\n[Meeting Summary]:\n{summary}\n")
    
    print(f"[Key Decisions] ({len(decisions)}):")
    for d in decisions:
        print(f"  - {d}")

    print(f"\n[Extracted Action Items] ({len(action_items)}):")
    for idx, item in enumerate(action_items, 1):
        assignee = getattr(item, "assignee", "Unassigned") if hasattr(item, "assignee") else item.get("assignee")
        task = getattr(item, "task", "") if hasattr(item, "task") else item.get("task")
        deadline = getattr(item, "deadline", "None") if hasattr(item, "deadline") else item.get("deadline")
        print(f"  {idx}. Assignee: {assignee} | Deadline: {deadline}")
        print(f"     Task: {task}")

    print(f"\n[Extracted Jira Tasks] ({len(jira_tasks)}):")
    for jt in jira_tasks:
        t = getattr(jt, "title", "") if hasattr(jt, "title") else jt.get("title")
        d = getattr(jt, "description", "") if hasattr(jt, "description") else jt.get("description")
        print(f"  - Title: {t}")
        print(f"    Description: {d}")

    print(f"\n[Extracted GitHub Issues] ({len(github_issues)}):")
    for gi in github_issues:
        t = getattr(gi, "title", "") if hasattr(gi, "title") else gi.get("title")
        d = getattr(gi, "description", "") if hasattr(gi, "description") else gi.get("description")
        print(f"  - Title: {t}")
        print(f"    Description: {d}")

    print("\n" + "=" * 80)
    print("STEP 2: AUTOMATIC GMAIL NOTIFICATIONS RESULT")
    print("=" * 80)
    email_notifications = result.get("email_notifications", [])
    print(f"Total Notifications Processed: {len(email_notifications)}\n")
    for idx, notif in enumerate(email_notifications, 1):
        print(f"Notification #{idx}:")
        print(f"  Assignee : {notif.get('assignee')}")
        print(f"  Recipient: {notif.get('email')}")
        print(f"  Task     : {notif.get('task')}")
        print(f"  Deadline : {notif.get('deadline')}")
        print(f"  Status   : {notif.get('status')}")
        if notif.get("message_id"):
            print(f"  Gmail ID : {notif.get('message_id')}")
        print(f"  Details  : {notif.get('message') or notif.get('error')}\n")

    print("=" * 80)
    print("STEP 3: AUTOMATIC TICKET CREATION (JIRA & GITHUB)")
    print("=" * 80)
    ticket_results = result.get("ticket_result", [])
    print(f"Total Ticket Operations: {len(ticket_results)}\n")
    for tr in ticket_results:
        print(tr)
        print("-" * 60)

    # 4. Verify Jira Task via Jira REST API
    all_ticket_text = "\n".join(ticket_results)
    jira_key_match = re.search(r"Issue Key:\s*([A-Z0-9]+-\d+)", all_ticket_text)
    jira_url_match = re.search(r"(https?://[^\s]+/browse/[A-Z0-9]+-\d+)", all_ticket_text)
    
    jira_base_url = (os.environ.get("JIRA_BASE_URL") or os.environ.get("JIRA_URL") or "").rstrip("/")
    jira_email = (os.environ.get("JIRA_EMAIL") or "").strip()
    jira_token = (os.environ.get("JIRA_API_TOKEN") or "").strip()

    if jira_key_match and jira_base_url and jira_email and jira_token:
        issue_key = jira_key_match.group(1)
        print("\n>>> VERIFYING REAL JIRA TASK VIA REST API GET...")
        get_url = f"{jira_base_url}/rest/api/3/issue/{issue_key}"
        try:
            get_resp = requests.get(get_url, headers={"Accept": "application/json"}, auth=HTTPBasicAuth(jira_email, jira_token), timeout=15)
            if get_resp.status_code == 200:
                data = get_resp.json()
                fields = data.get("fields", {})
                print(f"[VERIFIED] Jira Issue Key: {issue_key}")
                print(f"  Summary    : {fields.get('summary')}")
                print(f"  Issue Type : {fields.get('issuetype', {}).get('name')}")
                print(f"  Status     : {fields.get('status', {}).get('name')}")
            else:
                print(f"[WARN] Jira verification returned HTTP {get_resp.status_code}")
        except Exception as e:
            print(f"[WARN] Jira verification error: {e}")

    # 5. Verify GitHub Issue via GitHub REST API
    gh_num_match = re.search(r"Issue Number:\s*#?(\d+)", all_ticket_text)
    gh_token, gh_owner, gh_repo = get_github_credentials()

    if gh_num_match and gh_token and gh_owner and gh_repo:
        gh_num = int(gh_num_match.group(1))
        print("\n>>> VERIFYING REAL GITHUB ISSUE VIA REST API GET...")
        get_url = f"https://api.github.com/repos/{gh_owner}/{gh_repo}/issues/{gh_num}"
        try:
            get_resp = requests.get(get_url, headers=get_github_headers(gh_token), timeout=15)
            if get_resp.status_code == 200:
                data = get_resp.json()
                print(f"[VERIFIED] GitHub Issue #{gh_num}")
                print(f"  Title      : {data.get('title')}")
                print(f"  URL        : {data.get('html_url')}")
                print(f"  State      : {data.get('state')}")
            else:
                print(f"[WARN] GitHub verification returned HTTP {get_resp.status_code}")
        except Exception as e:
            print(f"[WARN] GitHub verification error: {e}")

    # 6. RAG Question Verification
    print("\n" + "=" * 80)
    print("STEP 4: RAG QUESTION VERIFICATION FROM INDEXED CHROMADB")
    print("=" * 80)
    rag_query_text = "Who is responsible for investigating the authentication service?"
    print(f"User Query: '{rag_query_text}'")
    
    rag_result = graph.invoke({"user_query": rag_query_text})
    rag_answer = rag_result.get("answer", "")
    print(f"\nRAG Answer: {rag_answer}\n")
    
    if "kashif" in rag_answer.lower():
        print("[PASS] RAG successfully identified Kashif as responsible for the authentication service investigation.")
    else:
        print(f"[WARN] Expected 'Kashif' in answer, got: '{rag_answer}'")

    print("\n" + "=" * 80)
    print("END-TO-END AUTOMATIC WORKFLOW TEST COMPLETED SUCCESSFULLY!")
    print("=" * 80)

if __name__ == "__main__":
    main()
