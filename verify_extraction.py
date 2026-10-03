import os
from dotenv import load_dotenv
load_dotenv(override=True)

from graph import graph

TEST_TRANSCRIPT = """Megashree: I will review the project architecture and provide feedback by Friday.

Kashif: I will complete the backend authentication API by Monday.

Priya: I will test the authentication API and report any failures by Tuesday."""

def main():
    print(">>> RUNNING ACTUAL EXTRACTION NODE...")
    # We invoke the graph with just the transcript
    result = graph.invoke({"transcript": TEST_TRANSCRIPT})

    analysis = result.get("analysis")
    action_items = getattr(analysis, "action_items", []) or (analysis.get("action_items") if isinstance(analysis, dict) else [])
    
    print(f"\nActionItems count: {len(action_items)}\n")
    for idx, item in enumerate(action_items, 1):
        assignee = getattr(item, "assignee", "Unassigned") if hasattr(item, "assignee") else item.get("assignee")
        task = getattr(item, "task", "") if hasattr(item, "task") else item.get("task")
        deadline = getattr(item, "deadline", "") if hasattr(item, "deadline") else item.get("deadline")
        print(f"{idx}. {assignee}")
        print(f"   Task: {task}")
        print(f"   Deadline: {deadline}")
        
    print("\n>>> DOWNSTREAM EMAIL NOTIFICATIONS...")
    notifications = result.get("email_notifications", [])
    print(f"email_notifications count: {len(notifications)}\n")
    
    for notif in notifications:
        print(f"Recipient Name: {notif.get('assignee')}")
        print(f"Recipient Email: {notif.get('email')}")
        print(f"Task: {notif.get('task')}")
        print(f"Format matching: Check the logs above")
        print(notif.get("body", "No body available"))
        print("-" * 40)

if __name__ == "__main__":
    main()
