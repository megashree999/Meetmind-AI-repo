import os
import sys
import unittest
import tempfile
import shutil
import threading
import time
from unittest.mock import patch, MagicMock

# Ensure current working directory is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from graph import graph, AgentState, MeetingExtraction, ActionItem
from sprint_database import (
    init_sprint_db,
    get_sprint_connection,
    get_project_tasks,
    get_task_history,
    claim_jira_creation,
    resolve_jira_claim,
    get_cached_jira_ticket,
    generate_jira_idempotency_key
)


class TestIdempotencyHardening(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.test_db = os.path.join(self.temp_dir, "test_hardening.db")
        self.orig_db_env = os.environ.get("SPRINT_COMPARISON_DB")
        os.environ["SPRINT_COMPARISON_DB"] = self.test_db
        init_sprint_db(self.test_db)

        self.env_patcher = patch.dict(os.environ, {
            "JIRA_BASE_URL": "https://testdomain.atlassian.net",
            "JIRA_EMAIL": "test@example.com",
            "JIRA_API_TOKEN": "test_token",
            "JIRA_PROJECT_KEY": "HARDEN",
            "EMAIL_NOTIFICATIONS_ENABLED": "False"
        })
        self.env_patcher.start()

        # By default patch Jira sync to return empty issues
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
    def test_two_simultaneous_identical_requests(self, mock_jira_api, mock_reminders, mock_cal, mock_email):
        """1. Concurrency: Two simultaneous identical requests cannot both call Jira API."""
        mock_email.return_value = {"status": "mocked"}
        mock_cal.return_value = []
        mock_reminders.return_value = []

        api_call_count = [0]
        call_lock = threading.Lock()

        def slow_fake_jira_create(title, description, project_key=None):
            with call_lock:
                api_call_count[0] += 1
            # Simulate network latency of 0.3s
            time.sleep(0.3)
            return {
                "mode": "real",
                "status": "SUCCESS",
                "http_status": 201,
                "issue_key": "HARDEN-501",
                "formatted_text": f"REAL JIRA ISSUE\nIssue Key: HARDEN-501\nTitle: {title}\nStatus: Created successfully"
            }
        mock_jira_api.side_effect = slow_fake_jira_create

        analysis = MeetingExtraction(
            summary="Concurrent creation test.",
            decisions=[],
            action_items=[ActionItem(assignee="Kashif", task="Build concurrent pipeline", deadline="2026-11-20")],
            jira_tasks=[{"title": "Build concurrent pipeline", "description": "Details"}],
            github_issues=[]
        )
        transcript = "Kashif will build concurrent pipeline."
        state = {
            "transcript": transcript,
            "analysis": analysis,
            "project_key": "HARDEN",
            "ticket_result": [],
            "email_notifications": []
        }

        results = [None, None]
        errors = [None, None]

        def worker(idx):
            try:
                results[idx] = graph.invoke(dict(state))
            except Exception as e:
                errors[idx] = e

        t1 = threading.Thread(target=worker, args=(0,))
        t2 = threading.Thread(target=worker, args=(1,))

        t1.start()
        # Small stagger of 20ms to simulate near-simultaneous arrival
        time.sleep(0.02)
        t2.start()

        t1.join()
        t2.join()

        self.assertIsNone(errors[0])
        self.assertIsNone(errors[1])

        # HARDENING CHECK: Only ONE worker should have called the Jira API!
        self.assertEqual(api_call_count[0], 1, "Only 1 worker must call Jira API during concurrent arrival.")

        # One worker got the created issue, the other got either REUSED or IN_PROGRESS guard
        ticket_strs = []
        for r in results:
            if r and r.get("ticket_result"):
                ticket_strs.extend(r["ticket_result"])

        self.assertTrue(any("HARDEN-501" in s or "IN_PROGRESS" in s for s in ticket_strs))

    @patch("graph.send_action_item_notification")
    @patch("graph.create_calendar_events_for_action_items")
    @patch("reminder_service.schedule_reminders")
    @patch("graph.create_jira_issue_api")
    def test_same_task_retried_after_successful_creation(self, mock_jira_api, mock_reminders, mock_cal, mock_email):
        """2. Same task retried after successful Jira creation reuses issue without calling API again."""
        mock_email.return_value = {"status": "mocked"}
        mock_cal.return_value = []
        mock_reminders.return_value = []

        mock_jira_api.return_value = {
            "mode": "real",
            "status": "SUCCESS",
            "http_status": 201,
            "issue_key": "HARDEN-701",
            "formatted_text": "REAL JIRA ISSUE\nIssue Key: HARDEN-701\nTitle: Cache Layer\nStatus: Created successfully"
        }

        analysis = MeetingExtraction(
            summary="Retry test.",
            decisions=[],
            action_items=[ActionItem(assignee="Ali", task="Cache Layer", deadline="2026-11-22")],
            jira_tasks=[{"title": "Cache Layer", "description": "Redis cache"}],
            github_issues=[]
        )
        transcript = "Ali will implement Cache Layer."
        state = {"transcript": transcript, "analysis": analysis, "project_key": "HARDEN", "ticket_result": [], "email_notifications": []}

        out1 = graph.invoke(state)
        self.assertEqual(mock_jira_api.call_count, 1)

        out2 = graph.invoke(state)
        # Idempotency check: call count remains 1
        self.assertEqual(mock_jira_api.call_count, 1)
        self.assertIn("HARDEN-701", out2["ticket_result"][0])

    @patch("graph.send_action_item_notification")
    @patch("graph.create_calendar_events_for_action_items")
    @patch("reminder_service.schedule_reminders")
    @patch("graph.create_jira_issue_api")
    def test_jira_timeout_before_creation(self, mock_jira_api, mock_reminders, mock_cal, mock_email):
        """3. Jira timeout before creation: fails safely and can be retried cleanly."""
        mock_email.return_value = {"status": "mocked"}
        mock_cal.return_value = []
        mock_reminders.return_value = []

        # Attempt 1: Connection timeout
        mock_jira_api.return_value = {
            "mode": "real",
            "status": "FAILED",
            "http_status": None,
            "error": "HTTPSConnectionPool ConnectTimeout connecting to Jira",
            "formatted_text": "FAILED JIRA ISSUE (API ERROR)\nTitle: TLS Setup\nError: ConnectTimeout\nStatus: FAILED"
        }

        analysis = MeetingExtraction(
            summary="Timeout test.",
            decisions=[],
            action_items=[ActionItem(assignee="Kashif", task="TLS Setup", deadline="2026-11-25")],
            jira_tasks=[{"title": "TLS Setup", "description": "Configure TLS"}],
            github_issues=[]
        )
        t = "Kashif configure TLS Setup."
        state = {"transcript": t, "analysis": analysis, "project_key": "HARDEN", "ticket_result": [], "email_notifications": []}

        out1 = graph.invoke(state)
        self.assertEqual(mock_jira_api.call_count, 1)
        self.assertIn("FAILED", out1["ticket_result"][0])

        # Attempt 2: Jira recovers
        mock_jira_api.return_value = {
            "mode": "real",
            "status": "SUCCESS",
            "http_status": 201,
            "issue_key": "HARDEN-801",
            "formatted_text": "REAL JIRA ISSUE\nIssue Key: HARDEN-801\nTitle: TLS Setup\nStatus: Created successfully"
        }
        out2 = graph.invoke(state)
        self.assertEqual(mock_jira_api.call_count, 2)
        self.assertIn("HARDEN-801", out2["ticket_result"][0])

    @patch("graph.send_action_item_notification")
    @patch("graph.create_calendar_events_for_action_items")
    @patch("reminder_service.schedule_reminders")
    @patch("graph.create_jira_issue_api")
    def test_jira_read_timeout_uncertain_outcome(self, mock_jira_api, mock_reminders, mock_cal, mock_email):
        """4. Jira timeout after request sent (ReadTimeout): marks UNCERTAIN_TIMEOUT and does NOT blindly retry."""
        mock_email.return_value = {"status": "mocked"}
        mock_cal.return_value = []
        mock_reminders.return_value = []

        # Attempt 1: Read timeout after request was sent
        mock_jira_api.return_value = {
            "mode": "real",
            "status": "FAILED",
            "http_status": None,
            "error": "HTTPSConnectionPool Read timed out after 20s",
            "formatted_text": "FAILED JIRA ISSUE (API ERROR)\nTitle: Migration\nError: Read timed out\nStatus: FAILED"
        }

        analysis = MeetingExtraction(
            summary="Read timeout test.",
            decisions=[],
            action_items=[ActionItem(assignee="Ali", task="Run database migration", deadline="2026-11-29")],
            jira_tasks=[{"title": "Run database migration", "description": "Migration"}],
            github_issues=[]
        )
        t = "Ali will run database migration."
        state = {"transcript": t, "analysis": analysis, "project_key": "HARDEN", "ticket_result": [], "email_notifications": []}

        out1 = graph.invoke(state)
        self.assertEqual(mock_jira_api.call_count, 1)
        self.assertIn("FAILED", out1["ticket_result"][0])

        # Attempt 2: Immediate blind retry should be BLOCKED because prior outcome was uncertain!
        out2 = graph.invoke(state)
        # Verify Jira API was NOT called a second time blindly!
        self.assertEqual(mock_jira_api.call_count, 1, "Must NOT blindly retry when previous request timed out reading response.")
        self.assertIn("UNCERTAIN TIMEOUT", out2["ticket_result"][0])

    @patch("graph.send_action_item_notification")
    @patch("graph.create_calendar_events_for_action_items")
    @patch("reminder_service.schedule_reminders")
    @patch("graph.create_jira_issue_api")
    def test_failed_jira_request_allows_clean_retry(self, mock_jira_api, mock_reminders, mock_cal, mock_email):
        """5. Confirmed failed Jira request (HTTP 401/500) allows clean retry."""
        mock_email.return_value = {"status": "mocked"}
        mock_cal.return_value = []
        mock_reminders.return_value = []

        # Attempt 1: Confirmed HTTP 401
        mock_jira_api.return_value = {
            "mode": "real",
            "status": "FAILED",
            "http_status": 401,
            "error": "HTTP 401 Unauthorized",
            "formatted_text": "FAILED JIRA ISSUE (API ERROR)\nTitle: Bugfix\nHTTP Status: 401\nStatus: FAILED"
        }

        analysis = MeetingExtraction(
            summary="Auth fail test.",
            decisions=[],
            action_items=[ActionItem(assignee="Kashif", task="Fix auth bug", deadline="2026-11-20")],
            jira_tasks=[{"title": "Fix auth bug", "description": "Fix bug"}],
            github_issues=[]
        )
        t = "Fix auth bug by Nov 20."
        state = {"transcript": t, "analysis": analysis, "project_key": "HARDEN", "ticket_result": [], "email_notifications": []}

        out1 = graph.invoke(state)
        self.assertEqual(mock_jira_api.call_count, 1)

        # Attempt 2: After fixing token, retry succeeds
        mock_jira_api.return_value = {
            "mode": "real",
            "status": "SUCCESS",
            "http_status": 201,
            "issue_key": "HARDEN-901",
            "formatted_text": "REAL JIRA ISSUE\nIssue Key: HARDEN-901\nTitle: Fix auth bug\nStatus: Created successfully"
        }
        out2 = graph.invoke(state)
        self.assertEqual(mock_jira_api.call_count, 2, "Confirmed failure must allow retry.")
        self.assertIn("HARDEN-901", out2["ticket_result"][0])

    @patch("graph.send_action_item_notification")
    @patch("graph.create_calendar_events_for_action_items")
    @patch("reminder_service.schedule_reminders")
    @patch("graph.create_jira_issue_api")
    def test_two_distinct_tasks_with_identical_titles(self, mock_jira_api, mock_reminders, mock_cal, mock_email):
        """6. False reuse guard: two distinct tasks with identical titles assigned to different people get distinct issues."""
        mock_email.return_value = {"status": "mocked"}
        mock_cal.return_value = []
        mock_reminders.return_value = []

        counter = [0]
        def fake_create(title, description, project_key=None):
            counter[0] += 1
            key = f"HARDEN-100{counter[0]}"
            return {
                "mode": "real",
                "status": "SUCCESS",
                "http_status": 201,
                "issue_key": key,
                "formatted_text": f"REAL JIRA ISSUE\nIssue Key: {key}\nTitle: {title}\nStatus: Created successfully"
            }
        mock_jira_api.side_effect = fake_create

        # Task 1 assigned to Alice in Meeting 1
        analysis1 = MeetingExtraction(
            summary="Meeting 1.",
            decisions=[],
            action_items=[ActionItem(assignee="Alice", task="Write integration tests", deadline="2026-11-10")],
            jira_tasks=[{"title": "Write integration tests", "description": "Alice's test suite"}],
            github_issues=[]
        )
        t1 = "Meeting 1: Alice will write integration tests by Nov 10."
        out1 = graph.invoke({"transcript": t1, "analysis": analysis1, "project_key": "HARDEN", "ticket_result": [], "email_notifications": []})
        self.assertEqual(mock_jira_api.call_count, 1)
        self.assertIn("HARDEN-1001", out1["ticket_result"][0])

        # Task 2 assigned to Bob in Meeting 2 (identical title, different person and meeting)
        analysis2 = MeetingExtraction(
            summary="Meeting 2.",
            decisions=[],
            action_items=[ActionItem(assignee="Bob", task="Write integration tests", deadline="2026-11-20")],
            jira_tasks=[{"title": "Write integration tests", "description": "Bob's test suite"}],
            github_issues=[]
        )
        t2 = "Meeting 2: Bob will write integration tests by Nov 20."
        out2 = graph.invoke({"transcript": t2, "analysis": analysis2, "project_key": "HARDEN", "ticket_result": [], "email_notifications": []})

        # FALSE REUSE GUARD: Bob's task must NOT silently hijack Alice's Jira ticket!
        self.assertEqual(mock_jira_api.call_count, 2, "Bob's task must receive its own distinct Jira issue.")
        self.assertIn("HARDEN-1002", out2["ticket_result"][0])

    @patch("graph.send_action_item_notification")
    @patch("graph.create_calendar_events_for_action_items")
    @patch("reminder_service.schedule_reminders")
    @patch("graph.create_jira_issue_api")
    def test_same_task_consecutive_meetings_reuses_jira(self, mock_jira_api, mock_reminders, mock_cal, mock_email):
        """7. Same task (same assignee, same task) in consecutive meetings reuses original Jira issue."""
        mock_email.return_value = {"status": "mocked"}
        mock_cal.return_value = []
        mock_reminders.return_value = []

        mock_jira_api.return_value = {
            "mode": "real",
            "status": "SUCCESS",
            "http_status": 201,
            "issue_key": "HARDEN-301",
            "formatted_text": "REAL JIRA ISSUE\nIssue Key: HARDEN-301\nTitle: Refactor Auth\nStatus: Created successfully"
        }

        # Meeting 1: Alice assigned Refactor Auth
        a1 = MeetingExtraction(
            summary="M1.",
            decisions=[],
            action_items=[ActionItem(assignee="Alice", task="Refactor Auth", deadline="2026-11-15")],
            jira_tasks=[{"title": "Refactor Auth", "description": "Refactor"}],
            github_issues=[]
        )
        t1 = "Alice: Refactor Auth by Nov 15."
        out1 = graph.invoke({"transcript": t1, "analysis": a1, "project_key": "HARDEN", "ticket_result": [], "email_notifications": []})
        self.assertEqual(mock_jira_api.call_count, 1)

        # Meeting 2: Alice is still working on Refactor Auth
        a2 = MeetingExtraction(
            summary="M2.",
            decisions=[],
            action_items=[ActionItem(assignee="Alice", task="Refactor Auth", deadline="2026-11-20", progress="In progress")],
            jira_tasks=[{"title": "Refactor Auth", "description": "Refactor"}],
            github_issues=[]
        )
        t2 = "Alice: Still working on Refactor Auth, extended to Nov 20."
        out2 = graph.invoke({"transcript": t2, "analysis": a2, "project_key": "HARDEN", "ticket_result": [], "email_notifications": []})

        self.assertEqual(mock_jira_api.call_count, 1, "Consecutive meeting for same task must reuse Jira issue.")
        self.assertIn("HARDEN-301", out2["ticket_result"][0])

    @patch("graph.send_action_item_notification")
    @patch("graph.create_calendar_events_for_action_items")
    @patch("reminder_service.schedule_reminders")
    @patch("graph.create_jira_issue_api")
    def test_two_projects_with_same_task_title(self, mock_jira_api, mock_reminders, mock_cal, mock_email):
        """8. Two different projects with tasks of the same title must receive distinct Jira tickets."""
        mock_email.return_value = {"status": "mocked"}
        mock_cal.return_value = []
        mock_reminders.return_value = []

        created_keys = []
        def fake_create(title, description, project_key=None):
            pkey = project_key or "DEF"
            key = f"{pkey}-1"
            created_keys.append(key)
            return {
                "mode": "real",
                "status": "SUCCESS",
                "http_status": 201,
                "issue_key": key,
                "formatted_text": f"REAL JIRA ISSUE\nIssue Key: {key}\nTitle: {title}\nStatus: Created successfully"
            }
        mock_jira_api.side_effect = fake_create

        analysis = MeetingExtraction(
            summary="Common task.",
            decisions=[],
            action_items=[ActionItem(assignee="Alice", task="Set up CI pipeline", deadline="2026-11-20")],
            jira_tasks=[{"title": "Set up CI pipeline", "description": "CI setup"}],
            github_issues=[]
        )
        t = "Set up CI pipeline by Nov 20."

        out_alpha = graph.invoke({"transcript": t, "analysis": analysis, "project_key": "PROJA", "ticket_result": [], "email_notifications": []})
        out_beta = graph.invoke({"transcript": t, "analysis": analysis, "project_key": "PROJB", "ticket_result": [], "email_notifications": []})

        self.assertEqual(mock_jira_api.call_count, 2, "Both projects must receive their own distinct Jira tickets.")
        self.assertIn("PROJA-1", out_alpha["ticket_result"][0])
        self.assertIn("PROJB-1", out_beta["ticket_result"][0])


if __name__ == "__main__":
    unittest.main()

