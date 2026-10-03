from graph import extract_meeting
import os
import json

def test_full_transcript_extraction():
    transcript = """
Sprint Planning Meeting

Megashree: Welcome everyone. What is the current status of the API?

Arun: I will complete the authentication API by Friday.

Priya: I will test the authentication API by Monday.

Kashif: I have finished my previous work. I will fix the authentication API by Monday. I will also update the API documentation by Wednesday.

Rahul: Please create a Jira task for fixing the login timeout.

Divya: I will review the database changes by Tuesday.

Sanjay: Please create a GitHub issue for reviewing the authentication code.

Anitha: I will prepare the deployment documentation by Wednesday.
"""

    state = {"transcript": transcript, "user_query": ""}
    
    print("==========================================")
    print("Extracting Meeting (LLM call)")
    print("==========================================")
    
    state = extract_meeting(state)
    
    analysis = state["analysis"]
    
    print("\nExtraction Result:")
    action_items = analysis.action_items
    
    print("\n--- ACTION ITEMS ---")
    for i, item in enumerate(action_items):
        print(f"{i} -> {item.assignee}: {item.task}")
        
    print("\n--- JIRA TASKS ---")
    for task in analysis.jira_tasks:
        print(f"Jira Task: {task.title}")
        
    print("\n--- GITHUB ISSUES ---")
    for issue in analysis.github_issues:
        print(f"GitHub Issue: {issue.title}")

if __name__ == "__main__":
    test_full_transcript_extraction()
