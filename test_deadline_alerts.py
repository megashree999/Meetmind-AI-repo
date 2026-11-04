import os
import sys
import sqlite3
import datetime
import unittest
from unittest.mock import patch, MagicMock

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from reminder_service import init_reminders_db, schedule_reminders
from process_reminders import process_reminders as _orig_process_reminders, create_reminder_email_content, KOLKATA_TZ
from team_directory import get_assignee_email

def process_reminders(db_path="test_reminders_sandbox.db", current_time=None):
    """Wrapper ensuring time-gate allows upcoming reminder tests to run regardless of midnight clock hour."""
    if current_time is None:
        current_time = datetime.datetime.now(KOLKATA_TZ).replace(hour=10, minute=0, second=0)
    return _orig_process_reminders(db_path=db_path, current_time=current_time)

TEST_DB = "test_reminders_sandbox.db"

class TestDeadlineAlertsModule(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_reminders_db(TEST_DB)

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB):
            try:
                os.remove(TEST_DB)
            except Exception:
                pass

    def setUp(self):
        # Reset database table before each test
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("DELETE FROM reminders")
        c.execute("DELETE FROM calendar_events")
        conn.commit()
        conn.close()

        self.today = datetime.datetime.now().date()
        self.today_str = self.today.strftime("%Y-%m-%d")
        self.tomorrow_str = (self.today + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        self.two_days_later_str = (self.today + datetime.timedelta(days=2)).strftime("%Y-%m-%d")
        self.yesterday_str = (self.today - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        self.three_days_ago_str = (self.today - datetime.timedelta(days=3)).strftime("%Y-%m-%d")

    def _link_cal(self, google_event_id, assignee, task, deadline=None, meeting="Meeting", status="CREATED"):
        deadline = deadline or self.tomorrow_str
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT OR REPLACE INTO calendar_events (google_event_id, assignee, email, task, deadline, meeting, status, created_at)
            VALUES (?, ?, 'test@gmail.com', ?, ?, ?, ?, ?)
        ''', (google_event_id, assignee, task, deadline, meeting, status, datetime.datetime.now().isoformat()))
        conn.commit()
        conn.close()

    # =========================================================================
    # STEP 2: UPCOMING DEADLINE ALERTS CLASSIFICATION
    # =========================================================================

    def test_deadline_tomorrow_small_task(self):
        """Case 1a: Task deadline is tomorrow (SMALL/MEDIUM task) -> reminder_date is today, status PENDING."""
        action_items = [{
            "task": "Review API endpoints by tomorrow",
            "assignee": "Kashif",
            "deadline": self.tomorrow_str,
            "task_size": "MEDIUM"
        }]
        schedule_reminders(action_items, "Sprint Planning Meeting", db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT reminder_date, status, task_size FROM reminders WHERE assignee='Kashif'")
        row = c.fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], self.today_str, "Reminder date should be today for small/medium task due tomorrow")
        self.assertEqual(row[1], "PENDING", "Status should be PENDING")
        self.assertEqual(row[2], "MEDIUM")

    def test_deadline_tomorrow_large_task(self):
        """Case 1b: Task deadline is tomorrow (LARGE task) -> scheduled for today, status PENDING."""
        action_items = [{
            "task": "Complete core authentication architecture by tomorrow",
            "assignee": "Megashree",
            "deadline": self.tomorrow_str,
            "task_size": "LARGE"
        }]
        schedule_reminders(action_items, "Sprint Planning Meeting", db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT reminder_date, status, task_size FROM reminders WHERE assignee='Megashree'")
        row = c.fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], self.today_str, "LARGE task due tomorrow should alert today")
        self.assertEqual(row[1], "PENDING")
        self.assertEqual(row[2], "LARGE")

    def test_deadline_today(self):
        """Case 2: Task deadline is today -> scheduled for immediate same-day alert, status PENDING."""
        action_items = [{
            "task": "Fix critical production database bug today",
            "assignee": "Ali",
            "deadline": self.today_str,
            "task_size": "SMALL"
        }]
        schedule_reminders(action_items, "Emergency Sync Meeting", db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT reminder_date, status FROM reminders WHERE assignee='Ali'")
        row = c.fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], self.today_str, "Task due today must have reminder_date set to today")
        self.assertEqual(row[1], "PENDING", "Task due today must have status PENDING for immediate processing")

    def test_deadline_two_days_away_small_task(self):
        """Case 3a: Task deadline is two days away (SMALL/MEDIUM) -> reminder_date is tomorrow, status PENDING."""
        action_items = [{
            "task": "Update documentation by two days",
            "assignee": "Priya",
            "deadline": self.two_days_later_str,
            "task_size": "SMALL"
        }]
        schedule_reminders(action_items, "Doc Sync Meeting", db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT reminder_date, status FROM reminders WHERE assignee='Priya'")
        row = c.fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], self.tomorrow_str, "1 day before two days away is tomorrow")
        self.assertEqual(row[1], "PENDING")

    def test_deadline_two_days_away_large_task(self):
        """Case 3b: Task deadline is two days away (LARGE task) -> reminder_date is today, status PENDING."""
        action_items = [{
            "task": "Implement payment gateway end-to-end",
            "assignee": "Kashif",
            "deadline": self.two_days_later_str,
            "task_size": "LARGE"
        }]
        schedule_reminders(action_items, "Architecture Review", db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT reminder_date, status FROM reminders WHERE assignee='Kashif'")
        row = c.fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], self.today_str, "LARGE task 2 days before deadline should trigger today")
        self.assertEqual(row[1], "PENDING")

    def test_deadline_already_passed(self):
        """Case 4: Task deadline is already passed -> classified as OVERDUE."""
        action_items = [{
            "task": "Submit release notes",
            "assignee": "Ali",
            "deadline": self.yesterday_str,
            "task_size": "MEDIUM"
        }]
        schedule_reminders(action_items, "Past Meeting", db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT reminder_date, status FROM reminders WHERE assignee='Ali'")
        row = c.fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row[1], "OVERDUE", "Past deadline task must be classified as OVERDUE")

    # =========================================================================
    # STEP 3: OVERDUE ALERTS & EDGE CASES
    # =========================================================================

    @patch("process_reminders.get_gmail_service")
    def test_overdue_tasks_detected_correctly(self, mock_get_gmail):
        """Case 5: Overdue tasks are detected and processed by process_reminders."""
        mock_service = MagicMock()
        mock_messages = MagicMock()
        mock_send = MagicMock()
        mock_send.execute.return_value = {"id": "msg_overdue_123"}
        mock_messages.send.return_value = mock_send
        mock_service.users.return_value.messages.return_value = mock_messages
        mock_get_gmail.return_value = (mock_service, "OK")

        # Insert an overdue task directly into database
        self._link_cal("cal_case_5", "Kashif", "Finish overdue payment API", self.three_days_ago_str, "Past Meeting")
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, google_event_id)
            VALUES ('Kashif', 'kashifahamad2003@gmail.com', 'Finish overdue payment API', ?, 'MEDIUM', ?, 'Past Meeting', 'OVERDUE', '2026-10-01T00:00:00', 'cal_case_5')
        ''', (self.three_days_ago_str, self.today_str))
        conn.commit()
        conn.close()

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}):
            summary = process_reminders(db_path=TEST_DB)

        self.assertEqual(summary["sent"], 1)
        # Verify status transitioned to SENT_OVERDUE
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT status, sent_at FROM reminders WHERE assignee='Kashif'")
        row = c.fetchone()
        conn.close()
        self.assertEqual(row[0], "SENT_OVERDUE")
        self.assertIsNotNone(row[1])

    @patch("process_reminders.get_gmail_service")
    def test_completed_tasks_do_not_receive_overdue_alerts(self, mock_get_gmail):
        """Case 6: Completed tasks (status='COMPLETED') do NOT receive overdue alerts."""
        mock_service = MagicMock()
        mock_get_gmail.return_value = (mock_service, "OK")

        # Insert a completed task whose deadline has passed
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at)
            VALUES ('Ali', 'amohammedali2005@gmail.com', 'Migrate user table', ?, 'MEDIUM', ?, 'Old Meeting', 'COMPLETED', '2026-10-01T00:00:00')
        ''', (self.three_days_ago_str, self.today_str))
        conn.commit()
        conn.close()

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}):
            summary = process_reminders(db_path=TEST_DB)

        self.assertEqual(summary["processed"], 0, "Completed tasks must be completely ignored")
        self.assertEqual(summary["sent"], 0)
        mock_service.users.return_value.messages.return_value.send.assert_not_called()

    def test_tasks_without_deadlines_skipped(self):
        """Case 7: Tasks without deadlines are skipped from reminder scheduling."""
        action_items = [
            {"task": "Prepare report", "assignee": "Priya", "deadline": None, "task_size": "SMALL"},
            {"task": "Prepare slides", "assignee": "Priya", "deadline": "", "task_size": "SMALL"}
        ]
        schedule_reminders(action_items, "General Meeting", db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM reminders")
        count = c.fetchone()[0]
        conn.close()

        self.assertEqual(count, 0, "Items without deadline must not be scheduled")

    def test_tasks_without_assignee_or_task_skipped(self):
        """Case 8: Tasks without assignee or task description are skipped."""
        action_items = [
            {"task": "", "assignee": "Ali", "deadline": self.tomorrow_str},
            {"task": "Valid task", "assignee": None, "deadline": self.tomorrow_str}
        ]
        schedule_reminders(action_items, "General Meeting", db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM reminders")
        count = c.fetchone()[0]
        conn.close()

        self.assertEqual(count, 0)

    @patch("process_reminders.get_gmail_service")
    def test_previously_sent_reminders_not_duplicated(self, mock_get_gmail):
        """Case 9: Previously sent reminders (status='SENT' or 'SENT_OVERDUE') are not sent again."""
        mock_service = MagicMock()
        mock_get_gmail.return_value = (mock_service, "OK")

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, sent_at)
            VALUES ('Megashree', 'megashree999@gmail.com', 'Deploy staging', ?, 'MEDIUM', ?, 'Meeting', 'SENT', '2026-10-01T00:00:00', '2026-10-02T09:00:00')
        ''', (self.tomorrow_str, self.today_str))
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, sent_at)
            VALUES ('Ali', 'amohammedali2005@gmail.com', 'Old task', ?, 'MEDIUM', ?, 'Meeting', 'SENT_OVERDUE', '2026-10-01T00:00:00', '2026-10-02T09:00:00')
        ''', (self.yesterday_str, self.today_str))
        conn.commit()
        conn.close()

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}):
            summary = process_reminders(db_path=TEST_DB)

        self.assertEqual(summary["processed"], 0, "Already sent reminders must not be processed again")
        mock_service.users.return_value.messages.return_value.send.assert_not_called()

    def test_duplicate_scheduling_prevented(self):
        """Case 10: Duplicate action items from the same meeting are not scheduled twice."""
        item = {
            "task": "Review authentication code by Friday",
            "assignee": "Kashif",
            "deadline": self.tomorrow_str,
            "task_size": "MEDIUM"
        }
        # Schedule first time
        schedule_reminders([item], "Sprint Review", db_path=TEST_DB)
        # Schedule second time (simulating repeated meeting extraction)
        schedule_reminders([item], "Sprint Review", db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM reminders WHERE task LIKE 'Review authentication%'")
        count = c.fetchone()[0]
        conn.close()

        self.assertEqual(count, 1, "Duplicate scheduling must be prevented")

    @patch("process_reminders.get_gmail_service")
    def test_failed_notifications_retried_safely(self, mock_get_gmail):
        """Case 11: Failed notifications (status='FAILED') can be retried safely on subsequent run."""
        mock_service = MagicMock()
        mock_messages = MagicMock()
        mock_send = MagicMock()
        mock_send.execute.return_value = {"id": "msg_retry_success"}
        mock_messages.send.return_value = mock_send
        mock_service.users.return_value.messages.return_value = mock_messages
        mock_get_gmail.return_value = (mock_service, "OK")

        # Insert a previously FAILED reminder scheduled for today
        self._link_cal("cal_case_11", "Rahul", "QA automated regression suite", self.tomorrow_str, "QA Standup")
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, google_event_id)
            VALUES ('Rahul', 'rahulns09876@gmail.com', 'QA automated regression suite', ?, 'MEDIUM', ?, 'QA Standup', 'FAILED', '2026-10-01T00:00:00', 'cal_case_11')
        ''', (self.tomorrow_str, self.today_str))
        conn.commit()
        conn.close()

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}):
            summary = process_reminders(db_path=TEST_DB)

        self.assertEqual(summary["sent"], 1, "Failed reminder should be picked up and sent on retry")

        # Verify status changed to SENT
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT status FROM reminders WHERE assignee='Rahul'")
        new_status = c.fetchone()[0]
        conn.close()
        self.assertEqual(new_status, "SENT")

    # =========================================================================
    # STEP 4: REMINDER SCHEDULING & WINDOWS TASK SCHEDULER
    # =========================================================================

    def test_process_reminders_identifies_needed_reminders(self):
        """Case 12: process_reminders correctly identifies both upcoming and overdue items needing processing."""
        self._link_cal("cal_case_12_a", "Megashree", "Upcoming Task", self.tomorrow_str, "Meeting A")
        self._link_cal("cal_case_12_b", "Ali", "Overdue Task", self.yesterday_str, "Meeting B")
        self._link_cal("cal_case_12_c", "Priya", "Future Task", self.two_days_later_str, "Meeting C")

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        # Item 1: Upcoming due tomorrow, reminder date today
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, google_event_id)
            VALUES ('Megashree', 'megashree999@gmail.com', 'Upcoming Task', ?, 'MEDIUM', ?, 'Meeting A', 'PENDING', '2026-10-01T00:00:00', 'cal_case_12_a')
        ''', (self.tomorrow_str, self.today_str))
        # Item 2: Overdue item (deadline 2 days ago)
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, google_event_id)
            VALUES ('Ali', 'amohammedali2005@gmail.com', 'Overdue Task', ?, 'MEDIUM', ?, 'Meeting B', 'OVERDUE', '2026-10-01T00:00:00', 'cal_case_12_b')
        ''', (self.yesterday_str, self.today_str))
        # Item 3: Future item (reminder date tomorrow) - should NOT be processed today
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, google_event_id)
            VALUES ('Priya', 'jeevadhardhinitv23@gmail.com', 'Future Task', ?, 'MEDIUM', ?, 'Meeting C', 'PENDING', '2026-10-01T00:00:00', 'cal_case_12_c')
        ''', (self.two_days_later_str, self.tomorrow_str))
        conn.commit()
        conn.close()

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "false"}):
            summary = process_reminders(db_path=TEST_DB)

        # Should process Item 1 and Item 2, but NOT Item 3
        self.assertEqual(summary["processed"], 2)

    # =========================================================================
    # STEP 5: EMAIL NOTIFICATIONS CONTENT & MOCKED GMAIL
    # =========================================================================

    def test_email_content_upcoming_vs_overdue(self):
        """Case 13: Upcoming and overdue email content have distinct subjects and bodies."""
        task = "Complete API documentation by Friday."
        deadline = "2026-11-10"
        meeting = "Sprint Retrospective"

        upcoming_sub, upcoming_body = create_reminder_email_content(
            "Kashif", task, deadline, meeting, is_overdue=False
        )
        overdue_sub, overdue_body = create_reminder_email_content(
            "Kashif", task, deadline, meeting, is_overdue=True
        )

        # Subjects must be distinct
        self.assertIn("Reminder", upcoming_sub)
        self.assertIn("Due Tomorrow", upcoming_sub)
        self.assertIn("Overdue Alert", overdue_sub)
        self.assertNotEqual(upcoming_sub, overdue_sub)

        # Bodies must be distinct
        self.assertIn("friendly reminder", upcoming_body.lower())
        self.assertIn("urgent reminder", overdue_body.lower())
        self.assertIn("overdue", overdue_body.lower())
        self.assertNotEqual(upcoming_body, overdue_body)

        # Temporal cleaning verification: "by Friday." should be cleaned from task description
        self.assertNotIn("by Friday", upcoming_body)
        self.assertNotIn("by Friday", overdue_body)
        self.assertIn("Complete API documentation", upcoming_body)

    @patch("process_reminders.get_gmail_service")
    def test_mocked_gmail_notification_dispatch(self, mock_get_gmail):
        """Case 14: Mocked Gmail receives correct recipient, subject, and encoded payload."""
        mock_service = MagicMock()
        mock_messages = MagicMock()
        mock_send = MagicMock()
        mock_send.execute.return_value = {"id": "mock_sent_msg_999"}
        mock_messages.send.return_value = mock_send
        mock_service.users.return_value.messages.return_value = mock_messages
        mock_get_gmail.return_value = (mock_service, "OK")

        self._link_cal("cal_case_14", "Kashif", "Refactor auth controller by Monday", self.tomorrow_str, "Eng Sync")
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, google_event_id)
            VALUES ('Kashif', 'kashifahamad2003@gmail.com', 'Refactor auth controller by Monday', ?, 'MEDIUM', ?, 'Eng Sync', 'PENDING', '2026-10-01T00:00:00', 'cal_case_14')
        ''', (self.tomorrow_str, self.today_str))
        conn.commit()
        conn.close()

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}):
            summary = process_reminders(db_path=TEST_DB)

        self.assertEqual(summary["sent"], 1)
        mock_messages.send.assert_called_once()
        call_kwargs = mock_messages.send.call_args[1]
        self.assertEqual(call_kwargs["userId"], "me")
        self.assertIn("raw", call_kwargs["body"])

    @patch("process_reminders.get_gmail_service")
    def test_failed_email_delivery_recorded_properly(self, mock_get_gmail):
        """Case 15: Failed email delivery (e.g. Gmail API error) records status FAILED in database."""
        mock_service = MagicMock()
        mock_messages = MagicMock()
        mock_send = MagicMock()
        mock_send.execute.side_effect = Exception("Gmail API 503 Backend Error")
        mock_messages.send.return_value = mock_send
        mock_service.users.return_value.messages.return_value = mock_messages
        mock_get_gmail.return_value = (mock_service, "OK")

        self._link_cal("cal_case_15", "Ali", "Execute DB migration", self.tomorrow_str, "DB Sync")
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, google_event_id)
            VALUES ('Ali', 'amohammedali2005@gmail.com', 'Execute DB migration', ?, 'MEDIUM', ?, 'DB Sync', 'PENDING', '2026-10-01T00:00:00', 'cal_case_15')
        ''', (self.tomorrow_str, self.today_str))
        conn.commit()
        conn.close()

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}):
            summary = process_reminders(db_path=TEST_DB)

        self.assertEqual(summary["failed"], 1)
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT status FROM reminders WHERE assignee='Ali'")
        status = c.fetchone()[0]
        conn.close()
        self.assertEqual(status, "FAILED", "Failed delivery must update status to FAILED")

    # =========================================================================
    # STEP 6: DATABASE STATUS TRANSITIONS
    # =========================================================================

    @patch("process_reminders.get_gmail_service")
    def test_status_transition_pending_to_sent(self, mock_get_gmail):
        """Case 16: Status transition PENDING -> SENT upon successful delivery."""
        mock_service = MagicMock()
        mock_send = MagicMock()
        mock_send.execute.return_value = {"id": "1"}
        mock_service.users.return_value.messages.return_value.send.return_value = mock_send
        mock_get_gmail.return_value = (mock_service, "OK")

        self._link_cal("cal_case_16", "Rahul", "Verify login", self.tomorrow_str, "QA Meeting")
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, google_event_id)
            VALUES ('Rahul', 'rahulns09876@gmail.com', 'Verify login', ?, 'MEDIUM', ?, 'QA Meeting', 'PENDING', '2026-10-01T00:00:00', 'cal_case_16')
        ''', (self.tomorrow_str, self.today_str))
        conn.commit()
        conn.close()

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}):
            process_reminders(db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT status, sent_at FROM reminders WHERE assignee='Rahul'")
        row = c.fetchone()
        conn.close()
        self.assertEqual(row[0], "SENT")
        self.assertIsNotNone(row[1])

    @patch("process_reminders.get_gmail_service")
    def test_status_transition_pending_to_failed(self, mock_get_gmail):
        """Case 17: Status transition PENDING -> FAILED upon network/API failure."""
        mock_service = MagicMock()
        mock_send = MagicMock()
        mock_send.execute.side_effect = RuntimeError("Network timeout")
        mock_service.users.return_value.messages.return_value.send.return_value = mock_send
        mock_get_gmail.return_value = (mock_service, "OK")

        self._link_cal("cal_case_17", "Rahul", "Verify login", self.tomorrow_str, "QA Meeting")
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, google_event_id)
            VALUES ('Rahul', 'rahulns09876@gmail.com', 'Verify login', ?, 'MEDIUM', ?, 'QA Meeting', 'PENDING', '2026-10-01T00:00:00', 'cal_case_17')
        ''', (self.tomorrow_str, self.today_str))
        conn.commit()
        conn.close()

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}):
            process_reminders(db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT status FROM reminders WHERE assignee='Rahul'")
        row = c.fetchone()
        conn.close()
        self.assertEqual(row[0], "FAILED")

    @patch("process_reminders.get_gmail_service")
    def test_status_transition_pending_to_skipped_invalid_email(self, mock_get_gmail):
        """Case 18: Status transition PENDING -> SKIPPED when recipient email is invalid or demo address."""
        mock_service = MagicMock()
        mock_get_gmail.return_value = (mock_service, "OK")

        self._link_cal("cal_case_18", "Arun", "Backend work", self.tomorrow_str, "Meeting")
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        # Arun has email 'arun@company.com' which is blocked as demo domain by is_valid_email
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, google_event_id)
            VALUES ('Arun', 'arun@company.com', 'Backend work', ?, 'MEDIUM', ?, 'Meeting', 'PENDING', '2026-10-01T00:00:00', 'cal_case_18')
        ''', (self.tomorrow_str, self.today_str))
        conn.commit()
        conn.close()

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}):
            summary = process_reminders(db_path=TEST_DB)

        self.assertEqual(summary["skipped"], 1)
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT status FROM reminders WHERE assignee='Arun'")
        row = c.fetchone()
        conn.close()
        self.assertEqual(row[0], "SKIPPED")

    @patch("process_reminders.get_gmail_service")
    def test_real_calendar_event_linked_and_gmail_id_recorded(self, mock_get_gmail):
        """Case 19: Real Calendar event ID is linked, email sent, and gmail_message_id recorded."""
        mock_service = MagicMock()
        mock_messages = MagicMock()
        mock_send = MagicMock()
        mock_send.execute.return_value = {"id": "gmail_msg_real_001"}
        mock_messages.send.return_value = mock_send
        mock_service.users.return_value.messages.return_value = mock_messages
        mock_get_gmail.return_value = (mock_service, "OK")

        self._link_cal("real_cal_event_xyz789", "Kashif", "Deploy production payment module", self.tomorrow_str, "Sprint Planning Meeting")
        # Schedule reminder with real Google Calendar event ID
        action_items = [{
            "task": "Deploy production payment module",
            "assignee": "Kashif",
            "deadline": self.tomorrow_str,
            "task_size": "MEDIUM",
            "google_event_id": "real_cal_event_xyz789"
        }]
        schedule_reminders(action_items, "Sprint Planning Meeting", db_path=TEST_DB)

        # Verify google_event_id linked in reminders table
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT google_event_id, status FROM reminders WHERE assignee='Kashif'")
        row = c.fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "real_cal_event_xyz789")

        # Process reminder
        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}):
            summary = process_reminders(db_path=TEST_DB)

        self.assertEqual(summary["sent"], 1)

        # Verify status is SENT and gmail_message_id is recorded
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT status, gmail_message_id FROM reminders WHERE assignee='Kashif'")
        row2 = c.fetchone()
        conn.close()
        self.assertEqual(row2[0], "SENT")
        self.assertEqual(row2[1], "gmail_msg_real_001")

    @patch("process_reminders.get_gmail_service")
    def test_simulated_calendar_event_not_scheduled_or_emailed(self, mock_get_gmail):
        """Case 20: Simulated Calendar events are never scheduled or emailed."""
        mock_service = MagicMock()
        mock_get_gmail.return_value = (mock_service, "OK")

        # Attempt to schedule with simulated calendar event ID
        action_items = [{
            "task": "Simulated sandbox test",
            "assignee": "Ali",
            "deadline": self.tomorrow_str,
            "task_size": "MEDIUM",
            "google_event_id": "simulated_cal_99999"
        }]
        schedule_reminders(action_items, "Simulated Meeting", db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM reminders WHERE task LIKE 'Simulated sandbox test%'")
        count = c.fetchone()[0]
        conn.close()
        self.assertEqual(count, 0, "Simulated calendar events must not be scheduled")

        # If a reminder row already had a simulated ID in DB, process_reminders must skip it
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, google_event_id)
            VALUES ('Ali', 'amohammedali2005@gmail.com', 'Existing simulated task', ?, 'MEDIUM', ?, 'Meeting', 'PENDING', '2026-10-01T00:00:00', 'simulated_cal_11111')
        ''', (self.tomorrow_str, self.today_str))
        conn.commit()
        conn.close()

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}):
            summary = process_reminders(db_path=TEST_DB)

        self.assertEqual(summary["sent"], 0, "Must not send email for simulated Calendar event")
        mock_service.users.return_value.messages.return_value.send.assert_not_called()

    @patch("process_reminders.get_gmail_service")
    def test_repeated_execution_every_15_minutes_no_duplicates(self, mock_get_gmail):
        """Case 21: Running process_reminders repeatedly (15-min scheduler) produces zero duplicates."""
        mock_service = MagicMock()
        mock_messages = MagicMock()
        mock_send = MagicMock()
        mock_send.execute.return_value = {"id": "msg_run1"}
        mock_messages.send.return_value = mock_send
        mock_service.users.return_value.messages.return_value = mock_messages
        mock_get_gmail.return_value = (mock_service, "OK")

        # Insert a pending reminder for today
        self._link_cal("real_cal_123", "Megashree", "Prepare quarterly report", self.tomorrow_str, "Q-Sync")
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, google_event_id)
            VALUES ('Megashree', 'megashree999@gmail.com', 'Prepare quarterly report', ?, 'MEDIUM', ?, 'Q-Sync', 'PENDING', '2026-10-01T00:00:00', 'real_cal_123')
        ''', (self.tomorrow_str, self.today_str))
        conn.commit()
        conn.close()

        # Run 1 (e.g. at 10:00 AM)
        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}):
            summary1 = process_reminders(db_path=TEST_DB)
        self.assertEqual(summary1["sent"], 1)

        # Run 2 (e.g. at 10:15 AM)
        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}):
            summary2 = process_reminders(db_path=TEST_DB)
        self.assertEqual(summary2["sent"], 0, "Second run must not re-send already sent reminder")
        self.assertEqual(summary2["processed"], 0)

        # Verify send was called exactly once across both runs
        mock_messages.send.assert_called_once()

    @patch("process_reminders.get_gmail_service")
    def test_recipient_email_retrieved_from_company_directory(self, mock_get_gmail):
        """Case 22: Recipient email is looked up directly from company_directory.db."""
        mock_service = MagicMock()
        mock_messages = MagicMock()
        mock_send = MagicMock()
        mock_send.execute.return_value = {"id": "msg_dir_lookup"}
        mock_messages.send.return_value = mock_send
        mock_service.users.return_value.messages.return_value = mock_messages
        mock_get_gmail.return_value = (mock_service, "OK")

        # Insert reminder with empty/placeholder email for known employee 'Megashree'
        self._link_cal("real_cal_456", "Megashree", "Review project metrics", self.tomorrow_str, "Metrics Sync")
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, google_event_id)
            VALUES ('Megashree', '', 'Review project metrics', ?, 'MEDIUM', ?, 'Metrics Sync', 'PENDING', '2026-10-01T00:00:00', 'real_cal_456')
        ''', (self.tomorrow_str, self.today_str))
        conn.commit()
        conn.close()

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}):
            summary = process_reminders(db_path=TEST_DB)

        self.assertEqual(summary["sent"], 1)
        # Verify the email was sent to Megashree's directory address
        call_kwargs = mock_messages.send.call_args[1]
        self.assertIn("raw", call_kwargs["body"])

    @patch("process_reminders.get_gmail_service")
    def test_completed_task_ignored_for_both_upcoming_and_overdue(self, mock_get_gmail):
        """Case 23: Tasks with status='COMPLETED' are never sent as upcoming or overdue reminders."""
        mock_service = MagicMock()
        mock_get_gmail.return_value = (mock_service, "OK")

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        # Completed upcoming task
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, google_event_id)
            VALUES ('Ali', 'amohammedali2005@gmail.com', 'Completed task 1', ?, 'MEDIUM', ?, 'Meeting', 'COMPLETED', '2026-10-01T00:00:00', 'cal_1')
        ''', (self.tomorrow_str, self.today_str))
        # Completed overdue task
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, google_event_id)
            VALUES ('Ali', 'amohammedali2005@gmail.com', 'Completed task 2', ?, 'MEDIUM', ?, 'Meeting', 'COMPLETED', '2026-10-01T00:00:00', 'cal_2')
        ''', (self.three_days_ago_str, self.today_str))
        conn.commit()
        conn.close()

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}):
            summary = process_reminders(db_path=TEST_DB)

        self.assertEqual(summary["processed"], 0)
        self.assertEqual(summary["sent"], 0)
        mock_service.users.return_value.messages.return_value.send.assert_not_called()

    @patch("process_reminders.get_gmail_service")
    def test_expired_gmail_auth_returns_gracefully(self, mock_get_gmail):
        """Case 24: Expired or unavailable Gmail auth returns error summary gracefully without crashing."""
        mock_get_gmail.return_value = (None, "OAuth token expired / network unreachable")

        self._link_cal("cal_3", "Kashif", "Pending task", self.tomorrow_str, "Meeting")
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, google_event_id)
            VALUES ('Kashif', 'kashifahamad2003@gmail.com', 'Pending task', ?, 'MEDIUM', ?, 'Meeting', 'PENDING', '2026-10-01T00:00:00', 'cal_3')
        ''', (self.tomorrow_str, self.today_str))
        conn.commit()
        conn.close()

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}):
            summary = process_reminders(db_path=TEST_DB)

        self.assertEqual(summary["sent"], 0)
        self.assertIn("error", summary)
        self.assertIn("OAuth token expired", summary["error"])

if __name__ == "__main__":
    unittest.main()
