import os
import sys
import unittest
import tempfile
import shutil
from unittest.mock import patch, MagicMock

# Ensure current working directory is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from graph import graph, AgentState, MeetingExtraction, ActionItem
from sprint_database import init_sprint_db, get_sprint_connection, get_project_tasks
from sprint_comparison_engine import compare_sprint_meetings


class TestSprintGraphIntegration(unittest.TestCase):
    def setUp(self):
        # Create an isolated temporary SQLite database for sprint comparison tests
        self.temp_dir = tempfile.mkdtemp()
        self.test_db = os.path.join(self.temp_dir, "test_sprint_graph.db")
        self.orig_db_env = os.environ.get("SPRINT_COMPARISON_DB")
        os.environ["SPRINT_COMPARISON_DB"] = self.test_db
        init_sprint_db(self.test_db)

        # Environment patcher for Jira/GitHub
        self.env_patcher = patch.dict(os.environ, {
            "JIRA_BASE_URL": "https://testdomain.atlassian.net",
            "JIRA_EMAIL": "test@example.com",
            "JIRA_API_TOKEN": "test_token",
            "JIRA_PROJECT_KEY": "SCRUM",
            "EMAIL_NOTIFICATIONS_ENABLED": "False"
        })
        self.env_patcher.start()

        # Patch Jira read sync to return empty issues by default in tests
        self.jira_sync_patcher = patch("sprint_comparison_engine.get_jira_issues_for_project", return_value={"status": "SUCCESS", "issues": []})
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
    def test_first_meeting_no_previous(self, mock_reminders, mock_cal, mock_email):
        """1. First meeting with no previous meeting returns NEWLY ASSIGNED and previous_meeting_id: None."""
        mock_email.return_value = {"status": "mocked"}
        mock_cal.return_value = []
        mock_reminders.return_value = []

        analysis = MeetingExtraction(
            summary="Kickoff sprint discussion on architecture.",
            decisions=["Use PostgreSQL"],
            action_items=[
                ActionItem(assignee="Kashif", task="Set up auth service database", deadline="2026-11-20"),
                ActionItem(assignee="Ali", task="Design API schema specifications", deadline="2026-11-22")
            ],
            jira_tasks=[],
            github_issues=[]
        )

        transcript = "Alice: Welcome team. Kashif will set up auth service database by Nov 20. Ali will design API schema specifications by Nov 22."
        initial_state = {
            "transcript": transcript,
            "analysis": analysis,
            "project_key": "PROJA",
            "ticket_result": [],
            "email_notifications": []
        }

        output = graph.invoke(initial_state)

        # Verify sprint_comparison in state
        sprint_comp = output.get("sprint_comparison")
        self.assertIsNotNone(sprint_comp)
        self.assertEqual(sprint_comp.get("project_key"), "PROJA")
        self.assertIsNone(sprint_comp.get("previous_meeting_id"))
        self.assertEqual(sprint_comp["summary_counts"]["newly_assigned"], 2)
        self.assertEqual(sprint_comp["summary_counts"]["completed"], 0)
        self.assertEqual(sprint_comp["summary_counts"]["pending"], 0)

    @patch("graph.send_action_item_notification")
    @patch("graph.create_calendar_events_for_action_items")
    @patch("reminder_service.schedule_reminders")
    def test_second_meeting_detects_completed_pending_changed(self, mock_reminders, mock_cal, mock_email):
        """2. Second meeting compares against first meeting: detects COMPLETED, PENDING, CHANGED."""
        mock_email.return_value = {"status": "mocked"}
        mock_cal.return_value = []
        mock_reminders.return_value = []

        # Meeting 1
        analysis1 = MeetingExtraction(
            summary="Meeting 1 kickoff.",
            decisions=[],
            action_items=[
                ActionItem(assignee="Kashif", task="Implement JWT auth tokens", deadline="2026-11-20"),
                ActionItem(assignee="Ali", task="Write API documentation", deadline="2026-11-25"),
                ActionItem(assignee="Priya", task="Set up CI pipeline", deadline="2026-11-15")
            ],
            jira_tasks=[],
            github_issues=[]
        )
        t1 = "Meeting 1: Kashif implement JWT auth tokens. Ali write API documentation. Priya set up CI pipeline."
        state1 = {"transcript": t1, "analysis": analysis1, "project_key": "PROJB", "ticket_result": [], "email_notifications": []}
        out1 = graph.invoke(state1)
        self.assertEqual(out1["sprint_comparison"]["summary_counts"]["newly_assigned"], 3)

        # Meeting 2: Kashif finished JWT auth; Ali changed deadline; Priya still pending (omitted from meeting 2 discussion)
        analysis2 = MeetingExtraction(
            summary="Meeting 2 status update.",
            decisions=[],
            action_items=[
                ActionItem(assignee="Kashif", task="Implement JWT auth tokens", deadline=None, progress="The JWT auth tokens task is completed and deployed."),
                ActionItem(assignee="Ali", task="Write API documentation", deadline="2026-11-30")
            ],
            jira_tasks=[],
            github_issues=[]
        )
        t2 = "Meeting 2: Kashif completed JWT auth tokens. Ali had to extend deadline for write API documentation to Nov 30."
        state2 = {"transcript": t2, "analysis": analysis2, "project_key": "PROJB", "ticket_result": [], "email_notifications": []}
        out2 = graph.invoke(state2)

        sprint_comp = out2.get("sprint_comparison")
        self.assertIsNotNone(sprint_comp)
        self.assertIsNotNone(sprint_comp.get("previous_meeting_id"))
        self.assertEqual(sprint_comp["summary_counts"]["completed"], 1)
        self.assertEqual(sprint_comp["summary_counts"]["changed"], 1)
        # Priya was omitted from Meeting 2, so by Absence Guard she remains PENDING
        self.assertEqual(sprint_comp["summary_counts"]["pending"], 1)

    @patch("graph.send_action_item_notification")
    @patch("graph.create_calendar_events_for_action_items")
    @patch("reminder_service.schedule_reminders")
    def test_project_key_and_action_items_propagation(self, mock_reminders, mock_cal, mock_email):
        """3. Action items and project key reach comparison engine cleanly."""
        mock_email.return_value = {"status": "mocked"}
        mock_cal.return_value = []
        mock_reminders.return_value = []

        custom_project = "CUSTOMPROJ"
        analysis = MeetingExtraction(
            summary="Custom project standup.",
            decisions=[],
            action_items=[
                ActionItem(assignee="DevA", task="Optimize query performance", deadline="2026-12-01")
            ],
            jira_tasks=[],
            github_issues=[]
        )
        transcript = "Standup: DevA will optimize query performance by Dec 1."
        state = {
            "transcript": transcript,
            "analysis": analysis,
            "project_key": custom_project,
            "ticket_result": [],
            "email_notifications": []
        }

        output = graph.invoke(state)
        sprint_comp = output.get("sprint_comparison")
        self.assertIsNotNone(sprint_comp)
        self.assertEqual(sprint_comp.get("project_key"), custom_project)
        self.assertEqual(len(sprint_comp.get("newly_assigned_tasks", [])), 1)
        self.assertEqual(sprint_comp["newly_assigned_tasks"][0]["task_name"], "Optimize query performance")

    @patch("graph.create_jira_issue_api")
    @patch("graph.send_action_item_notification")
    @patch("graph.create_calendar_events_for_action_items")
    @patch("reminder_service.schedule_reminders")
    def test_jira_issue_keys_preserved_in_comparison(self, mock_reminders, mock_cal, mock_email, mock_jira_api):
        """4. Jira issue keys created in graph are attached and preserved in comparison."""
        mock_email.return_value = {"status": "mocked"}
        mock_cal.return_value = []
        mock_reminders.return_value = []

        # Mock Jira creation
        mock_jira_api.return_value = {
            "mode": "real",
            "status": "SUCCESS",
            "http_status": 201,
            "issue_key": "SCRUM-999",
            "formatted_text": (
                "REAL JIRA ISSUE\n"
                "Issue Key: SCRUM-999\n"
                "Issue URL: https://testdomain.atlassian.net/browse/SCRUM-999\n"
                "HTTP Status: 201\n"
                "Title: Refactor payment gateway\n"
                "Status: Created successfully"
            )
        }

        analysis = MeetingExtraction(
            summary="Payment gateway meeting.",
            decisions=[],
            action_items=[
                ActionItem(assignee="Kashif", task="Refactor payment gateway", deadline="2026-11-28")
            ],
            jira_tasks=[
                {"title": "Refactor payment gateway", "description": "Refactor payment module"}
            ],
            github_issues=[]
        )

        transcript = "We must refactor payment gateway. Let's create a Jira ticket."
        state = {
            "transcript": transcript,
            "analysis": analysis,
            "project_key": "SCRUM",
            "ticket_result": [],
            "email_notifications": []
        }

        output = graph.invoke(state)
        sprint_comp = output.get("sprint_comparison")
        self.assertIsNotNone(sprint_comp)
        new_tasks = sprint_comp.get("newly_assigned_tasks", [])
        self.assertEqual(len(new_tasks), 1)
        self.assertEqual(new_tasks[0].get("jira_issue_key"), "SCRUM-999")

    @patch("graph.send_action_item_notification")
    @patch("graph.create_calendar_events_for_action_items")
    @patch("reminder_service.schedule_reminders")
    def test_comparison_result_appears_in_graph_output(self, mock_reminders, mock_cal, mock_email):
        """5. Comparison result appears in graph output state with structured schema."""
        mock_email.return_value = {"status": "mocked"}
        mock_cal.return_value = []
        mock_reminders.return_value = []

        analysis = MeetingExtraction(
            summary="Weekly sync.",
            decisions=["Proceed with plan"],
            action_items=[
                ActionItem(assignee="Priya", task="Audit security permissions", deadline="2026-11-19")
            ],
            jira_tasks=[],
            github_issues=[]
        )
        transcript = "Weekly sync: Priya will audit security permissions by Nov 19."
        state = {
            "transcript": transcript,
            "analysis": analysis,
            "ticket_result": [],
            "email_notifications": []
        }

        output = graph.invoke(state)
        self.assertIn("sprint_comparison", output)
        res = output["sprint_comparison"]
        self.assertIn("summary_counts", res)
        self.assertIn("current_meeting_id", res)
        self.assertIn("newly_assigned_tasks", res)
        self.assertIn("completed_tasks", res)
        self.assertIn("pending_tasks", res)
        self.assertIn("in_progress_tasks", res)
        self.assertIn("changed_tasks", res)
        self.assertIn("needs_review_tasks", res)

    @patch("graph.send_action_item_notification")
    @patch("graph.create_calendar_events_for_action_items")
    @patch("reminder_service.schedule_reminders")
    def test_repeated_transcript_does_not_duplicate_records(self, mock_reminders, mock_cal, mock_email):
        """6. Repeated transcript does not duplicate meeting or task records."""
        mock_email.return_value = {"status": "mocked"}
        mock_cal.return_value = []
        mock_reminders.return_value = []

        analysis = MeetingExtraction(
            summary="Duplicate check test.",
            decisions=[],
            action_items=[
                ActionItem(assignee="Ali", task="Configure Redis cache", deadline="2026-11-20")
            ],
            jira_tasks=[],
            github_issues=[]
        )
        transcript = "Ali will configure Redis cache by Nov 20."
        state = {
            "transcript": transcript,
            "analysis": analysis,
            "project_key": "DEDUP",
            "ticket_result": [],
            "email_notifications": []
        }

        # Run first time
        out1 = graph.invoke(state)
        # Run second time with identical input
        out2 = graph.invoke(state)

        # Check DB records
        tasks = get_project_tasks(project_key="DEDUP", db_path=self.test_db)
        self.assertEqual(len(tasks), 1, "There should be exactly 1 task record, no duplicates.")

        conn = get_sprint_connection(self.test_db)
        try:
            meetings = conn.execute("SELECT * FROM meetings WHERE project_key = 'DEDUP'").fetchall()
            self.assertEqual(len(meetings), 1, "There should be exactly 1 meeting record, no duplicates.")
        finally:
            conn.close()

    @patch("graph.compare_sprint_meetings")
    @patch("graph.send_action_item_notification")
    @patch("graph.create_calendar_events_for_action_items")
    @patch("reminder_service.schedule_reminders")
    def test_failure_isolation(self, mock_reminders, mock_cal, mock_email, mock_compare):
        """7. Failure isolation: engine exception does not block graph execution or downstream nodes."""
        mock_email.return_value = {"status": "mocked"}
        mock_cal.return_value = []
        mock_reminders.return_value = []
        mock_compare.side_effect = RuntimeError("Simulated Sprint Engine crash")

        analysis = MeetingExtraction(
            summary="Crash safety test.",
            decisions=[],
            action_items=[
                ActionItem(assignee="Kashif", task="Run database migrations", deadline="2026-11-22")
            ],
            jira_tasks=[],
            github_issues=[]
        )
        transcript = "Kashif will run database migrations."
        state = {
            "transcript": transcript,
            "analysis": analysis,
            "project_key": "CRASHTEST",
            "ticket_result": [],
            "email_notifications": []
        }

        # Invoking graph should NOT raise exception
        output = graph.invoke(state)

        # sprint_comparison captured failure safely
        sprint_comp = output.get("sprint_comparison")
        self.assertIsNotNone(sprint_comp)
        self.assertEqual(sprint_comp.get("status"), "FAILED")
        self.assertIn("Simulated Sprint Engine crash", sprint_comp.get("error", ""))

        # Downstream action (reminders) still executed
        self.assertTrue(mock_reminders.called)

    @patch("graph.get_llm")
    @patch("jira_service.requests.post")
    def test_user_query_bypasses_sprint_comparison(self, mock_jira_post, mock_get_llm):
        """8. User query routing (CREATE_TICKET_JIRA) terminates without running sprint comparison."""
        from graph import RouterOutput
        mock_llm = MagicMock()
        mock_structured = MagicMock()
        mock_structured.invoke.return_value = RouterOutput(
            route="CREATE_TICKET_JIRA",
            ticket_title="Manual Jira Request",
            ticket_description="Manual Jira Description"
        )
        mock_llm.with_structured_output.return_value = mock_structured
        mock_get_llm.return_value = mock_llm

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"key": "SCRUM-777"}
        mock_jira_post.return_value = mock_resp

        inputs = {
            "user_query": "Create a Jira ticket for Manual Jira Request.",
            "analysis": None,
            "transcript": None,
            "ticket_result": [],
            "email_notifications": []
        }

        output = graph.invoke(inputs)
        # sprint_comparison is not executed
        self.assertIsNone(output.get("sprint_comparison"))
        # Jira ticket result is present
        ticket_res = output.get("ticket_result", [])
        self.assertTrue(len(ticket_res) > 0)
        self.assertIn("SCRUM-777", ticket_res[0])


if __name__ == "__main__":
    unittest.main()
