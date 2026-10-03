import os
import sys
from dotenv import load_dotenv

load_dotenv(override=True)

from graph import graph

TEST_TRANSCRIPT = """
Megashree: I will review the project architecture and provide feedback by Friday.

Kashif: I will complete the backend authentication API by Monday.

Priya: I will test the authentication API and report any failures by Tuesday.
"""

def main():
    print("Testing multi email notifications")
    
    # Mock the LLM to return 3 action items
    from graph import MeetingExtraction, ActionItem
    
    mock_analysis = MeetingExtraction(
        summary="Test summary",
        decisions=[],
        action_items=[
            ActionItem(assignee="Megashree", task="Review the project architecture and provide feedback", deadline="Friday", context="The team is currently working on the authentication module.", progress=None, expected_outcome=None, decisions=None, next_steps=None, blockers=None),
            ActionItem(assignee="Kashif", task="Complete the backend authentication API", deadline="Monday", context="The team is currently working on the authentication module.", progress=None, expected_outcome=None, decisions=None, next_steps=None, blockers=None),
            ActionItem(assignee="Priya", task="Test the authentication API and report any failures", deadline="Tuesday", context="The team is currently working on the authentication module.", progress=None, expected_outcome=None, decisions=None, next_steps=None, blockers=None)
        ],
        jira_tasks=[],
        github_issues=[]
    )
    
    # Start with mock analysis in state
    result = graph.invoke({
        "transcript": TEST_TRANSCRIPT,
        "analysis": mock_analysis
    })
    
    analysis = result.get("analysis")
    action_items = getattr(analysis, "action_items", []) or (analysis.get("action_items") if isinstance(analysis, dict) else [])
    
    print(f"\n[Extracted Action Items] ({len(action_items)}):")
    for idx, item in enumerate(action_items, 1):
        assignee = getattr(item, "assignee", "Unassigned") if hasattr(item, "assignee") else item.get("assignee")
        task = getattr(item, "task", "") if hasattr(item, "task") else item.get("task")
        print(f"  {idx}. Assignee: {assignee} | Task: {task}")
        
    notifications = result.get("email_notifications", [])
    print(f"\n[Email Notifications] ({len(notifications)}):")
    for notif in notifications:
        print(f"Assignee: {notif.get('assignee')} | Status: {notif.get('status')} | Email: {notif.get('email')}")

if __name__ == "__main__":
    main()
