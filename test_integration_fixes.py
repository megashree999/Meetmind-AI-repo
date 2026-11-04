import unittest
import datetime
import os
import tempfile
from unittest.mock import patch, MagicMock

import team_directory
from google_calendar_service import resolve_deterministic_deadline, parse_deadline, create_calendar_event
from sprint_database import (
    init_sprint_db,
    get_cached_github_issue,
    record_created_github_issue,
    get_dashboard_summary_counts
)
from graph import send_email_notifications, create_github_issue, create_jira_ticket, AgentState, ActionItem, GitHubIssue

class TestIntegrationFixes(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_sprint.db")
        init_sprint_db(self.db_path)
        os.environ["SPRINT_COMPARISON_DB"] = self.db_path

    def tearDown(self):
        if "SPRINT_COMPARISON_DB" in os.environ:
            del os.environ["SPRINT_COMPARISON_DB"]
        self.temp_dir.cleanup()

    # ==========================================
    # 1. EMAIL RESOLUTION & MEETING-SCOPED DEDUP
    # ==========================================
    def test_email_assignee_resolution(self):
        """Kashif and Ali must resolve to their correct emails from company directory."""
        kashif_email = team_directory.get_assignee_email("Kashif")
        ali_email = team_directory.get_assignee_email("Ali")
        self.assertEqual(kashif_email, "kashifahamad2003@gmail.com")
        self.assertEqual(ali_email, "amohammedali2005@gmail.com")

    @patch("graph.send_action_item_notification")
    def test_email_notifications_scoped_dedup_and_resolution(self, mock_send):
        """Email duplicates are scoped by meeting_id and use resolved email address, not N/A."""
        mock_send.return_value = {"status": "SENT", "message": "Notification sent successfully"}
        
        state: AgentState = {
            "meeting_id": "m_test_001",
            "analysis": {
                "action_items": [
                    ActionItem(task="Backend API", assignee="Kashif", deadline="2026-11-06"),
                    ActionItem(task="Database tables", assignee="Ali", deadline="2026-11-05")
                ],
                "summary": "Meeting 1"
            },
            "email_notifications": []
        }

        # First run: should send to both Kashif and Ali
        res1 = send_email_notifications(state)
        notifs1 = res1.get("email_notifications", [])
        self.assertEqual(len(notifs1), 2)
        for n in notifs1:
            self.assertEqual(n["status"], "SENT")
            if n["assignee"] == "Kashif":
                self.assertEqual(n["email"], "kashifahamad2003@gmail.com")
            elif n["assignee"] == "Ali":
                self.assertEqual(n["email"], "amohammedali2005@gmail.com")

        # Second run for SAME meeting: should skip duplicate WITH resolved email (not N/A)
        state["email_notifications"] = notifs1
        res2 = send_email_notifications(state)
        notifs2 = res2.get("email_notifications", [])
        self.assertEqual(len(notifs2), 2)
        for n in notifs2:
            self.assertEqual(n["status"], "skipped_duplicate")
            if n["assignee"] == "Kashif":
                self.assertEqual(n["email"], "kashifahamad2003@gmail.com")
            elif n["assignee"] == "Ali":
                self.assertEqual(n["email"], "amohammedali2005@gmail.com")

        # Third run for DIFFERENT meeting: must NOT be skipped as duplicate
        state["meeting_id"] = "m_test_002"
        res3 = send_email_notifications(state)
        notifs3 = res3.get("email_notifications", [])
        self.assertEqual(len(notifs3), 2)
        for n in notifs3:
            self.assertEqual(n["status"], "SENT")

    @patch("graph.send_action_item_notification")
    def test_email_missing_assignee_produces_failed(self, mock_send):
        """Missing email for a named assignee produces a clear FAILED status."""
        state: AgentState = {
            "meeting_id": "m_test_missing",
            "analysis": {
                "action_items": [
                    ActionItem(task="Fix unknown subsystem", assignee="NonexistentPerson", deadline="2026-11-10")
                ],
                "summary": "Meeting"
            },
            "email_notifications": []
        }
        res = send_email_notifications(state)
        notifs = res.get("email_notifications", [])
        self.assertEqual(len(notifs), 1)
        self.assertEqual(notifs[0]["status"], "FAILED")
        self.assertIn("not found in company directory", notifs[0]["message"])
        mock_send.assert_not_called()

    # ==========================================
    # 2. CALENDAR DETERMINISTIC RESOLUTION
    # ==========================================
    def test_calendar_weekday_resolution_from_wednesday(self):
        """Meeting date Wednesday Nov 4, 2026 resolves Thursday=Nov 5, Friday=Nov 6, Monday=Nov 9."""
        ref_wed = datetime.date(2026, 11, 4) # Wednesday

        # Kashif: Friday
        kashif_dl = resolve_deterministic_deadline("Finish the backend authentication API by Friday", ref_date=ref_wed)
        self.assertEqual(kashif_dl, "2026-11-06")

        # Ali: Thursday
        ali_dl = resolve_deterministic_deadline("Handle database tables and migration by Thursday", ref_date=ref_wed)
        self.assertEqual(ali_dl, "2026-11-05")

        # Megashree: Monday
        mega_dl = resolve_deterministic_deadline("Frontend login dashboard integration by Monday", ref_date=ref_wed)
        self.assertEqual(mega_dl, "2026-11-09")

        # No deadline
        doc_dl = resolve_deterministic_deadline("Ensure the authentication API is properly documented", ref_date=ref_wed)
        self.assertIsNone(doc_dl)

    def test_calendar_boundary_calculations(self):
        """Test weekday calculations across month boundaries and year boundaries."""
        # Month boundary: Friday Nov 27, 2026 -> Tuesday = Dec 1, 2026
        ref_nov = datetime.date(2026, 11, 27) # Friday
        tuesday_dl = resolve_deterministic_deadline("Deploy update by Tuesday", ref_date=ref_nov)
        self.assertEqual(tuesday_dl, "2026-12-01")

        # Year boundary: Wednesday Dec 30, 2026 -> Friday = Jan 1, 2027
        ref_dec = datetime.date(2026, 12, 30) # Wednesday
        friday_newyear = resolve_deterministic_deadline("Submit yearly audit by Friday", ref_date=ref_dec)
        self.assertEqual(friday_newyear, "2027-01-01")

    # ==========================================
    # 3. GITHUB ISSUE EXTRACTION & IDEMPOTENCY
    # ==========================================
    @patch("graph.create_github_issue_api")
    def test_github_issue_creation_and_idempotency(self, mock_gh_api):
        """GitHub issue is created once, recorded, and reused for the same meeting."""
        mock_gh_api.return_value = {
            "mode": "real",
            "status": "SUCCESS",
            "http_status": 201,
            "issue_number": 42,
            "issue_url": "https://github.com/megashree999/meetmind-ai/issues/42",
            "formatted_text": "REAL GITHUB ISSUE\nIssue Number: #42\nIssue URL: https://github.com/megashree999/meetmind-ai/issues/42\nHTTP Status: 201"
        }

        state: AgentState = {
            "meeting_id": "m_auth_jwt",
            "analysis": {
                "github_issues": [
                    {"title": "Testing the authentication API and database integration", "description": "Verify JWT login"}
                ]
            },
            "ticket_result": []
        }

        # 1. First execution creates the issue
        res1 = create_github_issue(state)
        results1 = res1.get("ticket_result", [])
        self.assertEqual(len(results1), 1)
        self.assertIn("#42", results1[0])
        self.assertEqual(mock_gh_api.call_count, 1)

        # Verify recorded in SQLite
        cached = get_cached_github_issue("m_auth_jwt", "Testing the authentication API and database integration", db_path=self.db_path)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["issue_number"], 42)

        # 2. Re-execution for same meeting reuses cached issue WITHOUT calling API again
        state["ticket_result"] = []
        res2 = create_github_issue(state)
        results2 = res2.get("ticket_result", [])
        self.assertEqual(len(results2), 1)
        self.assertIn("REUSED", results2[0])
        self.assertEqual(mock_gh_api.call_count, 1) # API was NOT called a second time

    # ==========================================
    # 4. JIRA ERROR HANDLING & RECONCILIATION
    # ==========================================
    @patch("graph.create_jira_issue_api")
    def test_jira_creation_rejects_missing_key(self, mock_jira_api):
        """Jira creation is marked FAILED if Jira API does not return a valid issue key."""
        mock_jira_api.return_value = {
            "mode": "real",
            "status": "FAILED",
            "http_status": 201,
            "issue_key": None,
            "error": "Missing key in response",
            "formatted_text": "FAILED JIRA ISSUE\nTitle: Auth Module\nError: Missing key"
        }

        state: AgentState = {
            "meeting_id": "m_test_jira",
            "project_key": "SCRUM",
            "analysis": {
                "jira_tasks": [{"title": "Auth Module", "description": "Details"}]
            },
            "ticket_result": []
        }

        res = create_jira_ticket(state)
        results = res.get("ticket_result", [])
        self.assertEqual(len(results), 1)
        self.assertIn("FAILED", results[0])

if __name__ == "__main__":
    unittest.main()
