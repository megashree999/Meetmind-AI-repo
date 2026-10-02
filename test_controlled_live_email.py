import os
import sys

current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)

from graph import graph

def run_controlled_test():
    transcript = """Megashree: Good morning team. Let's make sure our login module is ready by Friday. We will use JWT authentication.
Kashif: I will implement the backend JWT authentication API by Friday.
Ali: I will test the login flow and create test cases by Thursday.
Megashree: Sounds great, let's proceed with this plan."""

    print("=" * 60)
    print("RUNNING CONTROLLED LIVE GMAIL NOTIFICATION TEST")
    print("=" * 60)

    state = {
        "transcript": transcript,
        "ticket_result": [],
        "email_notifications": []
    }

    final_state = graph.invoke(state)

    notifications = final_state.get("email_notifications", [])
    print(f"\nTotal notifications processed: {len(notifications)}\n")

    for idx, notif in enumerate(notifications, 1):
        print(f"Notification #{idx}:")
        print(f"  Recipient: {notif.get('email')}")
        print(f"  Assignee: {notif.get('assignee')}")
        print(f"  Subject: {notif.get('subject')}")
        print(f"  Status: {notif.get('status')}")
        print(f"  Gmail Message ID: {notif.get('message_id')}")
        print(f"  Details: {notif.get('message') or notif.get('error')}\n")

if __name__ == "__main__":
    run_controlled_test()
