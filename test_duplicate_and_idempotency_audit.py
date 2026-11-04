import os
import sys
import unittest
import tempfile
import shutil
import re
from unittest.mock import patch, MagicMock

# Ensure current working directory is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from graph import graph, AgentState, MeetingExtraction, ActionItem
from sprint_database import (
    init_sprint_db,
    get_sprint_connection,
    get_project_tasks,
    get_task_history,
    normalize_task_name
)


class TestDuplicateAndIdempotencyAudit(unittest.TestCase):
    def setUp(self):
        # Create an isolated temporary SQLite database
        self.temp_dir = tempfile.mkdtemp()
        self.test_db = os.path.join(self.temp_dir, "test_audit.db")
        self.orig_db_env = os.environ.get("SPRINT_COMPARISON_DB")
        os.environ["SPRINT_COMPARISON_DB"] = self.test_db
        init_sprint_db(self.test_db)

        # Environment patcher
        self.env_patcher = patch.dict(os.environ, {
            "JIRA_BASE_URL": "https://testdomain.atlassian.net",
            "JIRA_EMAIL": "test@example.com",
            "JIRA_API_TOKEN": "test_token",
            "JIRA_PROJECT_KEY": "AUDIT",
            "EMAIL_NOTIFICATIONS_ENABLED": "False"
        })
        self.env_patcher.start()

        self.jira_sync_patcher = patch(
            "sprint_comparison_engine.get_jira_issues_for_project",
            return_value={"status": "SUCCESS", "issues": []}
        )
        self.jira_sync_patcher.start()

    def tearDown(self):
        self.jira_sync_patcher.stop()
        self.env_patcher.stop()
        if self.orig_db_env is not None:
            os.environ["SPRINT_COMPARISON_DB"] = self.orig_db_env
        else:
            os.environ.pop("SPRINT_COMPARISON_DB", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("graph.send_action_item_notification")
    @patch("graph.create_calendar_events_for_action_items")
    @patch("reminder_service.schedule_reminders")
    @patch("graph.create_jira_issue_api")
    def test_same_transcript_submitted_twice_jira_calls(self, mock_jira_api, mock_reminders, mock_cal, mock_email):
        """1. Audit: verify whether duplicate Jira issues are created when the same transcript is submitted twice."""
        mock_email.return_value = {"status": "mocked"}
        mock_cal.return_value = []
        mock_reminders.return_value = []

        created_counter = [0]
        def fake_jira_create(title, description, project_key=None):
            created_counter[0] += 1
            key = f"AUDIT-{100 + created_counter[0]}"
            return {
                "mode": "real",
                "status": "SUCCESS",
                "http_status": 201,
                "issue_key": key,
                "formatted_text": f"REAL JIRA ISSUE\nIssue Key: {key}\nTitle: {title}\nStatus: Created successfully"
            }
        mock_jira_api.side_effect = fake_jira_create

        analysis = MeetingExtraction(
            summary="Kickoff sync.",
            decisions=[],
            action_items=[
                ActionItem(assignee="Kashif", task="Implement OAuth2 provider", deadline="2026-11-20")
            ],
            jira_tasks=[
                {"title": "Implement OAuth2 provider", "description": "OAuth2 provider details"}
            ],
            github_issues=[]
        )
        transcript = "Alice: Welcome team. Kashif will implement OAuth2 provider by Nov 20. Let's create a Jira ticket."

        # First submission
        state1 = {
            "transcript": transcript,
            "analysis": analysis,
            "project_key": "AUDIT",
            "ticket_result": [],
            "email_notifications": []
        }
        out1 = graph.invoke(state1)
        self.assertEqual(mock_jira_api.call_count, 1, "First run should create 1 Jira ticket.")
        self.assertIn("AUDIT-101", out1["ticket_result"][0])

        # Second submission (identical transcript)
        state2 = {
            "transcript": transcript,
            "analysis": analysis,
            "project_key": "AUDIT",
            "ticket_result": [],
            "email_notifications": []
        }
        out2 = graph.invoke(state2)

        # AUDIT: Check Jira calls and state output
        self.assertEqual(mock_jira_api.call_count, 1, "Second run with identical transcript must NOT create a second Jira ticket.")
        self.assertIn("AUDIT-101", out2["ticket_result"][0], "Second run should reuse the existing Jira issue key.")

    @patch("graph.send_action_item_notification")
    @patch("graph.create_calendar_events_for_action_items")
    @patch("reminder_service.schedule_reminders")
    def test_same_transcript_different_whitespace(self, mock_reminders, mock_cal, mock_email):
        """2. Audit: same transcript with different whitespace should still be recognized as duplicate."""
        mock_email.return_value = {"status": "mocked"}
        mock_cal.return_value = []
        mock_reminders.return_value = []

        analysis = MeetingExtraction(
            summary="Whitespace test sync.",
            decisions=[],
            action_items=[
                ActionItem(assignee="Ali", task="Configure database connection pooling", deadline="2026-11-22")
            ],
            jira_tasks=[],
            github_issues=[]
        )

        t1 = "Alice: Welcome team.\nAli will configure database connection pooling by Nov 22."
        t2 = "   Alice: Welcome team.   \r\n\r\n\nAli will configure database connection pooling by Nov 22.   \n"

        out1 = graph.invoke({"transcript": t1, "analysis": analysis, "project_key": "WS_TEST", "ticket_result": [], "email_notifications": []})
        out2 = graph.invoke({"transcript": t2, "analysis": analysis, "project_key": "WS_TEST", "ticket_result": [], "email_notifications": []})

        conn = get_sprint_connection(self.test_db)
        try:
            meetings = conn.execute("SELECT * FROM meetings WHERE project_key = 'WS_TEST'").fetchall()
            self.assertEqual(len(meetings), 1, "There should be exactly 1 meeting recorded despite whitespace differences.")
            tasks = conn.execute("SELECT * FROM project_tasks WHERE project_key = 'WS_TEST'").fetchall()
            self.assertEqual(len(tasks), 1, "There should be exactly 1 task recorded.")
        finally:
            conn.close()

    @patch("graph.send_action_item_notification")
    @patch("graph.create_calendar_events_for_action_items")
    @patch("reminder_service.schedule_reminders")
    def test_same_transcript_different_meeting_title(self, mock_reminders, mock_cal, mock_email):
        """3. Audit: same transcript with different title should be detected as duplicate by transcript hash."""
        mock_email.return_value = {"status": "mocked"}
        mock_cal.return_value = []
        mock_reminders.return_value = []

        analysis = MeetingExtraction(
            summary="Title test sync.",
            decisions=[],
            action_items=[
                ActionItem(assignee="Ali", task="Set up Redis cluster", deadline="2026-11-25")
            ],
            jira_tasks=[],
            github_issues=[]
        )
        t = "Ali will set up Redis cluster by Nov 25."

        out1 = graph.invoke({"transcript": t, "meeting_title": "Sprint 1 Kickoff", "analysis": analysis, "project_key": "TITLE_TEST", "ticket_result": [], "email_notifications": []})
        out2 = graph.invoke({"transcript": t, "meeting_title": "Sprint 1 Review", "analysis": analysis, "project_key": "TITLE_TEST", "ticket_result": [], "email_notifications": []})

        conn = get_sprint_connection(self.test_db)
        try:
            meetings = conn.execute("SELECT * FROM meetings WHERE project_key = 'TITLE_TEST'").fetchall()
            self.assertEqual(len(meetings), 1, "There should be exactly 1 meeting recorded.")
        finally:
            conn.close()

    @patch("graph.send_action_item_notification")
    @patch("graph.create_calendar_events_for_action_items")
    @patch("reminder_service.schedule_reminders")
    @patch("graph.create_jira_issue_api")
    def test_same_action_item_consecutive_meetings(self, mock_jira_api, mock_reminders, mock_cal, mock_email):
        """4. Audit: same action item in consecutive meetings should reuse existing Jira issue key."""
        mock_email.return_value = {"status": "mocked"}
        mock_cal.return_value = []
        mock_reminders.return_value = []

        created_counter = [0]
        def fake_jira_create(title, description, project_key=None):
            created_counter[0] += 1
            key = f"CONSEC-{100 + created_counter[0]}"
            return {
                "mode": "real",
                "status": "SUCCESS",
                "http_status": 201,
                "issue_key": key,
                "formatted_text": f"REAL JIRA ISSUE\nIssue Key: {key}\nTitle: {title}\nStatus: Created successfully"
            }
        mock_jira_api.side_effect = fake_jira_create

        # Meeting 1 assigns task and creates Jira issue
        analysis1 = MeetingExtraction(
            summary="Meeting 1 kickoff.",
            decisions=[],
            action_items=[
                ActionItem(assignee="Kashif", task="Implement Payment Gateway", deadline="2026-11-25")
            ],
            jira_tasks=[
                {"title": "Implement Payment Gateway", "description": "Payment gateway integration"}
            ],
            github_issues=[]
        )
        t1 = "Meeting 1: Kashif will implement Payment Gateway by Nov 25. Let's create a Jira ticket."
        out1 = graph.invoke({"transcript": t1, "analysis": analysis1, "project_key": "CONSEC", "ticket_result": [], "email_notifications": []})
        self.assertEqual(mock_jira_api.call_count, 1)
        self.assertIn("CONSEC-101", out1["ticket_result"][0])

        # Meeting 2 discusses the SAME task again as still in progress
        analysis2 = MeetingExtraction(
            summary="Meeting 2 sync.",
            decisions=[],
            action_items=[
                ActionItem(assignee="Kashif", task="Implement Payment Gateway", deadline="2026-11-30", progress="Work is still in progress on payment gateway.")
            ],
            jira_tasks=[
                {"title": "Implement Payment Gateway", "description": "Payment gateway integration"}
            ],
            github_issues=[]
        )
        t2 = "Meeting 2: Kashif is still working on Implement Payment Gateway, extended to Nov 30."
        out2 = graph.invoke({"transcript": t2, "analysis": analysis2, "project_key": "CONSEC", "ticket_result": [], "email_notifications": []})

        # AUDIT CHECK: mock_jira_api should NOT be called again for the same task in meeting 2!
        self.assertEqual(mock_jira_api.call_count, 1, "Consecutive meeting discussing same task must NOT create a duplicate Jira ticket.")
        self.assertIn("CONSEC-101", out2["ticket_result"][0], "Existing Jira key must be reused in meeting 2.")

        # Check DB records
        tasks = get_project_tasks(project_key="CONSEC", db_path=self.test_db)
        self.assertEqual(len(tasks), 1, "There should be exactly 1 task record in project_tasks.")
        self.assertEqual(tasks[0]["jira_issue_key"], "CONSEC-101")

        # History should record creation in meeting 1 and deadline change in meeting 2
        hist = get_task_history(tasks[0]["task_id"], db_path=self.test_db)
        self.assertEqual(len(hist), 2, "Task history should record initial assignment (meeting 1) and deadline update (meeting 2).")
        self.assertEqual(hist[-1]["new_deadline"], "2026-11-30")
        self.assertEqual(hist[-1]["previous_deadline"], "2026-11-25")

    @patch("graph.send_action_item_notification")
    @patch("graph.create_calendar_events_for_action_items")
    @patch("reminder_service.schedule_reminders")
    @patch("graph.create_jira_issue_api")
    def test_two_different_tasks_similar_names(self, mock_jira_api, mock_reminders, mock_cal, mock_email):
        """5. Audit: two different tasks with similar names must remain distinct."""
        mock_email.return_value = {"status": "mocked"}
        mock_cal.return_value = []
        mock_reminders.return_value = []

        created_counter = [0]
        def fake_jira_create(title, description, project_key=None):
            created_counter[0] += 1
            key = f"SIM-{100 + created_counter[0]}"
            return {
                "mode": "real",
                "status": "SUCCESS",
                "http_status": 201,
                "issue_key": key,
                "formatted_text": f"REAL JIRA ISSUE\nIssue Key: {key}\nTitle: {title}\nStatus: Created successfully"
            }
        mock_jira_api.side_effect = fake_jira_create

        analysis = MeetingExtraction(
            summary="Deployment sync.",
            decisions=[],
            action_items=[
                ActionItem(assignee="Kashif", task="Deploy authentication service to staging", deadline="2026-11-20"),
                ActionItem(assignee="Ali", task="Deploy authentication service to production", deadline="2026-11-25")
            ],
            jira_tasks=[
                {"title": "Deploy authentication service to staging", "description": "Staging deployment"},
                {"title": "Deploy authentication service to production", "description": "Production deployment"}
            ],
            github_issues=[]
        )
        t = "Kashif will deploy authentication service to staging. Ali will deploy authentication service to production."
        out = graph.invoke({"transcript": t, "analysis": analysis, "project_key": "SIMILAR", "ticket_result": [], "email_notifications": []})

        self.assertEqual(mock_jira_api.call_count, 2, "Both distinct tasks must receive their own Jira issue.")
        tasks = get_project_tasks(project_key="SIMILAR", db_path=self.test_db)
        self.assertEqual(len(tasks), 2, "Both tasks must be saved distinctly.")

    @patch("graph.send_action_item_notification")
    @patch("graph.create_calendar_events_for_action_items")
    @patch("reminder_service.schedule_reminders")
    @patch("graph.create_jira_issue_api")
    def test_retry_after_sprint_comparison_failure(self, mock_jira_api, mock_reminders, mock_cal, mock_email):
        """6. Failure recovery: retry after sprint comparison failure must not duplicate Jira issues."""
        mock_email.return_value = {"status": "mocked"}
        mock_cal.return_value = []
        mock_reminders.return_value = []

        created_counter = [0]
        def fake_jira_create(title, description, project_key=None):
            created_counter[0] += 1
            key = f"RETRY-{100 + created_counter[0]}"
            return {
                "mode": "real",
                "status": "SUCCESS",
                "http_status": 201,
                "issue_key": key,
                "formatted_text": f"REAL JIRA ISSUE\nIssue Key: {key}\nTitle: {title}\nStatus: Created successfully"
            }
        mock_jira_api.side_effect = fake_jira_create

        analysis = MeetingExtraction(
            summary="Retry test sync.",
            decisions=[],
            action_items=[
                ActionItem(assignee="Kashif", task="Set up elasticsearch logging", deadline="2026-11-28")
            ],
            jira_tasks=[
                {"title": "Set up elasticsearch logging", "description": "Logging pipeline"}
            ],
            github_issues=[]
        )
        t = "Kashif will set up elasticsearch logging by Nov 28."

        # Attempt 1: compare_sprint_meetings crashes
        with patch("graph.compare_sprint_meetings", side_effect=RuntimeError("Simulated engine crash")):
            out1 = graph.invoke({"transcript": t, "analysis": analysis, "project_key": "RETRY_FAIL", "ticket_result": [], "email_notifications": []})
            self.assertEqual(mock_jira_api.call_count, 1)
            self.assertEqual(out1["sprint_comparison"]["status"], "FAILED")

        # Attempt 2: Retry the exact same meeting
        out2 = graph.invoke({"transcript": t, "analysis": analysis, "project_key": "RETRY_FAIL", "ticket_result": [], "email_notifications": []})

        # AUDIT CHECK: retry must NOT call Jira API again
        self.assertEqual(mock_jira_api.call_count, 1, "Retry must not create a duplicate Jira ticket.")
        self.assertIn("RETRY-101", out2["ticket_result"][0], "Retry should reuse the issue key created in attempt 1.")


if __name__ == "__main__":
    unittest.main()
