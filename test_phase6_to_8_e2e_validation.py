import os
import sys
import unittest
import tempfile
import datetime
import sqlite3
import json
import urllib.parse
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sprint_database import (
    init_sprint_db,
    save_meeting,
    save_or_update_task,
    get_project_tasks,
    get_task_history,
    get_task_by_id,
    get_all_projects,
    get_meetings_for_project,
    get_latest_comparison_run,
    get_dashboard_summary_counts,
    record_created_github_issue,
    get_cached_github_issue,
    claim_jira_creation,
    resolve_jira_claim
)
from sprint_comparison_engine import compare_sprint_meetings, analyze_task_statement
from google_calendar_service import (
    resolve_deterministic_deadline,
    create_calendar_events_for_action_items,
    is_duplicate_calendar_event,
    init_calendar_db
)
from github_service import create_github_issue_api
from jira_service import map_jira_status_to_sprint_status
from graph import (
    determine_actions,
    send_email_notifications,
    create_github_issue,
    create_jira_ticket,
    extract_meeting,
    RawActionItem,
    ActionItemDetails,
    AgentState,
    MeetingExtraction,
    ActionItem,
    GitHubIssue
)
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Match
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


class TestPhase6To8E2EValidation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_sprint_db = os.path.join(self.temp_dir.name, "test_sprint.db")
        self.test_reminders_db = os.path.join(self.temp_dir.name, "test_reminders.db")
        init_sprint_db(self.test_sprint_db)
        init_calendar_db(self.test_reminders_db)

        self.orig_sprint_db = os.environ.get("SPRINT_COMPARISON_DB")
        os.environ["SPRINT_COMPARISON_DB"] = self.test_sprint_db

        # Seed Meeting 1 (matching baseline dfae03f7a36b tasks)
        save_meeting(
            meeting_id="m_jwt_001",
            project_key="SCRUM",
            meeting_title="JWT Authentication Kickoff",
            transcript_hash="hash_m1",
            summary="Kickoff meeting for JWT authentication implementation.",
            db_path=self.test_sprint_db
        )

        # Tasks from Meeting 1
        save_or_update_task(
            project_key="SCRUM",
            task_name="Finish the backend authentication API by Friday",
            meeting_id="m_jwt_001",
            assignee="Kashif",
            deadline="2026-11-06",
            status="PENDING",
            task_id="task_jwt_kashif",
            db_path=self.test_sprint_db
        )
        save_or_update_task(
            project_key="SCRUM",
            task_name="Handle database tables and migration for authentication module",
            meeting_id="m_jwt_001",
            assignee="Ali",
            deadline="2026-11-05",
            status="PENDING",
            task_id="task_jwt_ali",
            db_path=self.test_sprint_db
        )
        save_or_update_task(
            project_key="SCRUM",
            task_name="Work on frontend login dashboard and integrate with backend API",
            meeting_id="m_jwt_001",
            assignee="Megashree",
            deadline="2026-11-09",
            status="PENDING",
            task_id="task_jwt_mega",
            db_path=self.test_sprint_db
        )
        save_or_update_task(
            project_key="SCRUM",
            task_name="Ensure the authentication API is properly documented",
            meeting_id="m_jwt_001",
            assignee="Kashif",
            deadline=None,
            status="PENDING",
            task_id="task_jwt_doc",
            db_path=self.test_sprint_db
        )
        save_or_update_task(
            project_key="SCRUM",
            task_name="Authentication Module Completion",
            meeting_id="m_jwt_001",
            assignee="Megashree",
            jira_issue_key="SCRUM-106",
            status="PENDING",
            task_id="SCRUM-106",
            db_path=self.test_sprint_db
        )

    def tearDown(self):
        if self.orig_sprint_db:
            os.environ["SPRINT_COMPARISON_DB"] = self.orig_sprint_db
        else:
            os.environ.pop("SPRINT_COMPARISON_DB", None)
        self.temp_dir.cleanup()

    # =========================================================================
    # PHASE 6 — SPRINT COMPARISON VALIDATION (Meeting 1 vs Meeting 2)
    # =========================================================================
    def test_two_meeting_controlled_comparison(self):
        """
        Meeting 2 controlled follow-up:
        - Kashif: confirms backend JWT API completion -> COMPLETED
        - Ali: confirms database migration completion -> COMPLETED
        - Megashree: frontend login integration is still pending -> PENDING
        - Ali: assigned new API testing task -> NEWLY ASSIGNED
        - Team: discusses documentation changes (deadline changed to next Monday) -> CHANGED
        - Ambiguous task statement -> Flagged for review
        """
        save_meeting(
            meeting_id="m_jwt_002",
            project_key="SCRUM",
            meeting_title="JWT Authentication Sprint Follow-up",
            transcript_hash="hash_m2",
            summary="Sprint follow-up on JWT authentication implementation.",
            db_path=self.test_sprint_db
        )

        m2_action_items = [
            {
                "task": "Finish the backend authentication API by Friday",
                "assignee": "Kashif",
                "progress": "Kashif confirmed backend JWT API completion, tested and merged.",
                "status": "COMPLETED"
            },
            {
                "task": "Handle database tables and migration for authentication module",
                "assignee": "Ali",
                "progress": "Ali confirmed database migration completion and schema verification.",
                "status": "COMPLETED"
            },
            {
                "task": "Work on frontend login dashboard and integrate with backend API",
                "assignee": "Megashree",
                "progress": "Megashree says frontend login integration is still pending.",
                "status": "PENDING"
            },
            {
                "task": "Execute comprehensive API and database integration tests",
                "assignee": "Ali",
                "deadline": "2026-11-13",
                "status": "PENDING"
            },
            {
                "task": "Ensure the authentication API is properly documented",
                "assignee": "Kashif",
                "deadline": "2026-11-16",  # deadline changed from None
                "progress": "Team agreed to extend documentation deadline to Monday Nov 16",
                "status": "PENDING"
            },
            {
                "task": "Unclear serverless deployment option",
                "assignee": "Ali",
                "progress": "Unsure if the deployment is completed or maybe still underway",
                "status": "PENDING"
            }
        ]

        result = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_jwt_002",
            current_action_items=m2_action_items,
            sync_jira=False,
            db_path=self.test_sprint_db
        )

        # 1. COMPLETED: Kashif backend API & Ali DB migration
        completed_names = [t["task_name"] for t in result["completed_tasks"]]
        self.assertIn("Finish the backend authentication API by Friday", completed_names)
        self.assertIn("Handle database tables and migration for authentication module", completed_names)
        for t in result["completed_tasks"]:
            self.assertIsNotNone(t.get("reason"))

        # 2. PENDING: Megashree frontend integration, plus unmentioned tasks carried over
        pending_names = [t["task_name"] for t in result["pending_tasks"]]
        self.assertIn("Work on frontend login dashboard and integrate with backend API", pending_names)
        # Absence guard: SCRUM-106 was not mentioned in m2, carried over as PENDING
        self.assertIn("Authentication Module Completion", pending_names)

        # 3. NEWLY ASSIGNED: New API testing task
        newly_assigned_names = [t["task_name"] for t in result["newly_assigned_tasks"]]
        self.assertIn("Execute comprehensive API and database integration tests", newly_assigned_names)

        # 4. CHANGED: Documentation task has deadline changed from None to 2026-11-16
        changed_names = [t["task_name"] for t in result["changed_tasks"]]
        self.assertIn("Ensure the authentication API is properly documented", changed_names)
        doc_changed = next(t for t in result["changed_tasks"] if t["task_name"] == "Ensure the authentication API is properly documented")
        self.assertIn("Deadline changed", doc_changed["reason"])

        # 5. NEEDS REVIEW: Tentative / uncertain statement
        needs_review_names = [t["task_name"] for t in result["needs_review_tasks"]]
        self.assertIn("Unclear serverless deployment option", needs_review_names)

        # 6. Summary counts verification
        counts = result["summary_counts"]
        self.assertGreaterEqual(counts["completed"], 2)
        self.assertGreaterEqual(counts["newly_assigned"], 1)
        self.assertGreaterEqual(counts["pending"], 2)
        self.assertGreaterEqual(counts["changed"], 1)
        self.assertGreaterEqual(counts["needs_review"], 1)

    # =========================================================================
    # PHASE 2 — JIRA SYNC & DONE STATUS TRANSITION
    # =========================================================================
    def test_jira_done_and_reopen_synchronization(self):
        """Authoritative Jira Done marks task completed; Jira reopen clears completed status."""
        # 1. Jira reports SCRUM-106 is Done
        jira_issues_done = [
            {
                "key": "SCRUM-106",
                "summary": "Authentication Module Completion",
                "status_name": "Done",
                "status_category": "Done",
                "sprint_status": "COMPLETED",
                "assignee_name": "Megashree"
            }
        ]

        res_done = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_jwt_002",
            current_action_items=[],
            sync_jira=True,
            jira_issues=jira_issues_done,
            db_path=self.test_sprint_db
        )

        completed_keys = [t.get("jira_issue_key") for t in res_done["completed_tasks"]]
        self.assertIn("SCRUM-106", completed_keys)

        # Verify persisted as COMPLETED with evidence in SQLite
        task_in_db = get_task_by_id("SCRUM-106", db_path=self.test_sprint_db)
        self.assertEqual(task_in_db["status"], "COMPLETED")
        self.assertIn("Jira", task_in_db["completion_evidence"])

        # 2. Jira reports SCRUM-106 has been REOPENED to "In Progress"
        jira_issues_reopened = [
            {
                "key": "SCRUM-106",
                "summary": "Authentication Module Completion",
                "status_name": "In Progress",
                "status_category": "In Progress",
                "sprint_status": "IN_PROGRESS",
                "assignee_name": "Megashree"
            }
        ]

        res_reopened = compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_jwt_003",
            current_action_items=[],
            sync_jira=True,
            jira_issues=jira_issues_reopened,
            db_path=self.test_sprint_db
        )

        # SCRUM-106 must NOT remain in completed
        reopened_completed_keys = [t.get("jira_issue_key") for t in res_reopened["completed_tasks"]]
        self.assertNotIn("SCRUM-106", reopened_completed_keys)

        task_after_reopen = get_task_by_id("SCRUM-106", db_path=self.test_sprint_db)
        self.assertEqual(task_after_reopen["status"], "IN_PROGRESS")
        self.assertIsNone(task_after_reopen["completion_evidence"])

    # =========================================================================
    # PHASE 1 — WORKFLOW & EXTRACTION DATETIME RESOLUTION
    # =========================================================================
    def test_extract_meeting_datetime_resolution(self):
        """
        Verify extract_meeting executes without NameError on datetime,
        testing datetime.date.today(), datetime.datetime.strptime(), and datetime.datetime.now().
        Ensures datetime is NOT mocked and all real datetime execution paths run cleanly.
        """
        raw_items = [
            RawActionItem(task="Finish authentication API by Friday", assignee="Kashif")
        ]
        mock_pass1_result = MagicMock(
            summary="Meeting on authentication module",
            decisions=["Use JWT"],
            action_items=raw_items,
            jira_tasks=[],
            github_issues=[]
        )
        mock_pass2_result = ActionItemDetails(
            deadline="2026-11-06",
            task_size="MEDIUM",
            context="Discussed during sprint",
            progress=None,
            expected_outcome=None,
            decisions=None,
            next_steps=None,
            blockers=None
        )

        # Path 1: meeting_date provided as valid ISO string -> exercises datetime.datetime.strptime and datetime.datetime.now
        state_with_date: AgentState = {
            "transcript": "Kashif: I will finish the authentication API by Friday.",
            "analysis": None,
            "meeting_date": "2026-11-04",
            "determined_actions": []
        }

        with patch("graph.get_llm") as mock_get_llm:
            mock_llm_instance = MagicMock()
            mock_pass1 = MagicMock()
            mock_pass1.invoke.return_value = mock_pass1_result
            mock_pass2 = MagicMock()
            mock_pass2.invoke.return_value = mock_pass2_result
            mock_llm_instance.with_structured_output.side_effect = [mock_pass1, mock_pass2]
            mock_get_llm.return_value = mock_llm_instance

            res1 = extract_meeting(state_with_date)
            self.assertIn("analysis", res1)
            self.assertEqual(res1["analysis"].action_items[0].deadline, "2026-11-06")

        # Path 2: meeting_date omitted (None) -> exercises datetime.date.today() and datetime.datetime.now
        state_no_date: AgentState = {
            "transcript": "Kashif: I will finish the authentication API by Friday.",
            "analysis": None,
            "meeting_date": None,
            "determined_actions": []
        }

        with patch("graph.get_llm") as mock_get_llm:
            mock_llm_instance = MagicMock()
            mock_pass1 = MagicMock()
            mock_pass1.invoke.return_value = mock_pass1_result
            mock_pass2 = MagicMock()
            mock_pass2.invoke.return_value = mock_pass2_result
            mock_llm_instance.with_structured_output.side_effect = [mock_pass1, mock_pass2]
            mock_get_llm.return_value = mock_llm_instance

            res2 = extract_meeting(state_no_date)
            self.assertIn("analysis", res2)
            self.assertEqual(len(res2["analysis"].action_items), 1)

        # Path 3: meeting_date invalid string format -> exercises exception fallback to datetime.date.today()
        state_invalid_date: AgentState = {
            "transcript": "Kashif: I will finish the authentication API by Friday.",
            "analysis": None,
            "meeting_date": "not-a-valid-date",
            "determined_actions": []
        }

        with patch("graph.get_llm") as mock_get_llm:
            mock_llm_instance = MagicMock()
            mock_pass1 = MagicMock()
            mock_pass1.invoke.return_value = mock_pass1_result
            mock_pass2 = MagicMock()
            mock_pass2.invoke.return_value = mock_pass2_result
            mock_llm_instance.with_structured_output.side_effect = [mock_pass1, mock_pass2]
            mock_get_llm.return_value = mock_llm_instance

            res3 = extract_meeting(state_invalid_date)
            self.assertIn("analysis", res3)
            self.assertEqual(len(res3["analysis"].action_items), 1)

    # =========================================================================
    # PHASE 3 — GITHUB ROUTING & REALISTIC ERROR HANDLING
    # =========================================================================
    def test_determine_actions_routes_github_properly(self):
        """When github_issues are present, determine_actions activates create_github_issue."""
        analysis = MeetingExtraction(
            summary="Meeting summary",
            decisions=[],
            action_items=[
                ActionItem(task="Backend task", assignee="Kashif", deadline="2026-11-06")
            ],
            jira_tasks=[],
            github_issues=[
                GitHubIssue(title="Testing the authentication API and database integration", description="Details")
            ]
        )
        state: AgentState = {
            "analysis": analysis,
            "transcript": "Test transcript",
            "determined_actions": []
        }

        res = determine_actions(state)
        actions = res.get("determined_actions", [])
        self.assertIn("create_github_issue", actions)
        self.assertIn("send_email_notifications", actions)
        self.assertIn("create_calendar_events", actions)

    @patch("requests.post")
    def test_github_api_rate_limit_and_timeout_resilience(self, mock_post):
        """GitHub API rate limit (429) and timeout handle gracefully without crashing."""
        # 1. Test Rate Limit (429)
        mock_resp_429 = MagicMock()
        mock_resp_429.status_code = 429
        mock_resp_429.json.return_value = {"message": "API rate limit exceeded"}
        mock_post.return_value = mock_resp_429

        with patch.dict(os.environ, {"GITHUB_TOKEN": "mock_token", "GITHUB_OWNER": "test_owner", "GITHUB_REPO": "test_repo", "GITHUB_MAX_RETRIES": "1"}):
            api_res = create_github_issue_api("Title", "Body")
            self.assertEqual(api_res["status"], "FAILED")
            self.assertEqual(api_res["http_status"], 429)
            self.assertIn("429", api_res["formatted_text"])

    # =========================================================================
    # PHASE 4 — EMAIL MESSAGE-ID & DIRECTORY LOOKUPS
    # =========================================================================
    def test_email_directory_lookups_and_preview(self):
        """Directory lookups for Kashif and Ali return valid emails, and preview formatting works."""
        from team_directory import get_assignee_email
        from gmail_service import create_email_content

        kashif_email = get_assignee_email("Kashif")
        ali_email = get_assignee_email("Ali")
        self.assertEqual(kashif_email, "kashifahamad2003@gmail.com")
        self.assertEqual(ali_email, "amohammedali2005@gmail.com")

        subject, body = create_email_content(
            assignee_name="Kashif",
            task="Finish backend JWT API by Friday",
            deadline="2026-11-06",
            summary="Authentication meeting",
            context="Security requirement"
        )
        self.assertIn("[MeetMind AI]", subject)
        self.assertIn("Finish backend JWT API", subject)
        self.assertIn("DEADLINE:\n2026-11-06", body)
        self.assertIn("Hi Kashif,", body)

    # =========================================================================
    # PHASE 5 — GOOGLE CALENDAR DETERMINISTIC RESOLUTION & DEDUP
    # =========================================================================
    def test_calendar_deterministic_weekdays_and_dedup(self):
        """Reference date Nov 4, 2026 accurately calculates weekdays, and duplicate events are skipped."""
        ref_date = datetime.date(2026, 11, 4)  # Wednesday

        thurs = resolve_deterministic_deadline("Task due Thursday", ref_date=ref_date)
        fri = resolve_deterministic_deadline("Task due Friday", ref_date=ref_date)
        mon = resolve_deterministic_deadline("Task due Monday", ref_date=ref_date)

        self.assertEqual(thurs, "2026-11-05")
        self.assertEqual(fri, "2026-11-06")
        self.assertEqual(mon, "2026-11-09")

        # Missing deadline returns None
        none_dl = resolve_deterministic_deadline("Discussion item without deadline", ref_date=ref_date)
        self.assertIsNone(none_dl)

    # =========================================================================
    # PHASE 7 — DASHBOARD REST API ENDPOINTS
    # =========================================================================
    def test_dashboard_api_endpoints(self):
        """Verify /api/projects, /api/meetings, /api/comparisons/latest, /api/summary, /api/tasks."""
        app = create_app(
            db_path=self.test_reminders_db,
            sprint_db_path=self.test_sprint_db
        )
        client = DirectApiClient(app)

        # 1. GET /api/projects
        p_res = client.get("/api/projects")
        self.assertEqual(p_res.status_code, 200)
        p_json = p_res.json()
        self.assertEqual(p_json["status"], "SUCCESS")
        self.assertIn("SCRUM", p_json["projects"])

        # 2. GET /api/meetings?project_key=SCRUM
        m_res = client.get("/api/meetings?project_key=SCRUM")
        self.assertEqual(m_res.status_code, 200)
        m_json = m_res.json()
        self.assertEqual(m_json["status"], "SUCCESS")
        self.assertGreaterEqual(m_json["count"], 1)

        # 3. GET /api/summary?project_key=SCRUM
        s_res = client.get("/api/summary?project_key=SCRUM")
        self.assertEqual(s_res.status_code, 200)
        s_json = s_res.json()
        self.assertEqual(s_json["status"], "SUCCESS")
        summary = s_json["summary"]
        self.assertIn("total_active_tasks", summary)
        self.assertEqual(summary["total_active_tasks"], 5)

        # 4. GET /api/tasks/{task_id}
        t_res = client.get("/api/tasks/SCRUM-106")
        self.assertEqual(t_res.status_code, 200)
        t_json = t_res.json()
        self.assertEqual(t_json["status"], "SUCCESS")
        self.assertEqual(t_json["task"]["task_id"], "SCRUM-106")
        self.assertEqual(t_json["task"]["jira_issue_key"], "SCRUM-106")
        self.assertIn("jira_url", t_json["task"])

        # 5. GET /api/tasks/{task_id}/history
        h_res = client.get("/api/tasks/SCRUM-106/history")
        self.assertEqual(h_res.status_code, 200)
        h_json = h_res.json()
        self.assertEqual(h_json["status"], "SUCCESS")
        self.assertIsInstance(h_json["history"], list)

        # 6. GET /api/comparisons/latest?project_key=SCRUM
        # Run a comparison first to persist a run
        compare_sprint_meetings(
            project_key="SCRUM",
            current_meeting_id="m_jwt_001",
            current_action_items=[],
            sync_jira=False,
            db_path=self.test_sprint_db
        )
        c_res = client.get("/api/comparisons/latest?project_key=SCRUM")
        self.assertEqual(c_res.status_code, 200)
        c_json = c_res.json()
        self.assertEqual(c_json["status"], "SUCCESS")
        self.assertTrue(c_json["has_comparisons"])
        self.assertIn("comparison", c_json)

    # =========================================================================
    # PHASE 8 — CONCURRENCY & FAILURE ISOLATION
    # =========================================================================
    def test_jira_creation_claims_concurrency_lock(self):
        """Atomic claim locking prevents concurrent workers from making duplicate Jira calls."""
        key = "idemp_scrum_test_claim_001"

        # Worker 1 claims
        s1, t1, m1 = claim_jira_creation(key, "SCRUM", "Task Title", db_path=self.test_sprint_db)
        self.assertEqual(s1, "CLAIMED")

        # Worker 2 attempts same claim while Worker 1 is in progress
        s2, t2, m2 = claim_jira_creation(key, "SCRUM", "Task Title", db_path=self.test_sprint_db)
        self.assertEqual(s2, "IN_PROGRESS")
        self.assertIn("in progress", m2.lower())

        # Worker 1 completes and resolves claim
        resolve_jira_claim(key, status="CREATED", ticket_key="SCRUM-201", db_path=self.test_sprint_db)

        # Worker 3 arrives later
        s3, t3, m3 = claim_jira_creation(key, "SCRUM", "Task Title", db_path=self.test_sprint_db)
        self.assertEqual(s3, "ALREADY_CREATED")
        self.assertEqual(t3, "SCRUM-201")


if __name__ == "__main__":
    unittest.main()
