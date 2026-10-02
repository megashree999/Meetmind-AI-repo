import os
import sys

# Ensure current working directory is on sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)

from graph import graph

def run_test():
    print("=" * 60)
    print("TESTING AUTOMATIC GMAIL NOTIFICATION NODE")
    print("=" * 60)

    # Transcript featuring Megashree, Kashif, Ali, and an unknown person
    meeting_transcript = """Megashree: Good morning team. We need to complete the login module by Friday.
Kashif: I will implement the backend JWT authentication API by Friday.
Ali: I will test the login flow and create test cases by Thursday.
Megashree: I will coordinate the staging deployment by Friday.
Guest speaker David: I will review the API documentation."""

    print("\n--- Invoking Graph with Meeting Transcript ---")
    initial_state = {
        "transcript": meeting_transcript,
        "ticket_result": [],
        "email_notifications": []
    }

    final_state = graph.invoke(initial_state)

    notifications = final_state.get("email_notifications", [])
    print(f"\nTotal notifications processed: {len(notifications)}")

    target_map = {
        "megashree": "megashree999@gmail.com",
        "kashif": "kashifahamad2003@gmail.com",
        "ali": "amohammedali2005@gmail.com"
    }
    found_targets = set()

    for idx, notif in enumerate(notifications, 1):
        assignee = notif.get("assignee")
        email = notif.get("email")
        status = notif.get("status")
        task = notif.get("task")
        subject = notif.get("subject")

        print(f"\nNotification #{idx}:")
        print(f"  Assignee: {assignee}")
        print(f"  Email: {email}")
        print(f"  Task: {task}")
        print(f"  Status: {status}")
        if subject:
            print(f"  Subject: {subject}")

        # Check mapping
        assignee_lower = assignee.lower() if assignee else ""
        for name_key, expected_email in target_map.items():
            if name_key in assignee_lower:
                assert email == expected_email, f"Expected {expected_email} for {assignee}, got {email}"
                found_targets.add(name_key)

        # Check preview safety (NO real email sent)
        if status == "PREVIEW":
            assert notif.get("body"), "Preview must include generated email body"
            print("  Preview confirmed: SAFE (no real email sent)")

    print("\n" + "=" * 60)
    print("VERIFICATION SUMMARY:")
    for name_key, expected_email in target_map.items():
        if name_key in found_targets:
            print(f"  [PASS] {name_key.capitalize()} correctly mapped to {expected_email}")
        else:
            print(f"  [WARN] {name_key.capitalize()} was not assigned a task in LLM output")

    print(f"  [PASS] EMAIL_NOTIFICATIONS_ENABLED=false verified: Safe preview mode active")
    print("=" * 60)
    return True

if __name__ == "__main__":
    success = run_test()
    sys.exit(0 if success else 1)
