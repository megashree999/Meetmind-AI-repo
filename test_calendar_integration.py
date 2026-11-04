"""
test_calendar_integration.py
─────────────────────────────
Unit and integration tests for google_calendar_service.py and the
create_calendar_events LangGraph node in graph.py.

All tests use mocked APIs — no real Google Calendar events are created,
no real emails are sent, and no real tokens are required.

Run:
    .\venv\Scripts\python.exe test_calendar_integration.py
"""

import os
import sys
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

# ── Ensure project root is on sys.path ───────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Suppress all dotenv / print noise during tests ───────────────────────────
import io
os.environ.setdefault("CALENDAR_NOTIFICATIONS_ENABLED", "false")
os.environ.setdefault("GOOGLE_CALENDAR_ID", "primary")
os.environ.setdefault("GOOGLE_CALENDAR_SEND_UPDATES", "none")
os.environ.setdefault("GOOGLE_CALENDAR_TOKEN_FILE", "token_calendar.json")

import google_calendar_service as cal_svc


# ── Shared test DB helper ─────────────────────────────────────────────────────
def make_temp_db() -> str:
    """Return a path to a fresh temporary SQLite database."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


# =============================================================================
# 1. DEADLINE PARSING
# =============================================================================
class TestParseDeadline(unittest.TestCase):
    """Validate safe deadline parsing for all edge cases."""

    def test_valid_iso_date(self):
        result = cal_svc.parse_deadline("2026-12-25")
        self.assertIsNotNone(result)
        self.assertEqual(result.strftime("%Y-%m-%d"), "2026-12-25")

    def test_none_returns_none(self):
        self.assertIsNone(cal_svc.parse_deadline(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(cal_svc.parse_deadline(""))

    def test_literal_none_string_returns_none(self):
        self.assertIsNone(cal_svc.parse_deadline("none"))

    def test_na_string_returns_none(self):
        self.assertIsNone(cal_svc.parse_deadline("N/A"))

    def test_null_string_returns_none(self):
        self.assertIsNone(cal_svc.parse_deadline("null"))

    def test_malformed_date_returns_none(self):
        self.assertIsNone(cal_svc.parse_deadline("25-12-2026"))

    def test_non_string_returns_none(self):
        self.assertIsNone(cal_svc.parse_deadline(20261225))


# =============================================================================
# 2. TASK TITLE CLEANING
# =============================================================================
class TestCleanTaskTitle(unittest.TestCase):

    def test_removes_temporal_phrase_by_friday(self):
        result = cal_svc.clean_task_title("Update documentation by friday")
        self.assertNotIn("by friday", result.lower())

    def test_empty_returns_default(self):
        result = cal_svc.clean_task_title("")
        self.assertEqual(result, "Untitled Action Item")

    def test_none_returns_default(self):
        result = cal_svc.clean_task_title(None)
        self.assertEqual(result, "Untitled Action Item")

    def test_no_temporal_phrase_unchanged(self):
        title = "Fix authentication bug"
        result = cal_svc.clean_task_title(title)
        self.assertIn("authentication", result.lower())


# =============================================================================
# 3. DATABASE INITIALISATION & DUPLICATE DETECTION
# =============================================================================
class TestDatabaseOperations(unittest.TestCase):

    def setUp(self):
        self.db_path = make_temp_db()
        cal_svc.init_calendar_db(self.db_path)

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_calendar_events_table_created(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='calendar_events'")
        self.assertIsNotNone(c.fetchone())
        conn.close()

    def test_no_duplicate_on_empty_db(self):
        result = cal_svc.is_duplicate_calendar_event(
            assignee="Alice", task="Write report", deadline="2026-12-01",
            meeting="Sprint Meeting", db_path=self.db_path
        )
        self.assertIsNone(result)

    def test_duplicate_detected_after_save(self):
        cal_svc.save_calendar_event_record(
            google_event_id="evt_abc123",
            assignee="Alice",
            email="alice@example.com",
            task="Write report",
            deadline="2026-12-01",
            meeting="Sprint Meeting",
            db_path=self.db_path
        )
        result = cal_svc.is_duplicate_calendar_event(
            assignee="Alice", task="Write report", deadline="2026-12-01",
            meeting="Sprint Meeting", db_path=self.db_path
        )
        self.assertEqual(result, "evt_abc123")

    def test_different_assignee_not_duplicate(self):
        cal_svc.save_calendar_event_record(
            google_event_id="evt_abc123",
            assignee="Alice",
            email="alice@example.com",
            task="Write report",
            deadline="2026-12-01",
            meeting="Sprint Meeting",
            db_path=self.db_path
        )
        result = cal_svc.is_duplicate_calendar_event(
            assignee="Bob", task="Write report", deadline="2026-12-01",
            meeting="Sprint Meeting", db_path=self.db_path
        )
        self.assertIsNone(result)

    def test_existing_real_event_is_detected_as_duplicate(self):
        """A real created event (status='CREATED') is detected as a duplicate."""
        cal_svc.save_calendar_event_record(
            google_event_id="real_evt_999",
            assignee="Eve",
            email="eve@example.com",
            task="Refactor codebase",
            deadline="2026-12-15",
            meeting="Sprint Meeting",
            status="CREATED",
            db_path=self.db_path
        )
        result = cal_svc.is_duplicate_calendar_event(
            assignee="Eve", task="Refactor codebase", deadline="2026-12-15",
            meeting="Sprint Meeting", db_path=self.db_path
        )
        self.assertEqual(result, "real_evt_999")

    def test_simulated_event_is_not_detected_as_duplicate(self):
        """Simulated events (status='SIMULATED' or 'simulated_cal_*') are ignored."""
        cal_svc.save_calendar_event_record(
            google_event_id="simulated_cal_123456",
            assignee="Frank",
            email="frank@example.com",
            task="Optimize queries",
            deadline="2026-12-18",
            meeting="Sprint Meeting",
            status="SIMULATED",
            db_path=self.db_path
        )
        result = cal_svc.is_duplicate_calendar_event(
            assignee="Frank", task="Optimize queries", deadline="2026-12-18",
            meeting="Sprint Meeting", db_path=self.db_path
        )
        self.assertIsNone(result)

    @patch("google_calendar_service.get_calendar_service")
    def test_simulated_record_does_not_block_subsequent_real_event_creation(self, mock_get_svc):
        """A previous simulated record in DB does NOT block creating a real Google Calendar event."""
        # 1. Create simulated event first
        res1 = cal_svc.create_calendar_event(
            task="Build feature", assignee="Alice", deadline="2026-12-25",
            meeting="Sprint Meeting", force_simulate=True, db_path=self.db_path
        )
        self.assertEqual(res1["status"], "SIMULATED")

        # 2. Mock Google Calendar service for a real event creation
        mock_svc = MagicMock()
        mock_events = MagicMock()
        mock_insert = MagicMock()
        mock_insert.execute.return_value = {
            "id": "real_google_evt_777",
            "htmlLink": "https://calendar.google.com/event?eid=real_777"
        }
        mock_events.insert.return_value = mock_insert
        mock_svc.events.return_value = mock_events
        mock_get_svc.return_value = (mock_svc, "")

        # 3. Create real event (force_simulate=False)
        res2 = cal_svc.create_calendar_event(
            task="Build feature", assignee="Alice", deadline="2026-12-25",
            meeting="Sprint Meeting", force_simulate=False, db_path=self.db_path
        )
        self.assertEqual(res2["status"], "SUCCESS")
        self.assertEqual(res2["event_id"], "real_google_evt_777")
        self.assertNotEqual(res2["status"], "SKIPPED")

    def test_existing_duplicate_detection_for_different_meetings_and_tasks(self):
        """Verify duplicate detection differentiates by meeting and task."""
        cal_svc.save_calendar_event_record(
            google_event_id="real_evt_arch_001",
            assignee="Bob",
            email="bob@example.com",
            task="Deploy container",
            deadline="2026-12-30",
            meeting="Meeting Alpha",
            status="CREATED",
            db_path=self.db_path
        )
        # Same task, same assignee, same deadline, DIFFERENT meeting -> NOT duplicate
        res_diff_meeting = cal_svc.is_duplicate_calendar_event(
            assignee="Bob", task="Deploy container", deadline="2026-12-30",
            meeting="Meeting Beta", db_path=self.db_path
        )
        self.assertIsNone(res_diff_meeting)

        # Different task, same meeting -> NOT duplicate
        res_diff_task = cal_svc.is_duplicate_calendar_event(
            assignee="Bob", task="Build container", deadline="2026-12-30",
            meeting="Meeting Alpha", db_path=self.db_path
        )
        self.assertIsNone(res_diff_task)

        # Exact match -> IS duplicate
        res_match = cal_svc.is_duplicate_calendar_event(
            assignee="Bob", task="Deploy container", deadline="2026-12-30",
            meeting="Meeting Alpha", db_path=self.db_path
        )
        self.assertEqual(res_match, "real_evt_arch_001")

    def test_reminders_table_gets_google_event_id_column(self):
        """init_calendar_db should add google_event_id to existing reminders table."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                reminder_id INTEGER PRIMARY KEY AUTOINCREMENT,
                assignee TEXT, email TEXT, task TEXT, deadline TEXT,
                status TEXT DEFAULT 'PENDING'
            )
        """)
        conn.commit()
        conn.close()
        # Re-run init — it should add the column
        cal_svc.init_calendar_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("PRAGMA table_info(reminders)")
        columns = [row[1] for row in c.fetchall()]
        conn.close()
        self.assertIn("google_event_id", columns)


# =============================================================================
# 4. create_calendar_event — SIMULATED MODE (CALENDAR_NOTIFICATIONS_ENABLED=false)
# =============================================================================
class TestCreateCalendarEventSimulated(unittest.TestCase):
    """Tests that run with CALENDAR_NOTIFICATIONS_ENABLED=false (default)."""

    def setUp(self):
        self.db_path = make_temp_db()
        os.environ["CALENDAR_NOTIFICATIONS_ENABLED"] = "false"

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_skip_task_without_deadline(self):
        result = cal_svc.create_calendar_event(
            task="Review codebase", assignee="Alice",
            deadline=None, db_path=self.db_path
        )
        self.assertEqual(result["status"], "SKIPPED")
        self.assertIsNone(result["event_id"])

    def test_skip_task_with_empty_deadline(self):
        result = cal_svc.create_calendar_event(
            task="Review codebase", assignee="Alice",
            deadline="", db_path=self.db_path
        )
        self.assertEqual(result["status"], "SKIPPED")

    def test_skip_task_with_invalid_deadline_format(self):
        result = cal_svc.create_calendar_event(
            task="Review codebase", assignee="Alice",
            deadline="next friday", db_path=self.db_path
        )
        self.assertEqual(result["status"], "SKIPPED")

    def test_simulated_mode_returns_simulated_status(self):
        result = cal_svc.create_calendar_event(
            task="Fix login bug", assignee="Bob",
            deadline="2026-12-15", db_path=self.db_path
        )
        self.assertEqual(result["status"], "SIMULATED")
        self.assertIsNotNone(result["event_id"])
        self.assertTrue(result["event_id"].startswith("simulated_cal_"))

    def test_simulated_mode_returns_calendar_link(self):
        result = cal_svc.create_calendar_event(
            task="Fix login bug", assignee="Bob",
            deadline="2026-12-15", db_path=self.db_path
        )
        self.assertIn("calendar.google.com", result.get("html_link", ""))

    def test_simulated_events_not_treated_as_duplicate_in_subsequent_calls(self):
        """Simulated events do not block subsequent calls as duplicates."""
        result1 = cal_svc.create_calendar_event(
            task="Write tests", assignee="Carol",
            deadline="2026-12-20", db_path=self.db_path
        )
        self.assertEqual(result1["status"], "SIMULATED")

        result2 = cal_svc.create_calendar_event(
            task="Write tests", assignee="Carol",
            deadline="2026-12-20", db_path=self.db_path
        )
        self.assertEqual(result2["status"], "SIMULATED")

    def test_force_simulate_true_overrides_env(self):
        """force_simulate=True always returns SIMULATED even if env flag is true."""
        os.environ["CALENDAR_NOTIFICATIONS_ENABLED"] = "true"
        result = cal_svc.create_calendar_event(
            task="Deploy to prod", assignee="Dave",
            deadline="2026-12-10", force_simulate=True, db_path=self.db_path
        )
        self.assertEqual(result["status"], "SIMULATED")
        os.environ["CALENDAR_NOTIFICATIONS_ENABLED"] = "false"


# =============================================================================
# 5. create_calendar_event — REAL API CALLS (mocked)
# =============================================================================
class TestCreateCalendarEventMockedAPI(unittest.TestCase):
    """Tests using mocked Google Calendar API service."""

    def setUp(self):
        self.db_path = make_temp_db()
        os.environ["CALENDAR_NOTIFICATIONS_ENABLED"] = "true"

    def tearDown(self):
        os.environ["CALENDAR_NOTIFICATIONS_ENABLED"] = "false"
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _make_mock_service(self, event_id="evt_real_001", html_link="https://calendar.google.com/event?eid=evt_real_001"):
        """Build a mock Google Calendar service that returns a successful insert."""
        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_insert = MagicMock()
        mock_insert.execute.return_value = {"id": event_id, "htmlLink": html_link}
        mock_events.insert.return_value = mock_insert
        mock_service.events.return_value = mock_events
        return mock_service

    @patch.dict(os.environ, {"CALENDAR_NOTIFICATIONS_ENABLED": "true"})
    @patch("google_calendar_service.get_calendar_service")
    def test_successful_event_creation(self, mock_get_service):
        """Mocked API returns SUCCESS with correct event_id."""
        mock_get_service.return_value = (self._make_mock_service(), "ready")
        result = cal_svc.create_calendar_event(
            task="Deploy new microservice", assignee="Eve",
            deadline="2026-12-18", task_size="MEDIUM",
            meeting="Sprint 42 Review", db_path=self.db_path
        )
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["event_id"], "evt_real_001")
        self.assertIn("calendar.google.com", result["html_link"])

    @patch.dict(os.environ, {"CALENDAR_NOTIFICATIONS_ENABLED": "true"})
    @patch("google_calendar_service.get_calendar_service")
    def test_event_id_saved_to_db_after_success(self, mock_get_service):
        """After SUCCESS, the event_id must be stored in SQLite."""
        mock_get_service.return_value = (self._make_mock_service("evt_db_save"), "ready")
        cal_svc.create_calendar_event(
            task="Database migration", assignee="Frank",
            deadline="2026-12-22", meeting="Sprint Review",
            db_path=self.db_path
        )
        existing = cal_svc.is_duplicate_calendar_event(
            assignee="Frank", task="Database migration",
            deadline="2026-12-22", meeting="Sprint Review",
            db_path=self.db_path
        )
        self.assertEqual(existing, "evt_db_save")

    @patch.dict(os.environ, {"CALENDAR_NOTIFICATIONS_ENABLED": "true"})
    @patch("google_calendar_service.get_calendar_service")
    def test_http_401_returns_failed(self, mock_get_service):
        """HTTP 401 from Calendar API → FAILED status."""
        from googleapiclient.errors import HttpError
        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 401
        http_error = HttpError(resp=mock_resp, content=b"Unauthorized")
        http_error._get_reason = lambda: "Unauthorized"
        mock_events.insert.return_value.execute.side_effect = http_error
        mock_service.events.return_value = mock_events
        mock_get_service.return_value = (mock_service, "ready")

        result = cal_svc.create_calendar_event(
            task="Plan sprint", assignee="Grace",
            deadline="2026-12-30", db_path=self.db_path
        )
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("401", result.get("error", "") + result.get("message", ""))

    @patch.dict(os.environ, {"CALENDAR_NOTIFICATIONS_ENABLED": "true"})
    @patch("google_calendar_service.get_calendar_service")
    def test_http_403_returns_failed(self, mock_get_service):
        """HTTP 403 (insufficient permission) → FAILED status."""
        from googleapiclient.errors import HttpError
        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 403
        http_error = HttpError(resp=mock_resp, content=b"Forbidden")
        http_error._get_reason = lambda: "Forbidden"
        mock_events.insert.return_value.execute.side_effect = http_error
        mock_service.events.return_value = mock_events
        mock_get_service.return_value = (mock_service, "ready")

        result = cal_svc.create_calendar_event(
            task="Quarterly review", assignee="Hank",
            deadline="2026-12-28", db_path=self.db_path
        )
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("403", result.get("error", "") + result.get("message", ""))

    @patch.dict(os.environ, {"CALENDAR_NOTIFICATIONS_ENABLED": "true"})
    @patch("google_calendar_service.get_calendar_service")
    def test_unexpected_exception_returns_failed(self, mock_get_service):
        """RuntimeError from API call → FAILED, not exception propagation."""
        mock_service = MagicMock()
        mock_service.events.return_value.insert.return_value.execute.side_effect = RuntimeError("Connection reset")
        mock_get_service.return_value = (mock_service, "ready")

        result = cal_svc.create_calendar_event(
            task="Investigate latency", assignee="Iris",
            deadline="2026-12-25", db_path=self.db_path
        )
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("Connection reset", result.get("error", ""))

    @patch.dict(os.environ, {"CALENDAR_NOTIFICATIONS_ENABLED": "true"})
    @patch("google_calendar_service.get_calendar_service")
    def test_service_unavailable_returns_failed(self, mock_get_service):
        """If get_calendar_service returns None, result must be FAILED."""
        mock_get_service.return_value = (None, "Token missing — run authorize_calendar.py")

        result = cal_svc.create_calendar_event(
            task="Security audit", assignee="Jack",
            deadline="2026-12-12", db_path=self.db_path
        )
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("Token missing", result.get("error", "") + result.get("message", ""))


# =============================================================================
# 6. REMINDER OVERRIDE LOGIC
# =============================================================================
class TestReminderOverrides(unittest.TestCase):
    """Verify correct reminder minutes for LARGE vs SMALL/MEDIUM tasks."""

    def setUp(self):
        self.db_path = make_temp_db()

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _get_reminder_minutes(self, task_size):
        """Helper: mock API and capture the event body that would be sent."""
        captured = {}

        def mock_insert(**kwargs):
            captured["body"] = kwargs.get("body", {})
            mock_exec = MagicMock()
            mock_exec.execute.return_value = {"id": "evt_test", "htmlLink": "https://cal"}
            return mock_exec

        with patch("google_calendar_service.get_calendar_service") as mock_svc:
            mock_service = MagicMock()
            mock_service.events.return_value.insert.side_effect = mock_insert
            mock_svc.return_value = (mock_service, "ready")

            with patch.dict(os.environ, {"CALENDAR_NOTIFICATIONS_ENABLED": "true"}):
                cal_svc.create_calendar_event(
                    task=f"Task for {task_size}", assignee="Tester",
                    deadline="2026-12-20", task_size=task_size,
                    db_path=self.db_path
                )

        overrides = captured.get("body", {}).get("reminders", {}).get("overrides", [])
        return [r["minutes"] for r in overrides]

    def test_large_task_has_48h_reminder(self):
        minutes = self._get_reminder_minutes("LARGE")
        self.assertIn(48 * 60, minutes)

    def test_large_task_has_24h_reminder(self):
        minutes = self._get_reminder_minutes("LARGE")
        self.assertIn(24 * 60, minutes)

    def test_medium_task_has_24h_reminder(self):
        minutes = self._get_reminder_minutes("MEDIUM")
        self.assertIn(24 * 60, minutes)

    def test_medium_task_has_9h_reminder(self):
        minutes = self._get_reminder_minutes("MEDIUM")
        self.assertIn(9 * 60, minutes)

    def test_medium_task_does_not_have_48h_reminder(self):
        minutes = self._get_reminder_minutes("MEDIUM")
        self.assertNotIn(48 * 60, minutes)

    def test_small_task_has_24h_reminder(self):
        minutes = self._get_reminder_minutes("SMALL")
        self.assertIn(24 * 60, minutes)


# =============================================================================
# 7. ALL-DAY EVENT DATE RANGE
# =============================================================================
class TestAllDayEventDates(unittest.TestCase):
    """Verify RFC 5545 exclusive end date for all-day events."""

    def setUp(self):
        self.db_path = make_temp_db()

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_end_date_is_deadline_plus_one_day(self):
        captured = {}

        def mock_insert(**kwargs):
            captured["body"] = kwargs.get("body", {})
            mock_exec = MagicMock()
            mock_exec.execute.return_value = {"id": "evt_date_test", "htmlLink": "https://cal"}
            return mock_exec

        with patch("google_calendar_service.get_calendar_service") as mock_svc:
            mock_service = MagicMock()
            mock_service.events.return_value.insert.side_effect = mock_insert
            mock_svc.return_value = (mock_service, "ready")

            with patch.dict(os.environ, {"CALENDAR_NOTIFICATIONS_ENABLED": "true"}):
                cal_svc.create_calendar_event(
                    task="End date test", assignee="Tester",
                    deadline="2026-11-10", db_path=self.db_path
                )

        body = captured.get("body", {})
        self.assertEqual(body.get("start", {}).get("date"), "2026-11-10")
        self.assertEqual(body.get("end", {}).get("date"), "2026-11-11")


# =============================================================================
# 8. ATTENDEE HANDLING
# =============================================================================
class TestAttendeeHandling(unittest.TestCase):
    """Verify attendees are added only for real email addresses."""

    def setUp(self):
        self.db_path = make_temp_db()

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _capture_event_body(self, assignee_name):
        captured = {}

        def mock_insert(**kwargs):
            captured["body"] = kwargs.get("body", {})
            mock_exec = MagicMock()
            mock_exec.execute.return_value = {"id": "evt_att_test", "htmlLink": "https://cal"}
            return mock_exec

        with patch("google_calendar_service.get_calendar_service") as mock_svc:
            mock_service = MagicMock()
            mock_service.events.return_value.insert.side_effect = mock_insert
            mock_svc.return_value = (mock_service, "ready")

            with patch.dict(os.environ, {"CALENDAR_NOTIFICATIONS_ENABLED": "true"}):
                cal_svc.create_calendar_event(
                    task="Attendee test task", assignee=assignee_name,
                    deadline="2026-12-01", db_path=self.db_path
                )
        return captured.get("body", {})

    def test_known_assignee_has_attendee(self):
        """A known team member (Megashree) should appear as attendee."""
        body = self._capture_event_body("Megashree")
        attendees = body.get("attendees", [])
        # Known team member — attendees list should not be empty
        emails = [a.get("email", "") for a in attendees]
        # megashree999@gmail.com is a valid email
        self.assertTrue(any("@" in e and "@company.com" not in e for e in emails),
                        f"Expected valid real email in attendees, got: {emails}")

    def test_unknown_assignee_no_attendee_or_company_email(self):
        """Assignee with @company.com placeholder must not receive invites."""
        body = self._capture_event_body("Arun")
        attendees = body.get("attendees", [])
        emails = [a.get("email", "") for a in attendees]
        for email in emails:
            self.assertNotIn("@company.com", email,
                             f"Placeholder company email should not be added as attendee: {email}")


# =============================================================================
# 9. BATCH PROCESSING — create_calendar_events_for_action_items
# =============================================================================
class TestBatchActionItems(unittest.TestCase):
    """Verify batch processing handles mixed valid/invalid action items."""

    def setUp(self):
        self.db_path = make_temp_db()
        os.environ["CALENDAR_NOTIFICATIONS_ENABLED"] = "false"  # simulated

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_items_without_deadline_are_skipped(self):
        items = [
            {"task": "Task A", "assignee": "Alice", "deadline": "2026-12-10"},
            {"task": "Task B", "assignee": "Bob",   "deadline": None},
            {"task": "Task C", "assignee": "Carol", "deadline": ""},
        ]
        results = cal_svc.create_calendar_events_for_action_items(
            items, meeting_title="Sprint 1", db_path=self.db_path
        )
        statuses = [r["status"] for r in results]
        self.assertEqual(statuses.count("SIMULATED"), 1)
        self.assertEqual(statuses.count("SKIPPED"), 2)

    def test_empty_list_returns_empty(self):
        results = cal_svc.create_calendar_events_for_action_items(
            [], meeting_title="Empty Meeting", db_path=self.db_path
        )
        self.assertEqual(results, [])

    def test_items_without_task_or_assignee_skipped(self):
        items = [
            {"task": "", "assignee": "Alice", "deadline": "2026-12-10"},
            {"task": "Task B", "assignee": None, "deadline": "2026-12-11"},
        ]
        results = cal_svc.create_calendar_events_for_action_items(
            items, meeting_title="Test Meeting", db_path=self.db_path
        )
        # Items missing task or assignee are silently skipped (not appended)
        self.assertEqual(len(results), 0)

    def test_pydantic_model_objects_supported(self):
        """Action items can be Pydantic-style objects (not just dicts)."""
        class FakeItem:
            def __init__(self, task, assignee, deadline, task_size=None, context=None):
                self.task = task
                self.assignee = assignee
                self.deadline = deadline
                self.task_size = task_size
                self.context = context

        items = [FakeItem("Deploy API", "Dave", "2026-12-15", "MEDIUM")]
        results = cal_svc.create_calendar_events_for_action_items(
            items, meeting_title="API Sprint", db_path=self.db_path
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "SIMULATED")


# =============================================================================
# 10. LANGGRAPH NODE — create_calendar_events (from graph.py)
# =============================================================================
class TestLangGraphCalendarNode(unittest.TestCase):
    """Tests for the create_calendar_events LangGraph node."""

    def setUp(self):
        self.db_path = make_temp_db()
        os.environ["CALENDAR_NOTIFICATIONS_ENABLED"] = "false"

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _build_state(self, action_items, transcript="Sprint Meeting\nAlice: Let's ship it.", meeting_title=None, meeting_id=None):
        from graph import MeetingExtraction, ActionItem
        analysis = MeetingExtraction(
            summary="Test meeting summary",
            decisions=["Ship the feature"],
            action_items=[
                ActionItem(**item) if isinstance(item, dict) else item
                for item in action_items
            ],
            jira_tasks=[],
            github_issues=[]
        )
        return {
            "transcript": transcript,
            "analysis": analysis,
            "ticket_result": [],
            "email_notifications": [],
            "calendar_events": [],
            "meeting_id": meeting_id,
            "meeting_title": meeting_title,
            "user_query": None,
            "retrieved_context": None,
            "answer": None,
            "router_output": None,
            "determined_actions": ["create_calendar_events"],
        }

    def test_node_returns_calendar_events_key(self):
        from graph import create_calendar_events as cal_node
        state = self._build_state([
            {"task": "Review PR node key test", "assignee": "Alice", "deadline": "2026-12-05"}
        ])
        result = cal_node(state)
        self.assertIn("calendar_events", result)

    def test_node_produces_list(self):
        from graph import create_calendar_events as cal_node
        state = self._build_state([
            {"task": "Review PR produces list test", "assignee": "Alice", "deadline": "2026-12-05"}
        ])
        result = cal_node(state)
        self.assertIsInstance(result["calendar_events"], list)

    def test_node_skips_item_without_deadline(self):
        from graph import create_calendar_events as cal_node
        state = self._build_state([
            {"task": "Write report", "assignee": "Bob", "deadline": None}
        ])
        result = cal_node(state)
        statuses = [r.get("status") for r in result["calendar_events"]]
        self.assertIn("SKIPPED", statuses)

    def test_node_does_not_crash_on_empty_analysis(self):
        from graph import create_calendar_events as cal_node
        state = {
            "transcript": "Meeting title\nPresenter: OK",
            "analysis": None,
            "ticket_result": [],
            "email_notifications": [],
            "calendar_events": [],
            "meeting_id": None,
            "user_query": None,
            "retrieved_context": None,
            "answer": None,
            "router_output": None,
            "determined_actions": [],
        }
        result = cal_node(state)
        # Should return {} gracefully
        self.assertEqual(result, {})

    @patch("graph.create_calendar_events_for_action_items")
    def test_node_survives_service_exception(self, mock_batch):
        """If batch processing raises, node returns FAILED record without crashing."""
        mock_batch.side_effect = RuntimeError("Mocked service crash")
        from graph import create_calendar_events as cal_node
        state = self._build_state([
            {"task": "Handle exception", "assignee": "Carol", "deadline": "2026-12-10"}
        ])
        result = cal_node(state)
        self.assertIn("calendar_events", result)
        statuses = [r.get("status") for r in result.get("calendar_events", [])]
        self.assertIn("FAILED", statuses)

    def test_node_returns_simulated_when_disabled(self):
        """CALENDAR_NOTIFICATIONS_ENABLED=false → node results are SIMULATED."""
        from graph import create_calendar_events as cal_node
        import time
        unique_task = f"Fix bug unique {int(time.time() * 1000)}"
        state = self._build_state([
            {"task": unique_task, "assignee": "Alice", "deadline": "2026-12-08"}
        ])
        result = cal_node(state)
        statuses = [r.get("status") for r in result["calendar_events"]]
        self.assertIn("SIMULATED", statuses)

    @patch("graph.get_vector_store")
    def test_caller_supplied_meeting_title_in_index_transcript(self, mock_vs):
        """Caller supplies meeting_title -> index_transcript preserves it."""
        mock_vs.return_value.get.return_value = {"ids": []}
        from graph import index_transcript
        import hashlib
        transcript = "Meeting body discussing sprint goals."
        expected_id = hashlib.sha256(transcript.encode("utf-8")).hexdigest()[:12]
        res = index_transcript({
            "transcript": transcript,
            "meeting_title": "Custom Sprint Title"
        })
        self.assertEqual(res.get("meeting_id"), expected_id)
        self.assertEqual(res.get("meeting_title"), "Custom Sprint Title")

    @patch("graph.get_vector_store")
    def test_missing_meeting_title_fallback_in_index_transcript(self, mock_vs):
        """Missing meeting_title -> index_transcript falls back to 'Meeting {meeting_id}'."""
        mock_vs.return_value.get.return_value = {"ids": []}
        from graph import index_transcript
        import hashlib
        transcript = "Transcript without explicit caller title."
        expected_id = hashlib.sha256(transcript.encode("utf-8")).hexdigest()[:12]
        res = index_transcript({
            "transcript": transcript,
            "meeting_title": None
        })
        self.assertEqual(res.get("meeting_id"), expected_id)
        self.assertEqual(res.get("meeting_title"), f"Meeting {expected_id}")

    @patch("graph.get_vector_store")
    def test_existing_meeting_id_generation_preservation(self, mock_vs):
        """Preserve existing 12-char SHA256 meeting_id generation."""
        mock_vs.return_value.get.return_value = {"ids": []}
        from graph import index_transcript
        import hashlib
        text = "Standup update line 1\nline 2"
        expected_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        res = index_transcript({"transcript": text})
        self.assertEqual(res.get("meeting_id"), expected_hash)
        self.assertEqual(len(res.get("meeting_id")), 12)

    @patch("graph.create_calendar_events_for_action_items")
    def test_calendar_node_receives_caller_meeting_title(self, mock_batch):
        """Calendar node passes caller-supplied meeting_title to create_calendar_events_for_action_items."""
        mock_batch.return_value = [{"status": "SIMULATED", "assignee": "Alice", "deadline": "2026-12-15"}]
        from graph import create_calendar_events as cal_node
        state = self._build_state(
            [{"task": "Design arch", "assignee": "Alice", "deadline": "2026-12-15"}],
            meeting_title="Platform Architecture Review",
            meeting_id="m12345678901"
        )
        cal_node(state)
        mock_batch.assert_called_once()
        call_kwargs = mock_batch.call_args.kwargs
        self.assertEqual(call_kwargs.get("meeting_title"), "Platform Architecture Review")

    @patch("graph.create_calendar_events_for_action_items")
    def test_calendar_node_fallback_when_title_missing(self, mock_batch):
        """When meeting_title is missing, Calendar node uses fallback 'Meeting {meeting_id}'."""
        mock_batch.return_value = [{"status": "SIMULATED", "assignee": "Alice", "deadline": "2026-12-15"}]
        from graph import create_calendar_events as cal_node
        state = self._build_state(
            [{"task": "Design arch", "assignee": "Alice", "deadline": "2026-12-15"}],
            meeting_title=None,
            meeting_id="m98765432100"
        )
        cal_node(state)
        mock_batch.assert_called_once()
        call_kwargs = mock_batch.call_args.kwargs
        self.assertEqual(call_kwargs.get("meeting_title"), "Meeting m98765432100")


# =============================================================================
# 11. DETERMINE_ACTIONS — Calendar triggered only when deadlines exist
# =============================================================================
class TestDetermineActionsCalendar(unittest.TestCase):
    """Verify determine_actions appends create_calendar_events correctly."""

    def _build_state(self, action_items, jira_tasks=None, github_issues=None):
        from graph import MeetingExtraction, ActionItem
        ai_list = [ActionItem(**i) for i in action_items]
        analysis = MeetingExtraction(
            summary="Test",
            decisions=[],
            action_items=ai_list,
            jira_tasks=jira_tasks or [],
            github_issues=github_issues or []
        )
        return {
            "analysis": analysis,
            "transcript": None, "meeting_id": None, "user_query": None,
            "retrieved_context": None, "answer": None, "ticket_result": [],
            "email_notifications": [], "calendar_events": [],
            "router_output": None, "determined_actions": None,
        }

    def test_calendar_action_added_when_deadline_present(self):
        from graph import determine_actions
        state = self._build_state([
            {"task": "Deploy API", "assignee": "Alice", "deadline": "2026-12-10"}
        ])
        result = determine_actions(state)
        self.assertIn("create_calendar_events", result["determined_actions"])

    def test_calendar_action_not_added_when_no_deadlines(self):
        from graph import determine_actions
        state = self._build_state([
            {"task": "Review docs", "assignee": "Bob", "deadline": None}
        ])
        result = determine_actions(state)
        self.assertNotIn("create_calendar_events", result["determined_actions"])

    def test_email_action_always_added_for_action_items(self):
        from graph import determine_actions
        state = self._build_state([
            {"task": "Review docs", "assignee": "Bob", "deadline": None}
        ])
        result = determine_actions(state)
        self.assertIn("send_email_notifications", result["determined_actions"])


if __name__ == "__main__":
    # UTF-8 output for Windows terminals
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(__import__("__main__"))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
