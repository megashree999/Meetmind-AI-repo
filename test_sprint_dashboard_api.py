import os
import unittest
import tempfile
import sqlite3
import json
import urllib.parse
from unittest.mock import patch, MagicMock

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Match

from sprint_database import (
    init_sprint_db,
    save_meeting,
    save_or_update_task,
    save_comparison_run,
    get_latest_comparison_run
)
from sprint_api import sanitize_identifier, enrich_task_with_jira_url
from jira_service import get_safe_jira_issue_url
from webhook_server import create_app

class ApiResponseWrapper:
    def __init__(self, response):
        self.status_code = response.status_code
        self.headers = getattr(response, "headers", {})
        if hasattr(response, "path") and os.path.isfile(response.path):
            with open(response.path, "rb") as f:
                self.body = f.read()
        else:
            self.body = getattr(response, "body", b"")

    def json(self):
        return json.loads(self.body.decode("utf-8"))

class DirectApiClient:
    def __init__(self, app):
        self.app = app

    def get(self, url: str):
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        query_string = parsed.query.encode("ascii")

        scope = {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": query_string,
            "app": self.app
        }

        for route in self.app.routes:
            match, child_scope = route.matches(scope)
            if match == Match.FULL:
                merged_scope = dict(scope)
                merged_scope.update(child_scope)
                req = Request(merged_scope)
                if hasattr(route, "endpoint"):
                    resp = route.endpoint(req)
                    return ApiResponseWrapper(resp)

        return ApiResponseWrapper(JSONResponse(
            {"status": "ERROR", "message": f"API endpoint not found: {path}"},
            status_code=404
        ))

