from graph import send_email_notifications
import os
import json
from pydantic import BaseModel

class SimulatedActionItem:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

class SimulatedAnalysis:
    def __init__(self, action_items, summary):
        self.action_items = action_items
        self.summary = summary

def test_simulations():
    os.environ["EMAIL_NOTIFICATIONS_ENABLED"] = "false"
    
    # Rich extraction
    item1 = SimulatedActionItem(
        task="Integrate the frontend login dashboard with the authentication API",
        assignee="Arun",
        deadline="Monday",
        context="The team discussed completing the User Authentication Module. The backend authentication endpoint is already available, and the remaining work is the frontend integration.",
        progress="The backend JWT API is completely done and working locally.",
        expected_outcome="Complete the connection between the login dashboard and the existing authentication API.",
        decisions="We will stick to the standard JWT implementation and not use custom tokens for now.",
        next_steps="Complete the frontend integration and verify the login flow.",
        blockers=None
    )
    
    # Sparse extraction
    item2 = SimulatedActionItem(
        task="Write unit tests for backend JWT API",
        assignee="Priya",
        deadline=None,
        context="Backend JWT API",
        progress=None,
        expected_outcome=None,
        decisions=None,
        next_steps=None,
        blockers="CI pipeline is down"
    )
    
    analysis = SimulatedAnalysis([item1, item2], "Sprint Planning Meeting")
    state = {"analysis": analysis}
    
    print("==========================================")
    print("Testing Email Formatting (Simulated Rich vs Sparse)")
    print("==========================================")
    
    email_result = send_email_notifications(state)
    
    notifs = email_result.get("email_notifications", [])
    for n in notifs:
        print(f"\n--- TO: {n.get('email')} ({n.get('assignee')}) ---")
        print(f"STATUS: {n.get('status')}")
        if 'body' in n:
            print("\nEMAIL BODY PREVIEW:")
            print(n['body'].encode('ascii', errors='replace').decode('ascii'))
            print("---------------------------------")

if __name__ == "__main__":
    test_simulations()
