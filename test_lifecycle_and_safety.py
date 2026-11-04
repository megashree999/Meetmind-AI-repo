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

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(PROJECT_ROOT, "test_safety_sandbox.db")

from reminder_service import init_reminders_db
from process_reminders import process_reminders as _orig_process_reminders, validate_calendar_event, KOLKATA_TZ

def process_reminders(db_path="test_safety_sandbox.db", current_time=None):
    """Wrapper ensuring time-gate allows upcoming reminder tests to run regardless of midnight clock hour."""
    if current_time is None:
        current_time = datetime.datetime.now(KOLKATA_TZ).replace(hour=10, minute=0, second=0)
    return _orig_process_reminders(db_path=db_path, current_time=current_time)
from task_lifecycle import (
    cancel_reminders_for_jira_task,
    update_jira_deadline_and_reschedule,
    handle_calendar_event_deleted,
    update_reminder_assignee
)

class TestReminderLifecycleAndSafety(unittest.TestCase):
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
        init_reminders_db(TEST_DB)
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("DELETE FROM reminders")
        c.execute("DELETE FROM calendar_events")
        conn.commit()
        conn.close()

        self.today = datetime.datetime.now().date()
        self.today_str = self.today.strftime("%Y-%m-%d")
        self.tomorrow_str = (self.today + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        self.yesterday_str = (self.today - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    def _insert_calendar_event(
        self,
        google_event_id: str,
        assignee: str = "Priya",
        task: str = "Test payment flow",
        deadline: str = None,
        meeting: str = "Meeting 123",
        status: str = "CREATED"
    ):
        deadline = deadline or self.tomorrow_str
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO calendar_events (google_event_id, assignee, email, task, deadline, meeting, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (google_event_id, assignee, "jeevadhardhinitv23@gmail.com", task, deadline, meeting, status, datetime.datetime.now().isoformat()))
        conn.commit()
        conn.close()

    def _insert_reminder(
        self,
        assignee: str = "Priya",
        task: str = "Test payment flow",
        deadline: str = None,
        reminder_date: str = None,
        meeting: str = "Meeting 123",
        status: str = "PENDING",
        google_event_id: str = None,
        processing_started_at: str = None
    ):
        deadline = deadline or self.tomorrow_str
        reminder_date = reminder_date or self.today_str
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, reminder_date, meeting, status, google_event_id, processing_started_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (assignee, "jeevadhardhinitv23@gmail.com", task, deadline, reminder_date, meeting, status, google_event_id, processing_started_at, datetime.datetime.now().isoformat()))
        r_id = c.lastrowid
        conn.commit()
        conn.close()
        return r_id

    # 1. Missing Calendar event ID
    def test_missing_calendar_event_id(self):
        """Case 1: Reminder with missing google_event_id must never be dispatched."""
        r_id = self._insert_reminder(google_event_id=None)

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}), \
             patch("process_reminders.get_gmail_service") as mock_get_gmail:
            mock_service = MagicMock()
            mock_get_gmail.return_value = (mock_service, None)

            res = process_reminders(TEST_DB)
            self.assertEqual(res["sent"], 0, "No email should be sent for unlinked reminder")
            mock_service.users().messages().send.assert_not_called()

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT status FROM reminders WHERE reminder_id = ?", (r_id,))
        self.assertEqual(c.fetchone()[0], "PENDING", "Historical/unlinked record status must remain unchanged")
        conn.close()

    # 2. Unknown Calendar event ID
    def test_unknown_calendar_event_id(self):
        """Case 2: Reminder with non-existent google_event_id must fail validation and not send."""
        r_id = self._insert_reminder(google_event_id="unknown_cal_event_999")

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}), \
             patch("process_reminders.get_gmail_service") as mock_get_gmail:
            mock_service = MagicMock()
            mock_get_gmail.return_value = (mock_service, None)

            res = process_reminders(TEST_DB)
            self.assertEqual(res["sent"], 0, "No email should be sent for unknown calendar event")
            self.assertEqual(res["skipped"], 1)
            mock_service.users().messages().send.assert_not_called()

    # 3. Simulated Calendar event
    def test_simulated_calendar_event(self):
        """Case 3: Simulated calendar event (simulated_cal_* or status=SIMULATED) must be rejected."""
        self._insert_calendar_event("simulated_cal_12345", status="SIMULATED")
        r_id = self._insert_reminder(google_event_id="simulated_cal_12345")

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}), \
             patch("process_reminders.get_gmail_service") as mock_get_gmail:
            mock_service = MagicMock()
            mock_get_gmail.return_value = (mock_service, None)

            res = process_reminders(TEST_DB)
            self.assertEqual(res["sent"], 0, "No email should be sent for simulated event")
            self.assertEqual(res["skipped"], 1)
            mock_service.users().messages().send.assert_not_called()

    # 4. Calendar event with status other than CREATED
    def test_calendar_event_status_other_than_created(self):
        """Case 4: Calendar event with status 'TENTATIVE' or 'CANCELLED' must be rejected."""
        self._insert_calendar_event("real_event_non_created", status="CANCELLED")
        r_id = self._insert_reminder(google_event_id="real_event_non_created")

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}), \
             patch("process_reminders.get_gmail_service") as mock_get_gmail:
            mock_service = MagicMock()
            mock_get_gmail.return_value = (mock_service, None)

            res = process_reminders(TEST_DB)
            self.assertEqual(res["sent"], 0, "No email should be sent when calendar event is not CREATED")
            self.assertEqual(res["skipped"], 1)
            mock_service.users().messages().send.assert_not_called()

    # 5. Historical overdue test records
    def test_historical_overdue_test_records(self):
        """Case 5: Historical overdue records (deadline in the past, unlinked) must remain PENDING and unsent."""
        r1 = self._insert_reminder(assignee="Kashif", deadline="2026-10-06", reminder_date="2026-10-05", status="PENDING", google_event_id=None)
        r2 = self._insert_reminder(assignee="Ali", deadline="2026-10-08", reminder_date="2026-10-07", status="PENDING", google_event_id=None)

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}), \
             patch("process_reminders.get_gmail_service") as mock_get_gmail:
            mock_service = MagicMock()
            mock_get_gmail.return_value = (mock_service, None)

            res = process_reminders(TEST_DB)
            self.assertEqual(res["sent"], 0, "Historical test records must never be sent")
            mock_service.users().messages().send.assert_not_called()

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT reminder_id, status FROM reminders WHERE reminder_id IN (?, ?)", (r1, r2))
        rows = dict(c.fetchall())
        self.assertEqual(rows[r1], "PENDING", "Historical status must remain PENDING")
        self.assertEqual(rows[r2], "PENDING", "Historical status must remain PENDING")
        conn.close()

    # 6. Genuine Calendar-linked reminder
    def test_genuine_calendar_linked_reminder(self):
        """Case 6: Genuine calendar-linked reminder with matching event in CREATED status sends successfully."""
        cal_id = "real_google_cal_12345"
        self._insert_calendar_event(cal_id, assignee="Priya", task="Test payment flow", deadline=self.tomorrow_str, status="CREATED")
        r_id = self._insert_reminder(assignee="Priya", task="Test payment flow", deadline=self.tomorrow_str, google_event_id=cal_id)

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}), \
             patch("process_reminders.get_gmail_service") as mock_get_gmail:
            mock_service = MagicMock()
            mock_get_gmail.return_value = (mock_service, None)
            mock_service.users().messages().list.return_value.execute.return_value = {"messages": []}
            mock_service.users().messages().send.return_value.execute.return_value = {"id": "gmail_msg_real_001"}

            res = process_reminders(TEST_DB)
            self.assertEqual(res["sent"], 1, "Genuine calendar-linked reminder should be sent")
            mock_service.users().messages().send.assert_called_once()

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT status, gmail_message_id, delivery_identifier FROM reminders WHERE reminder_id = ?", (r_id,))
        row = c.fetchone()
        self.assertEqual(row[0], "SENT")
        self.assertEqual(row[1], "gmail_msg_real_001")
        self.assertTrue("meetmind-reminder" in row[2])
        conn.close()

    # 7. Gmail success followed by SQLite failure recovery
    def test_gmail_success_followed_by_sqlite_failure(self):
        """Case 7: If Gmail succeeds but previous DB write failed, check_already_sent_in_gmail recovers without duplicate."""
        cal_id = "real_cal_id_recovery"
        self._insert_calendar_event(cal_id, assignee="Priya", task="Test payment flow", deadline=self.tomorrow_str, status="CREATED")
        r_id = self._insert_reminder(assignee="Priya", task="Test payment flow", deadline=self.tomorrow_str, google_event_id=cal_id)

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}), \
             patch("process_reminders.get_gmail_service") as mock_get_gmail:
            mock_service = MagicMock()
            mock_get_gmail.return_value = (mock_service, None)
            # Simulate that Gmail Sent box already has this message (from previous failed DB write)
            mock_service.users().messages().list.return_value.execute.return_value = {"messages": [{"id": "gmail_existing_recovered_999"}]}

            res = process_reminders(TEST_DB)
            self.assertEqual(res["sent"], 1, "Should count as recovered sent")
            # CRITICAL: send() must NOT be called again!
            mock_service.users().messages().send.assert_not_called()

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT status, gmail_message_id FROM reminders WHERE reminder_id = ?", (r_id,))
        row = c.fetchone()
        self.assertEqual(row[0], "SENT")
        self.assertEqual(row[1], "gmail_existing_recovered_999")
        conn.close()

    # 8. Concurrent reminder workers
    def test_concurrent_reminder_workers(self):
        """Case 8: Atomic transaction prevents two concurrent workers from double-sending the same reminder."""
        cal_id = "real_cal_concurrent_test"
        self._insert_calendar_event(cal_id, assignee="Priya", task="Test payment flow", deadline=self.tomorrow_str, status="CREATED")
        r_id = self._insert_reminder(assignee="Priya", task="Test payment flow", deadline=self.tomorrow_str, google_event_id=cal_id)

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}), \
             patch("process_reminders.get_gmail_service") as mock_get_gmail:
            mock_service = MagicMock()
            mock_get_gmail.return_value = (mock_service, None)
            mock_service.users().messages().list.return_value.execute.return_value = {"messages": []}
            mock_service.users().messages().send.return_value.execute.return_value = {"id": "concurrent_gmail_msg_01"}

            # First worker runs and claims/sends
            res1 = process_reminders(TEST_DB)
            self.assertEqual(res1["sent"], 1)

            # Second worker runs immediately after
            res2 = process_reminders(TEST_DB)
            self.assertEqual(res2["sent"], 0, "Second worker must not re-process already claimed/sent reminder")
            self.assertEqual(mock_service.users().messages().send.call_count, 1, "send() must only be called once")

    # 9. Recovery of PROCESSING records after a crash
    def test_recovery_of_processing_records_after_crash(self):
        """Case 9: A record stuck in PROCESSING whose lease expired (>15m) is reclaimed and processed safely."""
        cal_id = "real_cal_crash_recovery"
        self._insert_calendar_event(cal_id, assignee="Priya", task="Test payment flow", deadline=self.tomorrow_str, status="CREATED")
        
        # Expired lease (>15m ago, use 2 hours ago so it is always expired relative to test clock)
        past_iso = (datetime.datetime.now() - datetime.timedelta(hours=2)).isoformat()
        r_id = self._insert_reminder(
            assignee="Priya",
            task="Test payment flow",
            deadline=self.tomorrow_str,
            status="PROCESSING",
            google_event_id=cal_id,
            processing_started_at=past_iso
        )

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}), \
             patch("process_reminders.get_gmail_service") as mock_get_gmail:
            mock_service = MagicMock()
            mock_get_gmail.return_value = (mock_service, None)
            mock_service.users().messages().list.return_value.execute.return_value = {"messages": []}
            mock_service.users().messages().send.return_value.execute.return_value = {"id": "recovered_after_crash_msg"}

            res = process_reminders(TEST_DB)
            self.assertEqual(res["sent"], 1, "Expired lease in PROCESSING must be safely recovered and sent")

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT status, gmail_message_id FROM reminders WHERE reminder_id = ?", (r_id,))
        row = c.fetchone()
        self.assertEqual(row[0], "SENT")
        self.assertEqual(row[1], "recovered_after_crash_msg")
        conn.close()

    # 10. Jira completion and deadline modification
    def test_jira_completion_and_deadline_modification(self):
        """Case 10: Task completion cancels reminders, and deadline change reschedules reminders and patches calendar."""
        cal_id = "jira_linked_cal_001"
        self._insert_calendar_event(cal_id, assignee="Priya", task="Implement Payment Gateway", deadline=self.tomorrow_str, status="CREATED")
        r_id = self._insert_reminder(assignee="Priya", task="Implement Payment Gateway", deadline=self.tomorrow_str, google_event_id=cal_id)

        # 10a. Test deadline change & reschedule
        mock_cal_service = MagicMock()
        new_deadline = (self.today + datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        res_reschedule = update_jira_deadline_and_reschedule(
            task_identifier="Implement Payment Gateway",
            new_deadline=new_deadline,
            db_path=TEST_DB,
            calendar_service=mock_cal_service
        )
        self.assertEqual(res_reschedule["status"], "SUCCESS")
        self.assertEqual(res_reschedule["rescheduled_count"], 1)
        mock_cal_service.events().patch.assert_called_once()

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT deadline, reminder_date FROM reminders WHERE reminder_id = ?", (r_id,))
        rem_row = c.fetchone()
        self.assertEqual(rem_row[0], new_deadline)
        conn.close()

        # 10b. Test task completion in Jira -> Cancel pending reminders
        res_cancel = cancel_reminders_for_jira_task("Implement Payment Gateway", db_path=TEST_DB)
        self.assertEqual(res_cancel["status"], "SUCCESS")
        self.assertEqual(res_cancel["cancelled_count"], 1)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT status FROM reminders WHERE reminder_id = ?", (r_id,))
        self.assertEqual(c.fetchone()[0], "CANCELLED")
        conn.close()

if __name__ == "__main__":
    unittest.main()

