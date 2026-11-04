import unittest
from unittest.mock import patch, MagicMock
import os
import tempfile

from sprint_database import (
    init_sprint_db,
    save_meeting,
    save_or_update_task,
    get_project_tasks,
    get_task_history
)
from sprint_comparison_engine import (
    compare_sprint_meetings,
    calculate_task_similarity,
    find_candidate_matches
)

class TestSprintComparisonEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = os.path.join(self.temp_dir.name, "test_engine.db")
        init_sprint_db(self.test_db_path)

        # Snapshot production reminders.db size for safety verification
        self.reminders_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminders.db")
        self.reminders_initial_size = os.path.getsize(self.reminders_path) if os.path.exists(self.reminders_path) else None

        # Pre-seed Meeting 1
        save_meeting(
            meeting_id="m_001",
            project_key="SCRUM",
            meeting_title="Sprint 1 Kickoff",
            transcript_hash="hash_m1",
            summary="Initial sprint planning",
            db_path=self.test_db_path
        )

    def tearDown(self):
        self.temp_dir.cleanup()

        # Verify reminders.db was not modified
        if self.reminders_initial_size is not None and os.path.exists(self.reminders_path):
            current_size = os.path.getsize(self.reminders_path)
            self.assertEqual(
                self.reminders_initial_size,
                current_size,
                "SAFETY VIOLATION: reminders.db was modified during engine tests!"
            )

    def test_newly_assigned_task(self):
        """Test that tasks introduced in the current meeting are identified as NEWLY ASSIGNED."""
        action_items = [
            {"task": "Build the auth microservice", "assignee": "Kashif", "deadline": "2026-11-15"}
        ]

        result = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_002",
            current_action_items=action_items,
            sync_jira=False,
            db_path=self.test_db_path
        )

        self.assertEqual(len(result["newly_assigned_tasks"]), 1)
        self.assertEqual(result["newly_assigned_tasks"][0]["task_name"], "Build the auth microservice")
        self.assertEqual(result["newly_assigned_tasks"][0]["classification"], "NEWLY ASSIGNED")

    def test_explicitly_completed_task(self):
        """Test task explicitly completed via transcript/progress or status in current meeting."""
        # Seed previous task in m_001
        save_or_update_task(
            project_key="SCRUM",
            task_name="Fix memory leak in redis worker",
            meeting_id="m_001",
            assignee="Ali",
            status="PENDING",
            db_path=self.test_db_path
        )

        # Meeting 2: Ali reports task is completed and merged
        action_items = [
            {
                "task": "Fix memory leak in redis worker",
                "assignee": "Ali",
                "progress": "Memory leak has been resolved, PR merged and deployed",
                "status": "COMPLETED"
            }
        ]

        result = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_002",
            current_action_items=action_items,
            sync_jira=False,
            db_path=self.test_db_path
        )

        self.assertEqual(len(result["completed_tasks"]), 1)
        comp_task = result["completed_tasks"][0]
        self.assertEqual(comp_task["task_name"], "Fix memory leak in redis worker")
        self.assertEqual(comp_task["status"], "COMPLETED")
        self.assertIn("resolved", comp_task["reason"])

    def test_task_still_pending(self):
        """Test task mentioned in latest meeting that remains pending."""
        save_or_update_task(
            project_key="SCRUM",
            task_name="Refactor user repository layer",
            meeting_id="m_001",
            assignee="Priya",
            deadline="2026-11-20",
            status="PENDING",
            db_path=self.test_db_path
        )

        action_items = [
            {
                "task": "Refactor user repository layer",
                "assignee": "Priya",
                "deadline": "2026-11-20",
                "status": "PENDING"
            }
        ]

        result = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_002",
            current_action_items=action_items,
            sync_jira=False,
            db_path=self.test_db_path
        )

        self.assertEqual(len(result["pending_tasks"]), 1)
        self.assertEqual(result["pending_tasks"][0]["task_name"], "Refactor user repository layer")

    def test_task_marked_in_progress(self):
        """Test task transitioned from PENDING to IN_PROGRESS."""
        save_or_update_task(
            project_key="SCRUM",
            task_name="Configure Kubernetes cluster ingress",
            meeting_id="m_001",
            assignee="Ali",
            status="PENDING",
            db_path=self.test_db_path
        )

        action_items = [
            {
                "task": "Configure Kubernetes cluster ingress",
                "assignee": "Ali",
                "progress": "Currently working on Nginx ingress TLS certs",
                "status": "IN_PROGRESS"
            }
        ]

        result = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_002",
            current_action_items=action_items,
            sync_jira=False,
            db_path=self.test_db_path
        )

        self.assertEqual(len(result["in_progress_tasks"]), 1)
        self.assertEqual(result["in_progress_tasks"][0]["task_name"], "Configure Kubernetes cluster ingress")
        self.assertEqual(result["in_progress_tasks"][0]["status"], "IN_PROGRESS")

    def test_assignee_change(self):
        """Test detecting assignee reassignment."""
        save_or_update_task(
            project_key="SCRUM",
            task_name="Set up end to end cypress tests",
            meeting_id="m_001",
            assignee="Ali",
            status="PENDING",
            db_path=self.test_db_path
        )

        action_items = [
            {
                "task": "Set up end to end cypress tests",
                "assignee": "Megashree",
                "status": "PENDING"
            }
        ]

        result = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_002",
            current_action_items=action_items,
            sync_jira=False,
            db_path=self.test_db_path
        )

        self.assertEqual(len(result["changed_tasks"]), 1)
        changed_task = result["changed_tasks"][0]
        self.assertEqual(changed_task["previous_assignee"], "Ali")
        self.assertEqual(changed_task["new_assignee"], "Megashree")
        self.assertIn("Assignee changed", changed_task["reason"])

    def test_deadline_change(self):
        """Test detecting deadline extension."""
        save_or_update_task(
            project_key="SCRUM",
            task_name="Audit security permissions",
            meeting_id="m_001",
            deadline="2026-11-05",
            status="PENDING",
            db_path=self.test_db_path
        )

        action_items = [
            {
                "task": "Audit security permissions",
                "deadline": "2026-11-18",
                "status": "PENDING"
            }
        ]

        result = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_002",
            current_action_items=action_items,
            sync_jira=False,
            db_path=self.test_db_path
        )

        self.assertEqual(len(result["changed_tasks"]), 1)
        changed_task = result["changed_tasks"][0]
        self.assertEqual(changed_task["previous_deadline"], "2026-11-05")
        self.assertEqual(changed_task["new_deadline"], "2026-11-18")

    def test_jira_issue_key_matching(self):
        """Test matching tasks by exact Jira issue key."""
        save_or_update_task(
            project_key="SCRUM",
            task_name="Original Jira Task Title",
            meeting_id="m_001",
            jira_issue_key="SCRUM-501",
            status="PENDING",
            db_path=self.test_db_path
        )

        # In meeting 2, task description slightly rephrased, but Jira key matches exactly
        action_items = [
            {
                "task": "Updated phrasing of Jira task",
                "jira_issue_key": "SCRUM-501",
                "status": "IN_PROGRESS"
            }
        ]

        result = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_002",
            current_action_items=action_items,
            sync_jira=False,
            db_path=self.test_db_path
        )

        self.assertEqual(len(result["in_progress_tasks"]), 1)
        self.assertEqual(result["in_progress_tasks"][0]["jira_issue_key"], "SCRUM-501")
        self.assertEqual(len(result["newly_assigned_tasks"]), 0)

    def test_stable_task_id_matching(self):
        """Test matching tasks by stable internal task ID."""
        t1 = save_or_update_task(
            project_key="SCRUM",
            task_name="Internal backend migration",
            meeting_id="m_001",
            status="PENDING",
            db_path=self.test_db_path
        )
        task_id = t1["task_id"]

        action_items = [
            {
                "task_id": task_id,
                "task": "Internal backend migration continuation",
                "status": "IN_PROGRESS"
            }
        ]

        result = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_002",
            current_action_items=action_items,
            sync_jira=False,
            db_path=self.test_db_path
        )

        self.assertEqual(len(result["in_progress_tasks"]), 1)
        self.assertEqual(result["in_progress_tasks"][0]["task_id"], task_id)

    def test_ambiguous_task_matching(self):
        """Test that multiple similar candidates are flagged as NEEDS REVIEW."""
        # Two very similar tasks exist in the database
        save_or_update_task(
            project_key="SCRUM",
            task_name="Test payment flow on staging",
            meeting_id="m_001",
            assignee="Ali",
            status="PENDING",
            db_path=self.test_db_path
        )
        save_or_update_task(
            project_key="SCRUM",
            task_name="Test payment flow on production",
            meeting_id="m_001",
            assignee="Priya",
            status="PENDING",
            db_path=self.test_db_path
        )

        # Incoming task is vague: "Test payment flow"
        action_items = [
            {"task": "Test payment flow", "assignee": "Kashif"}
        ]

        result = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_002",
            current_action_items=action_items,
            sync_jira=False,
            db_path=self.test_db_path
        )

        self.assertEqual(len(result["needs_review_tasks"]), 1)
        self.assertIn("Ambiguous matching candidates", result["needs_review_tasks"][0]["reason"])

    def test_absence_guard(self):
        """CRITICAL: A task absent from the latest meeting must remain PENDING, not completed."""
        save_or_update_task(
            project_key="SCRUM",
            task_name="Implement legacy database connector",
            meeting_id="m_001",
            assignee="Rahul",
            status="PENDING",
            db_path=self.test_db_path
        )

        # In Meeting 2, nobody talked about Rahul's task at all!
        action_items = [
            {"task": "Completely different new task", "assignee": "Kashif"}
        ]

        result = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_002",
            current_action_items=action_items,
            sync_jira=False,
            db_path=self.test_db_path
        )

        # Rahul's task must still be in pending!
        pending_names = [t["task_name"] for t in result["pending_tasks"]]
        self.assertIn("Implement legacy database connector", pending_names)
        self.assertEqual(len(result["completed_tasks"]), 0)

    def test_reopened_jira_issue(self):
        """Test Jira issue transitioning from COMPLETED back to IN_PROGRESS/PENDING."""
        # Seed a completed task
        save_or_update_task(
            project_key="SCRUM",
            task_name="Fix login SSL handshake error",
            meeting_id="m_001",
            jira_issue_key="SCRUM-601",
            status="COMPLETED",
            completion_evidence="Previously marked completed",
            db_path=self.test_db_path
        )

        # Mock Jira reporting this issue was reopened to In Progress
        mock_jira = [
            {
                "key": "SCRUM-601",
                "summary": "Fix login SSL handshake error",
                "status_name": "In Progress",
                "status_category": "In Progress",
                "sprint_status": "IN_PROGRESS",
                "assignee_name": "Ali"
            }
        ]

        result = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_002",
            current_action_items=[],
            jira_issues=mock_jira,
            sync_jira=True,
            db_path=self.test_db_path
        )

        # Should be classified as in_progress, NOT completed
        in_prog_keys = [t["jira_issue_key"] for t in result["in_progress_tasks"]]
        self.assertIn("SCRUM-601", in_prog_keys)

        # Check database task history reflects the reopen transition
        hist = get_task_history("SCRUM-601", db_path=self.test_db_path)
        reopen_entry = next(h for h in hist if h["new_status"] == "IN_PROGRESS")
        self.assertEqual(reopen_entry["previous_status"], "COMPLETED")
        self.assertIn("Reopened in Jira", reopen_entry["change_reason"])

    def test_duplicate_meeting_processing(self):
        """Test running the comparison twice on the same meeting produces idempotent results without duplicating tasks."""
        action_items = [
            {"task": "Build billing invoice generator", "assignee": "Priya", "status": "PENDING"}
        ]

        # First run
        res1 = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_002",
            current_action_items=action_items,
            sync_jira=False,
            db_path=self.test_db_path
        )

        # Second run
        res2 = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_002",
            current_action_items=action_items,
            sync_jira=False,
            db_path=self.test_db_path
        )

        self.assertEqual(res1["summary_counts"], res2["summary_counts"])
        all_tasks = get_project_tasks(project_key="SCRUM", db_path=self.test_db_path)
        self.assertEqual(len(all_tasks), 1)

    def test_project_isolation(self):
        """Test that comparison strictly isolates projects."""
        save_or_update_task(
            project_key="PROJECT_A",
            task_name="Task belonging to A",
            meeting_id="m_a1",
            status="PENDING",
            db_path=self.test_db_path
        )

        result_b = compare_sprint_meetings(
            project_key="PROJECT_B",
            current_meeting_id="m_b1",
            current_action_items=[{"task": "Task belonging to B"}],
            sync_jira=False,
            db_path=self.test_db_path
        )

        self.assertEqual(result_b["project_key"], "PROJECT_B")
        self.assertEqual(len(result_b["newly_assigned_tasks"]), 1)
        self.assertEqual(len(result_b["pending_tasks"]), 0)

    def test_missing_previous_meeting_and_empty_tasks(self):
        """Test first meeting with no previous meeting and empty action items."""
        result = compare_sprint_meetings(
            project_key="BRAND_NEW_PROJ",
            current_meeting_id="m_first",
            current_action_items=[],
            sync_jira=False,
            db_path=self.test_db_path
        )

        self.assertIsNone(result["previous_meeting_id"])
        self.assertEqual(result["summary_counts"]["total_active_tasks"], 0)
        self.assertEqual(len(result["completed_tasks"]), 0)
        self.assertEqual(len(result["pending_tasks"]), 0)

    def test_linguistic_completion_detection(self):
        """Test the 6 specific linguistic completion audit cases:
           1. 'The task is completed.' -> COMPLETED
           2. 'The task is not completed.' -> NOT COMPLETED (remains PENDING)
           3. 'We will complete it tomorrow.' -> NOT COMPLETED (remains PENDING)
           4. 'The task may be completed.' -> NEEDS REVIEW
           5. 'The task is still in progress.' -> IN_PROGRESS
           6. 'We fixed and deployed the issue.' -> COMPLETED
        """
        # Case 1: Completed
        res1 = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_ling_1",
            current_action_items=[{"task": "Setup CI pipeline", "progress": "The task is completed."}],
            sync_jira=False,
            db_path=self.test_db_path
        )
        self.assertEqual(len(res1["completed_tasks"]), 1)

        # Case 2: Negation -> NOT COMPLETED (remains PENDING)
        res2 = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_ling_2",
            current_action_items=[{"task": "Setup CI pipeline", "progress": "The task is not completed."}],
            sync_jira=False,
            db_path=self.test_db_path
        )
        self.assertEqual(len(res2["completed_tasks"]), 0)

        # Case 3: Future intention -> NOT COMPLETED (remains PENDING)
        res3 = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_ling_3",
            current_action_items=[{"task": "Setup CI pipeline", "progress": "We will complete it tomorrow."}],
            sync_jira=False,
            db_path=self.test_db_path
        )
        self.assertEqual(len(res3["completed_tasks"]), 0)

        # Case 4: Tentative / uncertain -> NEEDS REVIEW
        res4 = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_ling_4",
            current_action_items=[{"task": "Setup CI pipeline", "progress": "The task may be completed."}],
            sync_jira=False,
            db_path=self.test_db_path
        )
        self.assertEqual(len(res4["needs_review_tasks"]), 1)
        self.assertIn("Tentative or uncertain", res4["needs_review_tasks"][0]["reason"])

        # Case 5: In progress
        res5 = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_ling_5",
            current_action_items=[{"task": "Setup CI pipeline", "progress": "The task is still in progress."}],
            sync_jira=False,
            db_path=self.test_db_path
        )
        self.assertEqual(len(res5["in_progress_tasks"]), 1)

        # Case 6: Fixed and deployed -> COMPLETED
        res6 = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_ling_6",
            current_action_items=[{"task": "Setup CI pipeline", "progress": "We fixed and deployed the issue."}],
            sync_jira=False,
            db_path=self.test_db_path
        )
        self.assertEqual(len(res6["completed_tasks"]), 1)

if __name__ == "__main__":
    unittest.main()