class TestSprintDashboardAPI(unittest.TestCase):
    def setUp(self):
        import gc
        gc.collect()
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.test_sprint_db = os.path.join(self.temp_dir.name, "test_sprint.db")
        self.test_reminders_db = os.path.join(self.temp_dir.name, "test_reminders.db")
        init_sprint_db(self.test_sprint_db)

        # Create dummy reminders.db
        with sqlite3.connect(self.test_reminders_db) as conn:
            conn.execute('''
                CREATE TABLE reminders (
                    id INTEGER PRIMARY KEY,
                    task_name TEXT,
                    recipient_email TEXT,
                    status TEXT
                )
            ''')
            conn.execute("INSERT INTO reminders VALUES (1, 'Test reminder', 'test@example.com', 'PENDING')")
            conn.commit()

        self.env_patcher = patch.dict(os.environ, {
            "SPRINT_COMPARISON_DB": self.test_sprint_db,
            "JIRA_BASE_URL": "https://testworkspace.atlassian.net",
            "JIRA_EMAIL": "user@example.com",
            "JIRA_API_TOKEN": "secret_token_12345",
            "JIRA_PROJECT_KEY": "SCRUM"
        })
        self.env_patcher.start()

        self.jira_sync_patcher = patch(
            "sprint_comparison_engine.get_jira_issues_for_project",
            return_value={"status": "SUCCESS", "issues": []}
        )
        self.jira_sync_patcher.start()

        self.app = create_app(
            db_path=self.test_reminders_db,
            sprint_db_path=self.test_sprint_db
        )
        self.client = DirectApiClient(self.app)

    def tearDown(self):
        import gc
        self.jira_sync_patcher.stop()
        self.env_patcher.stop()
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_list_projects_empty_and_populated(self):
        """Test GET /api/projects returns default SCRUM when empty and actual projects when populated."""
        resp = self.client.get("/api/projects")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "SUCCESS")
        self.assertIn("SCRUM", data["projects"])

        # Add a meeting in another project
        save_meeting(
            meeting_id="m_alpha_1",
            project_key="ALPHA",
            meeting_title="Alpha Kickoff",
            transcript_hash="hash_alpha",
            db_path=self.test_sprint_db
        )
        resp2 = self.client.get("/api/projects")
        self.assertEqual(resp2.status_code, 200)
        self.assertIn("ALPHA", resp2.json()["projects"])

    def test_list_meetings_sequence_order(self):
        """Test GET /api/meetings returns meetings chronologically ordered by sequence number."""
        save_meeting(
            meeting_id="m_seq_1",
            project_key="SCRUM",
            meeting_title="Sprint 1 Kickoff",
            transcript_hash="hash_seq_1",
            summary="Meeting 1 summary",
            db_path=self.test_sprint_db
        )
        save_meeting(
            meeting_id="m_seq_2",
            project_key="SCRUM",
            meeting_title="Sprint 2 Planning",
            transcript_hash="hash_seq_2",
            summary="Meeting 2 summary",
            db_path=self.test_sprint_db
        )

        resp = self.client.get("/api/meetings?project_key=SCRUM")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["count"], 2)
        # Sequence 1 must precede Sequence 2
        self.assertEqual(data["meetings"][0]["meeting_id"], "m_seq_1")
        self.assertEqual(data["meetings"][1]["meeting_id"], "m_seq_2")

    def test_latest_comparison_empty_and_success(self):
        """Test GET /api/comparisons/latest handles empty state and persistent comparison retrieval."""
        # Empty state
        resp = self.client.get("/api/comparisons/latest?project_key=SCRUM")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["has_comparisons"])
        self.assertIsNone(data["comparison"])

        # Record a successful comparison run
        comp_data = {
            "status": "SUCCESS",
            "project_key": "SCRUM",
            "current_meeting_id": "m_curr",
            "previous_meeting_id": "m_prev",
            "summary_counts": {"newly_assigned": 1, "completed": 1, "total_active_tasks": 2},
            "newly_assigned_tasks": [{
                "task_id": "t_new_1",
                "task_name": "Setup OAuth2",
                "assignee": "Alice",
                "jira_issue_key": "SCRUM-101",
                "status": "PENDING"
            }],
            "completed_tasks": [{
                "task_id": "t_done_1",
                "task_name": "Fix DB Migration",
                "assignee": "Bob",
                "jira_issue_key": "SCRUM-102",
                "status": "COMPLETED"
            }],
            "pending_tasks": [],
            "in_progress_tasks": [],
            "changed_tasks": [],
            "needs_review_tasks": []
        }
        save_comparison_run(
            project_key="SCRUM",
            current_meeting_id="m_curr",
            previous_meeting_id="m_prev",
            status="SUCCESS",
            summary_counts=comp_data["summary_counts"],
            comparison_data=comp_data,
            db_path=self.test_sprint_db
        )

        resp2 = self.client.get("/api/comparisons/latest?project_key=SCRUM")
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        self.assertTrue(data2["has_comparisons"])
        self.assertEqual(data2["current_meeting_id"], "m_curr")
        # Validate that Jira links were enriched safely
        new_task = data2["comparison"]["newly_assigned_tasks"][0]
        self.assertEqual(new_task["jira_url"], "https://testworkspace.atlassian.net/browse/SCRUM-101")
        # Ensure credentials/tokens are never present in response
        self.assertNotIn("secret_token_12345", json.dumps(data2))

    def test_failed_comparison_honestly_reported(self):
        """Test requirement: Never show an old comparison as latest when the newest comparison failed."""
        # 1. Save an initial successful comparison
        save_comparison_run(
            project_key="SCRUM",
            current_meeting_id="m_good",
            previous_meeting_id=None,
            status="SUCCESS",
            summary_counts={"total_active_tasks": 1},
            comparison_data={"status": "SUCCESS", "current_meeting_id": "m_good"},
            db_path=self.test_sprint_db
        )

        # 2. Save a subsequent FAILED comparison run (e.g. LLM timeout or parsing crash)
        save_comparison_run(
            project_key="SCRUM",
            current_meeting_id="m_bad",
            previous_meeting_id="m_good",
            status="FAILED",
            summary_counts={},
            comparison_data={},
            error_message="Model inference timeout after 60s",
            db_path=self.test_sprint_db
        )

        # 3. Query latest comparison: MUST report FAILED, not mask with m_good!
        resp = self.client.get("/api/comparisons/latest?project_key=SCRUM")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "FAILED")
        self.assertEqual(data["current_meeting_id"], "m_bad")
        self.assertIn("timeout", data["error"])
        self.assertIsNone(data["comparison"])

    def test_historical_comparison_snapshot_durability(self):
        """Test requirement: Historical comparison remains accurate even after subsequent meetings alter task status."""
        # Meeting 1: Task A is introduced as PENDING
        task_a = save_or_update_task(
            project_key="SCRUM",
            task_name="Deploy Microservice",
            meeting_id="m1",
            status="PENDING",
            db_path=self.test_sprint_db
        )["task"]
        t_id = task_a["task_id"]

        # Run comparison between m0 and m1: Task A is NEWLY_ASSIGNED
        run1_data = {
            "status": "SUCCESS",
            "project_key": "SCRUM",
            "current_meeting_id": "m1",
            "previous_meeting_id": None,
            "newly_assigned_tasks": [{"task_id": t_id, "task_name": "Deploy Microservice", "status": "PENDING"}],
            "completed_tasks": [],
            "pending_tasks": [],
            "in_progress_tasks": [],
            "changed_tasks": [],
            "needs_review_tasks": []
        }
        save_comparison_run(
            project_key="SCRUM",
            current_meeting_id="m1",
            previous_meeting_id=None,
            status="SUCCESS",
            summary_counts={"newly_assigned": 1},
            comparison_data=run1_data,
            db_path=self.test_sprint_db
        )

        # Later, Meeting 2 occurs and Task A is COMPLETED in the database
        save_or_update_task(
            project_key="SCRUM",
            task_name="Deploy Microservice",
            meeting_id="m2",
            task_id=t_id,
            status="COMPLETED",
            completion_evidence="Verified deployed by Bob",
            db_path=self.test_sprint_db
        )

        # Verify current task is COMPLETED
        current_task = self.client.get(f"/api/tasks/{t_id}").json()["task"]
        self.assertEqual(current_task["status"], "COMPLETED")

        # Now query historical comparison for m1: Task A must STILL be shown as PENDING / NEWLY_ASSIGNED!
        hist_resp = self.client.get("/api/comparisons?project_key=SCRUM&current_meeting_id=m1")
        self.assertEqual(hist_resp.status_code, 200)
        hist_data = hist_resp.json()
        hist_tasks = hist_data["comparison"]["newly_assigned_tasks"]
        self.assertEqual(len(hist_tasks), 1)
        self.assertEqual(hist_tasks[0]["status"], "PENDING")

    def test_task_details_and_history(self):
        """Test GET /api/tasks/{task_id} and GET /api/tasks/{task_id}/history."""
        task_res = save_or_update_task(
            project_key="SCRUM",
            task_name="Implement Auth",
            meeting_id="m_init",
            assignee="Alice",
            deadline="2026-11-20",
            jira_issue_key="SCRUM-77",
            status="PENDING",
            db_path=self.test_sprint_db
        )
        t_id = task_res["task"]["task_id"]

        # Update assignee and status
        save_or_update_task(
            project_key="SCRUM",
            task_name="Implement Auth",
            meeting_id="m_next",
            task_id=t_id,
            assignee="Bob",
            status="IN_PROGRESS",
            change_reason="Reassigned to Bob for faster delivery",
            db_path=self.test_sprint_db
        )

        # 1. Task Details
        det_resp = self.client.get(f"/api/tasks/{t_id}")
        self.assertEqual(det_resp.status_code, 200)
        task_obj = det_resp.json()["task"]
        self.assertEqual(task_obj["assignee"], "Bob")
        self.assertEqual(task_obj["status"], "IN_PROGRESS")
        self.assertEqual(task_obj["jira_url"], "https://testworkspace.atlassian.net/browse/SCRUM-77")

        # 2. Task History
        hist_resp = self.client.get(f"/api/tasks/{t_id}/history")
        self.assertEqual(hist_resp.status_code, 200)
        h_data = hist_resp.json()
        self.assertEqual(h_data["count"], 2)
        # Verify transition reason
        self.assertIn("Reassigned to Bob", h_data["history"][1]["change_reason"])

    def test_dashboard_summary_counts(self):
        """Test GET /api/summary returns correct aggregated counts."""
        save_or_update_task(project_key="SCRUM", task_name="Task 1", meeting_id="m1", status="PENDING", db_path=self.test_sprint_db)
        save_or_update_task(project_key="SCRUM", task_name="Task 2", meeting_id="m1", status="IN_PROGRESS", db_path=self.test_sprint_db)
        save_or_update_task(project_key="SCRUM", task_name="Task 3", meeting_id="m1", status="COMPLETED", db_path=self.test_sprint_db)

        resp = self.client.get("/api/summary?project_key=SCRUM")
        self.assertEqual(resp.status_code, 200)
        s = resp.json()["summary"]
        self.assertEqual(s["total_active_tasks"], 2)
        self.assertEqual(s["completed_tasks"], 1)
        self.assertEqual(s["pending_tasks"], 1)
        self.assertEqual(s["in_progress_tasks"], 1)

    def test_jira_url_security_validation(self):
        """Test requirement: Validate Jira URLs against configured domain; never build links from arbitrary inputs."""
        # Valid key
        valid_url = get_safe_jira_issue_url("SCRUM-123")
        self.assertEqual(valid_url, "https://testworkspace.atlassian.net/browse/SCRUM-123")

        # Malicious / arbitrary inputs
        self.assertIsNone(get_safe_jira_issue_url("javascript:alert(1)"))
        self.assertIsNone(get_safe_jira_issue_url("https://malicious-site.com/SCRUM-1"))
        self.assertIsNone(get_safe_jira_issue_url("SCRUM-123; DROP TABLE"))
        self.assertIsNone(get_safe_jira_issue_url("../../etc/passwd"))
        self.assertIsNone(get_safe_jira_issue_url(""))
        self.assertIsNone(get_safe_jira_issue_url(None))

    def test_api_404_does_not_hit_spa_fallback(self):
        """Test requirement: Register API routes before SPA fallback; broken /api requests must return JSON 404."""
        resp = self.client.get("/api/non_existent_endpoint")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.headers.get("content-type"), "application/json")
        data = resp.json()
        self.assertEqual(data["status"], "ERROR")
        self.assertIn("API endpoint not found", data["message"])

    def test_spa_serves_frontend_index(self):
        """Test that the root URL serves index.html from frontend/dist."""
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("MeetMind AI", resp.body.decode("utf-8"))

    def test_reminders_db_not_modified(self):
        """Test safety requirement: production reminders.db remains completely untouched."""
        # Call multiple endpoints
        self.client.get("/api/projects")
        self.client.get("/api/meetings?project_key=SCRUM")
        self.client.get("/api/comparisons/latest?project_key=SCRUM")
        self.client.get("/api/summary?project_key=SCRUM")

        with sqlite3.connect(self.test_reminders_db) as conn:
            cnt = conn.execute("SELECT count(*) FROM reminders").fetchone()[0]
            self.assertEqual(cnt, 1)

if __name__ == "__main__":
    unittest.main()
