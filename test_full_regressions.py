import json
import os
from graph import workflow

def run_regression_test():
    transcript = """Project Release Planning Meeting

Kashif: I will complete the backend release API and deploy it to the staging server by Friday.

Ali: I will update the production database schema and run the required migrations by Thursday.

Megashree: I will integrate the release API with the frontend deployment dashboard and complete it by Monday.

Kashif: I will also prepare the API deployment documentation for the development team.

Priya: I will test the complete release workflow on the staging server and report any failures by Wednesday.

Ali: Please create a GitHub issue to track the database migration testing.

Megashree: Please create a Jira ticket for the frontend deployment dashboard integration.

Kashif: After staging validation is completed, the team should perform a final release verification."""

    print("--- Running Top-to-Bottom Regression Test ---")
    
    app = workflow.compile()
    
    # We set up a state mimicking the input to graph
    initial_state = {
        "transcript": transcript,
        "user_query": "",
        "analysis": None,
        "ticket_result": [],
        "email_notifications": []
    }
    
    try:
        final_state = app.invoke(initial_state)
    except Exception as e:
        print(f"Graph execution failed: {e}")
        return

    print("\n--- RESULTS ---")
    analysis = final_state.get("analysis")
    
    print("\nAction Items:")
    if analysis and hasattr(analysis, "action_items"):
        for item in analysis.action_items:
            print(f" - Assignee: {item.assignee}")
            print(f"   Task: {item.task}")
            print(f"   Deadline: {item.deadline}")
            print(f"   Task Size: {item.task_size}")
            
    print("\nEmail Notifications:")
    for email in final_state.get("email_notifications", []):
        print(json.dumps(email, indent=2, ensure_ascii=True))
        
    print("\nTickets:")
    for ticket in final_state.get("ticket_result", []):
        print(ticket)

if __name__ == "__main__":
    run_regression_test()
