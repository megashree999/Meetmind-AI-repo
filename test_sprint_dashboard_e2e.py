import os
import unittest
import tempfile
import sqlite3
import json
import urllib.parse
from unittest.mock import patch, MagicMock

from sprint_database import (
    init_sprint_db,
    save_meeting,
    get_project_tasks,
    get_task_history,
    get_latest_comparison_run
)
from sprint_comparison_engine import compare_sprint_meetings
from webhook_server import create_app
from test_sprint_dashboard_api import DirectApiClient

class TestSprintDashboardE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import gc
        gc.collect()
        cls.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.test_sprint_db = os.path.join(cls.temp_dir.name, "e2e_sprint.db")
        cls.test_reminders_db = os.path.join(cls.temp_dir.name, "e2e_reminders.db")
        init_sprint_db(cls.test_sprint_db)

        # Seed dummy reminder in test_reminders_db
        with sqlite3.connect(cls.test_reminders_db) as conn:
            conn.execute('''
                CREATE TABLE reminders (
                    id INTEGER PRIMARY KEY,
                    task_name TEXT,
                    recipient_email TEXT,
                    status TEXT
                )
            ''')
            conn.execute("INSERT INTO reminders VALUES (1, 'Preserved reminder', 'dev@example.com', 'PENDING')")
            conn.commit()

        cls.env_patcher = patch.dict(os.environ, {
            "SPRINT_COMPARISON_DB": cls.test_sprint_db,
            "JIRA_BASE_URL": "https://company.atlassian.net",
            "JIRA_EMAIL": "test@company.com",
            "JIRA_API_TOKEN": "token_xyz",
            "JIRA_PROJECT_KEY": "SCRUM"
        })
        cls.env_patcher.start()

        cls.jira_mock = patch(
            "sprint_comparison_engine.get_jira_issues_for_project",
            return_value={"status": "SUCCESS", "issues": []}
        )
        cls.jira_mock.start()

        cls.app = create_app(
            db_path=cls.test_reminders_db,
            sprint_db_path=cls.test_sprint_db
        )
        cls.client = DirectApiClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        import gc
        cls.jira_mock.stop()
        cls.env_patcher.stop()
        gc.collect()
        try:
            cls.temp_dir.cleanup()
        except Exception:
            pass

    def test_01_server_serves_html_spa_dashboard(self):
        """Test GET / returns production dashboard HTML with MeetMind branding."""
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        body = resp.body.decode("utf-8")
        self.assertIn("MeetMind AI", body)

    def test_02_empty_state_before_any_meetings(self):
        """Test API correctly signals empty state when no meetings exist yet."""
        resp = self.client.get("/api/comparisons/latest?project_key=SCRUM")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "SUCCESS")
        self.assertFalse(data["has_comparisons"])
        self.assertIsNone(data["comparison"])

    def test_03_first_meeting_creates_baseline(self):
        """Meeting 1 (Sprint Planning) introduced: baseline created, tasks marked NEWLY_ASSIGNED."""
        m1_id = "m_sprint_101"
        save_meeting(
            meeting_id=m1_id,
            project_key="SCRUM",
            meeting_title="Sprint 101 Planning",
            transcript_hash="hash_101",
            summary="Kickoff meeting: assigned auth and db migration.",
            db_path=self.test_sprint_db
        )

        action_items = [
            {
                "task": "Build OAuth login",
                "assignee": "Alice",
                "deadline": "2026-11-15",
                "jira_issue_key": "SCRUM-101",
                "status": "PENDING"
            },
            {
                "task": "Setup Postgres schema",
                "assignee": "Bob",
                "deadline": "2026-11-12",
                "jira_issue_key": "SCRUM-102",
                "status": "PENDING"
            }
        ]

        comp1 = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id=m1_id,
            current_action_items=action_items,
            current_meeting_title="Sprint 101 Planning",
            db_path=self.test_sprint_db
        )
        self.assertEqual(comp1["status"], "SUCCESS")

        # Query live API
        resp = self.client.get("/api/comparisons/latest?project_key=SCRUM")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["has_comparisons"])
        comp = data["comparison"]
        self.assertEqual(len(comp["newly_assigned_tasks"]), 2)
        # Check safe Jira URL injection
        self.assertEqual(
            comp["newly_assigned_tasks"][0]["jira_url"],
            "https://company.atlassian.net/browse/SCRUM-101"
        )

    def test_04_second_meeting_detects_completion_and_changes(self):
        """Meeting 2: OAuth completed, Postgres deadline extended, new task added."""
        m2_id = "m_sprint_102"
        save_meeting(
            meeting_id=m2_id,
            project_key="SCRUM",
            meeting_title="Sprint 102 Mid-Sync",
            transcript_hash="hash_102",
            summary="Mid-sync: Alice finished OAuth; Bob extended deadline; Charlie joined.",
            db_path=self.test_sprint_db
        )

        action_items_m2 = [
            {
                "task": "Build OAuth login",
                "assignee": "Alice",
                "jira_issue_key": "SCRUM-101",
                "status": "COMPLETED",
                "completion_evidence": "Alice merged PR #42 and deployed to staging"
            },
            {
                "task": "Setup Postgres schema",
                "assignee": "Bob",
                "deadline": "2026-11-18", # Extended from 2026-11-12
                "jira_issue_key": "SCRUM-102",
                "status": "IN_PROGRESS"
            },
            {
                "task": "Implement Redis Cache",
                "assignee": "Charlie",
                "deadline": "2026-11-25",
                "jira_issue_key": "SCRUM-103",
                "status": "PENDING"
            }
        ]

        comp2 = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id=m2_id,
            current_action_items=action_items_m2,
            current_meeting_title="Sprint 102 Mid-Sync",
            transcript="Alice finished OAuth. Bob extended Postgres deadline to 18th. Charlie takes Redis.",
            db_path=self.test_sprint_db
        )
        self.assertEqual(comp2["status"], "SUCCESS")

        # Query live API
        resp = self.client.get("/api/comparisons/latest?project_key=SCRUM")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        comp = data["comparison"]

        # Verify classifications
        self.assertEqual(len(comp["completed_tasks"]), 1)
        self.assertEqual(comp["completed_tasks"][0]["task_name"], "Build OAuth login")
        self.assertEqual(len(comp["changed_tasks"]), 1)
        self.assertEqual(comp["changed_tasks"][0]["task_name"], "Setup Postgres schema")
        self.assertEqual(len(comp["newly_assigned_tasks"]), 1)
        self.assertEqual(comp["newly_assigned_tasks"][0]["task_name"], "Implement Redis Cache")

        # Verify summary counts
        sum_resp = self.client.get("/api/summary?project_key=SCRUM")
        s = sum_resp.json()["summary"]
        self.assertEqual(s["completed_tasks"], 1)

    def test_05_task_history_audit_trail(self):
        """Verify complete transition history is retrievable over API."""
        tasks = get_project_tasks("SCRUM", db_path=self.test_sprint_db)
        pg_task = next(t for t in tasks if "Postgres" in t["task_name"])
        t_id = pg_task["task_id"]

        resp = self.client.get(f"/api/tasks/{t_id}/history")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "SUCCESS")
        history = data["history"]
        self.assertGreaterEqual(len(history), 2)
        # Check that deadline extension was recorded
        self.assertTrue(any("deadline" in (h["change_reason"] or "").lower() for h in history))

    def test_06_api_security_and_nonexistent_endpoint(self):
        """Verify API 404 does not leak HTML or server stack trace."""
        resp = self.client.get("/api/unknown_route_xyz")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.headers.get("content-type"), "application/json")
        data = resp.json()
        self.assertEqual(data["status"], "ERROR")

    def test_07_reminders_db_unmodified(self):
        """Confirm production reminders table in test_reminders_db has not been modified."""
        with sqlite3.connect(self.test_reminders_db) as conn:
            cnt = conn.execute("SELECT count(*) FROM reminders").fetchone()[0]
            self.assertEqual(cnt, 1)

if __name__ == "__main__":
    unittest.main()
