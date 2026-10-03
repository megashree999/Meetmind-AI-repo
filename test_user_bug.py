from graph import send_email_notifications

class MockActionItem:
    def __init__(self, assignee, task, deadline, context):
        self.assignee = assignee
        self.task = task
        self.deadline = deadline
        self.context = context

state = {
    "analysis": {
        "action_items": [
            MockActionItem("Kashif", "Investigate authentication service issue and provide a report", "Monday", "authentication service issue"),
            MockActionItem("Kashif", "Create a GitHub issue for reviewing the authentication code", "None", "None")
        ],
        "summary": "Meeting summary"
    },
    "email_notifications": []
}

result = send_email_notifications(state)

for notif in result.get("email_notifications", []):
    print("--- EMAIL TO", notif["assignee"], "---")
    try:
        print(notif["body"])
    except UnicodeEncodeError:
        print(notif["body"].encode("ascii", "ignore").decode("ascii"))
