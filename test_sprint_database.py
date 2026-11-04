import unittest
import os
import tempfile
import sqlite3
from sprint_database import (
    init_sprint_db,
    get_sprint_connection,
    save_meeting,
    get_previous_meeting,
    save_or_update_task,
    get_project_tasks,
    get_task_history,
    normalize_task_name,
    ALLOWED_STATUSES
)

class TestSprintDatabase(unittest.TestCase):
    def setUp(self):
        # Create a dedicated temporary database file for each test
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = os.path.join(self.temp_dir.name, "test_sprint.db")

        # Snapshot reminders.db state if it exists
        self.reminders_db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminders.db")
        self.reminders_initial_size = os.path.getsize(self.reminders_db_path) if os.path.exists(self.reminders_db_path) else None

    def tearDown(self):
        # Clean up temporary test files
        self.temp_dir.cleanup()

        # Verify reminders.db was not modified
        if self.reminders_initial_size is not None and os.path.exists(self.reminders_db_path):
            current_size = os.path.getsize(self.reminders_db_path)
            self.assertEqual(
                self.reminders_initial_size,
                current_size,
                "SAFETY VIOLATION: reminders.db was modified during sprint database testing!"
            )

    def test_database_initialization(self):
        """Test tables, columns, constraints, and indexes are created properly."""
        init_sprint_db(self.test_db_path)
        self.assertTrue(os.path.exists(self.test_db_path))

        conn = get_sprint_connection(self.test_db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row["name"] for row in cursor.fetchall()}
        conn.close()

        self.assertIn("meetings", tables)
        self.assertIn("project_tasks", tables)
        self.assertIn("task_history", tables)

    def test_meeting_insertion_and_ordering(self):
        """Test saving sequential meetings for a project."""
        res1 = save_meeting(
            meeting_id="m_001",
            project_key="SCRUM",
            meeting_title="Sprint 1 Planning",
            transcript_hash="hash_aaa111",
            summary="Kickoff sprint 1",
            db_path=self.test_db_path
        )
        self.assertEqual(res1["status"], "SUCCESS")
        self.assertEqual(res1["sequence_number"], 1)

        res2 = save_meeting(
            meeting_id="m_002",
            project_key="SCRUM",
            meeting_title="Sprint 1 Review",
            transcript_hash="hash_bbb222",
            summary="Review sprint 1 deliverables",
            db_path=self.test_db_path
        )
        self.assertEqual(res2["status"], "SUCCESS")
        self.assertEqual(res2["sequence_number"], 2)

    def test_duplicate_meeting_prevention(self):
        """Test preventing duplicate meeting insertion by meeting_id and transcript_hash."""
        save_meeting(
            meeting_id="m_001",
            project_key="SCRUM",
            meeting_title="Sprint 1 Kickoff",
            transcript_hash="hash_unique_123",
            summary="First meeting",
            db_path=self.test_db_path
        )

        # 1. Duplicate meeting_id
        dup1 = save_meeting(
            meeting_id="m_001",
            project_key="SCRUM",
            meeting_title="Different Title Same ID",
            transcript_hash="hash_different_456",
            db_path=self.test_db_path
        )
        self.assertEqual(dup1["status"], "DUPLICATE")
        self.assertEqual(dup1["reason"], "MEETING_ID_EXISTS")

        # 2. Duplicate transcript_hash in the same project
        dup2 = save_meeting(
            meeting_id="m_002",
            project_key="SCRUM",
            meeting_title="Same Hash",
            transcript_hash="hash_unique_123",
            db_path=self.test_db_path
        )
        self.assertEqual(dup2["status"], "DUPLICATE")
        self.assertEqual(dup2["reason"], "TRANSCRIPT_HASH_EXISTS")

    def test_previous_meeting_retrieval(self):
        """Test retrieving the immediately preceding meeting."""
        save_meeting(
            meeting_id="m_001",
            project_key="SCRUM",
            meeting_title="Meeting 1",
            transcript_hash="hash_1",
            db_path=self.test_db_path
        )
        save_meeting(
            meeting_id="m_002",
            project_key="SCRUM",
            meeting_title="Meeting 2",
            transcript_hash="hash_2",
            db_path=self.test_db_path
        )
        save_meeting(
            meeting_id="m_003",
            project_key="SCRUM",
            meeting_title="Meeting 3",
            transcript_hash="hash_3",
            db_path=self.test_db_path
        )

        # For Meeting 3, previous should be Meeting 2
        prev_for_m3 = get_previous_meeting(
            project_key="SCRUM",
            current_meeting_id="m_003",
            db_path=self.test_db_path
        )
        self.assertIsNotNone(prev_for_m3)
        self.assertEqual(prev_for_m3["meeting_id"], "m_002")
        self.assertEqual(prev_for_m3["sequence_number"], 2)

        # For Meeting 2, previous should be Meeting 1
        prev_for_m2 = get_previous_meeting(
            project_key="SCRUM",
            current_sequence_number=2,
            db_path=self.test_db_path
        )
        self.assertIsNotNone(prev_for_m2)
        self.assertEqual(prev_for_m2["meeting_id"], "m_001")

        # For Meeting 1, previous should be None
        prev_for_m1 = get_previous_meeting(
            project_key="SCRUM",
            current_meeting_id="m_001",
            db_path=self.test_db_path
        )
        self.assertIsNone(prev_for_m1)

    def test_task_insertion_and_stable_id(self):
        """Test task creation and stable ID reuse."""
        res = save_or_update_task(
            project_key="SCRUM",
            task_name="Implement OAuth authentication",
            meeting_id="m_001",
            assignee="Kashif",
            assignee_email="kashif@example.com",
            deadline="2026-11-10",
            task_size="LARGE",
            jira_issue_key="SCRUM-201",
            status="PENDING",
            db_path=self.test_db_path
        )
        self.assertEqual(res["action"], "CREATED")
        task_id = res["task_id"]
        self.assertTrue(task_id.startswith("SCRUM-201") or task_id.startswith("task_"))

        # Query all tasks
        tasks = get_project_tasks(project_key="SCRUM", db_path=self.test_db_path)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task_name"], "Implement OAuth authentication")
        self.assertEqual(tasks[0]["status"], "PENDING")
        self.assertEqual(tasks[0]["jira_issue_key"], "SCRUM-201")

    def test_task_history_preservation(self):
        """Test that updating status, assignee, or deadline logs history."""
        # Initial creation
        t1 = save_or_update_task(
            project_key="SCRUM",
            task_name="Fix login timeout bug",
            meeting_id="m_001",
            assignee="Kashif",
            deadline="2026-11-05",
            status="PENDING",
            jira_issue_key="SCRUM-202",
            db_path=self.test_db_path
        )
        task_id = t1["task_id"]

        # Meeting 2: Kashif completes the task
        t2 = save_or_update_task(
            project_key="SCRUM",
            task_name="Fix login timeout bug",
            meeting_id="m_002",
            assignee="Kashif",
            deadline="2026-11-05",
            status="COMPLETED",
            completion_evidence="Kashif reported: 'Login timeout bug has been fixed and merged.'",
            jira_issue_key="SCRUM-202",
            db_path=self.test_db_path
        )
        self.assertEqual(t2["action"], "UPDATED")
        self.assertEqual(t2["task_id"], task_id)
        self.assertEqual(t2["task"]["status"], "COMPLETED")
        self.assertEqual(t2["task"]["last_seen_meeting_id"], "m_002")

        # Check history
        history = get_task_history(task_id=task_id, db_path=self.test_db_path)
        # 1 entry for initial creation + 1 entry for status update
        self.assertEqual(len(history), 2)
        initial_hist = history[0]
        update_hist = history[1]

        self.assertEqual(initial_hist["new_status"], "PENDING")
        self.assertIsNone(initial_hist["previous_status"])

        self.assertEqual(update_hist["previous_status"], "PENDING")
        self.assertEqual(update_hist["new_status"], "COMPLETED")
        self.assertIn("Status changed from PENDING to COMPLETED", update_hist["change_reason"])

    def test_task_assignee_and_deadline_changes(self):
        """Test task reassignment and deadline changes across meetings."""
        t1 = save_or_update_task(
            project_key="SCRUM",
            task_name="Perform load testing on payment gateway",
            meeting_id="m_001",
            assignee="Ali",
            deadline="2026-11-06",
            status="PENDING",
            db_path=self.test_db_path
        )
        task_id = t1["task_id"]

        # Meeting 2: Reassigned to Priya and deadline pushed to 2026-11-12
        t2 = save_or_update_task(
            project_key="SCRUM",
            task_name="Perform load testing on payment gateway",
            meeting_id="m_002",
            assignee="Priya",
            deadline="2026-11-12",
            status="IN_PROGRESS",
            change_reason="Ali reassigned to frontend; Priya assigned and deadline extended",
            db_path=self.test_db_path
        )
        self.assertEqual(t2["action"], "UPDATED")

        history = get_task_history(task_id=task_id, db_path=self.test_db_path)
        self.assertEqual(len(history), 2)
        change_entry = history[1]

        self.assertEqual(change_entry["previous_assignee"], "Ali")
        self.assertEqual(change_entry["new_assignee"], "Priya")
        self.assertEqual(change_entry["previous_deadline"], "2026-11-06")
        self.assertEqual(change_entry["new_deadline"], "2026-11-12")
        self.assertEqual(change_entry["previous_status"], "PENDING")
        self.assertEqual(change_entry["new_status"], "IN_PROGRESS")

    def test_multiple_projects_isolation(self):
        """Test that tasks and meetings across different projects remain completely separated."""
        # Project 1: SCRUM
        save_meeting(
            meeting_id="scrum_001",
            project_key="SCRUM",
            meeting_title="Scrum Standup",
            transcript_hash="hash_scrum",
            db_path=self.test_db_path
        )
        save_or_update_task(
            project_key="SCRUM",
            task_name="Scrum specific task",
            meeting_id="scrum_001",
            status="PENDING",
            db_path=self.test_db_path
        )

        # Project 2: KANBAN
        save_meeting(
            meeting_id="kanban_001",
            project_key="KANBAN",
            meeting_title="Kanban Sync",
            transcript_hash="hash_kanban",
            db_path=self.test_db_path
        )
        save_or_update_task(
            project_key="KANBAN",
            task_name="Kanban specific task",
            meeting_id="kanban_001",
            status="IN_PROGRESS",
            db_path=self.test_db_path
        )

        scrum_tasks = get_project_tasks(project_key="SCRUM", db_path=self.test_db_path)
        kanban_tasks = get_project_tasks(project_key="KANBAN", db_path=self.test_db_path)

        self.assertEqual(len(scrum_tasks), 1)
        self.assertEqual(scrum_tasks[0]["task_name"], "Scrum specific task")

        self.assertEqual(len(kanban_tasks), 1)
        self.assertEqual(kanban_tasks[0]["task_name"], "Kanban specific task")

    def test_status_validation_guard(self):
        """Test that invalid statuses are rejected immediately."""
        with self.assertRaises(ValueError):
            save_or_update_task(
                project_key="SCRUM",
                task_name="Invalid status task",
                meeting_id="m_001",
                status="INVALID_STATUS",
                db_path=self.test_db_path
            )

if __name__ == "__main__":
    unittest.main()

