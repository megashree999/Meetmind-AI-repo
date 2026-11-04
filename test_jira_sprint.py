import unittest
from unittest.mock import patch, MagicMock
import os
import tempfile
import requests

from jira_service import (
    get_jira_issues_for_project,
    get_jira_issue_details,
    get_jira_issue_status,
    map_jira_status_to_sprint_status,
    sync_jira_issues_to_sprint_db
)
from sprint_database import (
    init_sprint_db,
    get_project_tasks,
    get_task_history
)

class TestJiraSprintReadonly(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = os.path.join(self.temp_dir.name, "test_jira_sprint.db")
        init_sprint_db(self.test_db_path)

        # Snapshot reminders.db size for safety verification
        self.reminders_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminders.db")
        self.reminders_initial_size = os.path.getsize(self.reminders_path) if os.path.exists(self.reminders_path) else None

        # Set mock env vars
        self.env_patcher = patch.dict(os.environ, {
            "JIRA_BASE_URL": "https://test-meetmind.atlassian.net",
            "JIRA_EMAIL": "tester@example.com",
            "JIRA_API_TOKEN": "mock_secret_token_12345",
            "JIRA_PROJECT_KEY": "SCRUM",
            "JIRA_MAX_RETRIES": "1",
            "JIRA_TIMEOUT": "5"
        })
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()
        self.temp_dir.cleanup()

        # Verify reminders.db was not modified
        if self.reminders_initial_size is not None and os.path.exists(self.reminders_path):
            current_size = os.path.getsize(self.reminders_path)
            self.assertEqual(
                self.reminders_initial_size,
                current_size,
                "SAFETY VIOLATION: reminders.db was modified during Jira sprint tests!"
            )

    def test_status_mapping(self):
        """Test status category and name mappings."""
        self.assertEqual(map_jira_status_to_sprint_status("Done", "Done"), "COMPLETED")
        self.assertEqual(map_jira_status_to_sprint_status("Closed", "done"), "COMPLETED")
        self.assertEqual(map_jira_status_to_sprint_status("In Progress", "In Progress"), "IN_PROGRESS")
        self.assertEqual(map_jira_status_to_sprint_status("Code Review", "indeterminate"), "IN_PROGRESS")
        self.assertEqual(map_jira_status_to_sprint_status("To Do", "To Do"), "PENDING")
        self.assertEqual(map_jira_status_to_sprint_status("Backlog", "new"), "PENDING")
        self.assertEqual(map_jira_status_to_sprint_status("Blocked", "undefined"), "BLOCKED")
        self.assertEqual(map_jira_status_to_sprint_status("On Hold", "other"), "BLOCKED")

    @patch("requests.get")
    def test_successful_issue_retrieval(self, mock_get):
        """Test retrieving issues with full field parsing."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "total": 1,
            "issues": [
                {
                    "key": "SCRUM-101",
                    "fields": {
                        "summary": "Fix payment gateway timeout",
                        "description": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Detailed payment bug info"}]}]},
                        "status": {"name": "In Progress", "statusCategory": {"name": "In Progress"}},
                        "assignee": {"displayName": "Priya", "accountId": "acc_priya_123"},
                        "duedate": "2026-11-06",
                        "created": "2026-11-01T10:00:00.000Z",
                        "updated": "2026-11-02T12:00:00.000Z"
                    }
                }
            ]
        }
        mock_get.return_value = mock_resp

        result = get_jira_issues_for_project("SCRUM")
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["issues"]), 1)

        issue = result["issues"][0]
        self.assertEqual(issue["key"], "SCRUM-101")
        self.assertEqual(issue["summary"], "Fix payment gateway timeout")
        self.assertEqual(issue["description"], "Detailed payment bug info")
        self.assertEqual(issue["sprint_status"], "IN_PROGRESS")
        self.assertEqual(issue["assignee_name"], "Priya")
        self.assertEqual(issue["assignee_account_id"], "acc_priya_123")
        self.assertEqual(issue["due_date"], "2026-11-06")
        self.assertEqual(issue["issue_url"], "https://test-meetmind.atlassian.net/browse/SCRUM-101")

    @patch("requests.get")
    def test_pagination(self, mock_get):
        """Test handling multiple pages of search results."""
        page1 = MagicMock()
        page1.status_code = 200
        page1.json.return_value = {
            "total": 3,
            "startAt": 0,
            "maxResults": 2,
            "issues": [
                {"key": "SCRUM-1", "fields": {"summary": "Task 1", "status": {"name": "To Do"}}},
                {"key": "SCRUM-2", "fields": {"summary": "Task 2", "status": {"name": "To Do"}}}
            ]
        }

        page2 = MagicMock()
        page2.status_code = 200
        page2.json.return_value = {
            "total": 3,
            "startAt": 2,
            "maxResults": 2,
            "issues": [
                {"key": "SCRUM-3", "fields": {"summary": "Task 3", "status": {"name": "Done", "statusCategory": {"name": "Done"}}}}
            ]
        }

        mock_get.side_effect = [page1, page2]

        result = get_jira_issues_for_project("SCRUM", max_results=2, fetch_all=True)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["total"], 3)
        self.assertEqual(len(result["issues"]), 3)
        self.assertEqual([i["key"] for i in result["issues"]], ["SCRUM-1", "SCRUM-2", "SCRUM-3"])
        self.assertEqual(mock_get.call_count, 2)

    @patch("requests.get")
    def test_unassigned_and_missing_deadline(self, mock_get):
        """Test issue without assignee and without deadline."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "total": 1,
            "issues": [
                {
                    "key": "SCRUM-102",
                    "fields": {
                        "summary": "Unassigned backlog item",
                        "description": None,
                        "status": {"name": "To Do", "statusCategory": {"name": "To Do"}},
                        "assignee": None,
                        "duedate": None
                    }
                }
            ]
        }
        mock_get.return_value = mock_resp

        result = get_jira_issues_for_project("SCRUM")
        self.assertEqual(result["status"], "SUCCESS")
        issue = result["issues"][0]
        self.assertIsNone(issue["assignee_name"])
        self.assertIsNone(issue["assignee_account_id"])
        self.assertIsNone(issue["due_date"])
        self.assertEqual(issue["sprint_status"], "PENDING")

    @patch("requests.get")
    def test_jira_auth_failure(self, mock_get):
        """Test HTTP 401 Unauthorized handling."""
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {"errorMessages": ["Invalid credentials"]}
        mock_get.return_value = mock_resp

        result = get_jira_issues_for_project("SCRUM")
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("401", result["error"])

    @patch("requests.get")
    def test_jira_rate_limit(self, mock_get):
        """Test HTTP 429 Too Many Requests handling."""
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.json.return_value = {"errorMessages": ["Rate limit exceeded"]}
        mock_get.return_value = mock_resp

        result = get_jira_issues_for_project("SCRUM")
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("429", result["error"])

    @patch("requests.get")
    def test_jira_timeout(self, mock_get):
        """Test network timeout handling."""
        mock_get.side_effect = requests.exceptions.Timeout("Connection timed out")

        result = get_jira_issues_for_project("SCRUM")
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("Connection timed out", result["error"])

    @patch("requests.get")
    def test_get_issue_details_and_status(self, mock_get):
        """Test single issue details and status retrieval."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "key": "SCRUM-105",
            "fields": {
                "summary": "Integrate sprint comparison",
                "status": {"name": "Done", "statusCategory": {"name": "Done"}},
                "assignee": {"displayName": "Megashree"}
            }
        }
        mock_get.return_value = mock_resp

        details = get_jira_issue_details("SCRUM-105")
        self.assertEqual(details["status"], "SUCCESS")
        self.assertEqual(details["issue"]["key"], "SCRUM-105")

        status_res = get_jira_issue_status("SCRUM-105")
        self.assertEqual(status_res["status"], "SUCCESS")
        self.assertEqual(status_res["sprint_status"], "COMPLETED")
        self.assertEqual(status_res["status_name"], "Done")

    def test_database_synchronization_and_deduplication(self):
        """Test syncing Jira issues into sprint_comparison.db and preventing duplicates."""
        mock_issues = [
            {
                "key": "SCRUM-301",
                "summary": "Database index optimization",
                "status_name": "In Progress",
                "status_category": "In Progress",
                "sprint_status": "IN_PROGRESS",
                "assignee_name": "Ali",
                "due_date": "2026-11-08"
            },
            {
                "key": "SCRUM-302",
                "summary": "Fix security vulnerability",
                "status_name": "Done",
                "status_category": "Done",
                "sprint_status": "COMPLETED",
                "assignee_name": "Kashif",
                "due_date": "2026-11-04"
            }
        ]

        # 1. First synchronization
        res1 = sync_jira_issues_to_sprint_db(
            project_key="SCRUM",
            issues=mock_issues,
            meeting_id="m_sync_001",
            db_path=self.test_db_path
        )
        self.assertEqual(res1["status"], "SUCCESS")
        self.assertEqual(res1["created_count"], 2)
        self.assertEqual(res1["updated_count"], 0)

        tasks = get_project_tasks(project_key="SCRUM", db_path=self.test_db_path)
        self.assertEqual(len(tasks), 2)
        task_scrum302 = next(t for t in tasks if t["jira_issue_key"] == "SCRUM-302")
        self.assertEqual(task_scrum302["status"], "COMPLETED")
        self.assertIn("Verified completed in Jira", task_scrum302["completion_evidence"])

        # 2. Second synchronization with an update (SCRUM-301 moves to Done)
        mock_issues_updated = [
            {
                "key": "SCRUM-301",
                "summary": "Database index optimization",
                "status_name": "Done",
                "status_category": "Done",
                "sprint_status": "COMPLETED",
                "assignee_name": "Ali",
                "due_date": "2026-11-08"
            },
            {
                "key": "SCRUM-302",
                "summary": "Fix security vulnerability",
                "status_name": "Done",
                "status_category": "Done",
                "sprint_status": "COMPLETED",
                "assignee_name": "Kashif",
                "due_date": "2026-11-04"
            }
        ]

        res2 = sync_jira_issues_to_sprint_db(
            project_key="SCRUM",
            issues=mock_issues_updated,
            meeting_id="m_sync_002",
            db_path=self.test_db_path
        )
        self.assertEqual(res2["status"], "SUCCESS")
        self.assertEqual(res2["created_count"], 0)
        self.assertEqual(res2["updated_count"], 1)  # Only SCRUM-301 changed status

        # Total tasks must still be 2 (no duplicates!)
        tasks_after = get_project_tasks(project_key="SCRUM", db_path=self.test_db_path)
        self.assertEqual(len(tasks_after), 2)

        # Check history for SCRUM-301
        hist = get_task_history("SCRUM-301", db_path=self.test_db_path)
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[1]["previous_status"], "IN_PROGRESS")
        self.assertEqual(hist[1]["new_status"], "COMPLETED")

if __name__ == "__main__":
    unittest.main()

