import os
import sys
import sqlite3
import datetime
import unittest
import zoneinfo
from unittest.mock import patch, MagicMock

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(PROJECT_ROOT, "test_rules_sandbox.db")
try:
    import zoneinfo
    KOLKATA_TZ = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    KOLKATA_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="Asia/Kolkata")

from reminder_service import init_reminders_db, schedule_reminders
from process_reminders import process_reminders
from task_lifecycle import update_jira_deadline_and_reschedule

class TestReminderRulesAndTimeGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_env = os.environ.copy()
        init_reminders_db(TEST_DB)

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls._original_env)
        if os.path.exists(TEST_DB):
            try:
                os.remove(TEST_DB)
            except Exception:
                pass

    def setUp(self):
        self._test_env = os.environ.copy()
        init_reminders_db(TEST_DB)
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("DELETE FROM reminders")
        c.execute("DELETE FROM calendar_events")
        conn.commit()
        conn.close()

        self.today_kolkata = datetime.datetime.now(KOLKATA_TZ).date()
        self.today_str = self.today_kolkata.strftime("%Y-%m-%d")
        self.tomorrow_str = (self.today_kolkata + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        self.two_days_later_str = (self.today_kolkata + datetime.timedelta(days=2)).strftime("%Y-%m-%d")
        self.three_days_later_str = (self.today_kolkata + datetime.timedelta(days=3)).strftime("%Y-%m-%d")
        self.five_days_later_str = (self.today_kolkata + datetime.timedelta(days=5)).strftime("%Y-%m-%d")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._test_env)

    def _link_calendar_event(self, event_id: str, assignee: str, task: str, deadline: str, meeting: str = "Sprint Sync"):
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT OR REPLACE INTO calendar_events (google_event_id, assignee, email, task, deadline, meeting, status, created_at)
            VALUES (?, ?, 'test@gmail.com', ?, ?, ?, 'CREATED', ?)
        ''', (event_id, assignee, task, deadline, meeting, datetime.datetime.now().isoformat()))
        conn.commit()
        conn.close()

    # 1. Small task creates exactly one reminder
    def test_small_task_creates_exactly_one_reminder(self):
        """Rule 1: SMALL tasks create exactly one reminder, deadline minus 1 day."""
        action_items = [{
            "task": "Small documentation fix",
            "assignee": "Kashif",
            "deadline": self.three_days_later_str,
            "task_size": "SMALL",
            "google_event_id": "cal_small_01"
        }]
        schedule_reminders(action_items, "Sprint Sync", db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT reminder_id, reminder_date, task_size FROM reminders WHERE assignee='Kashif'")
        rows = c.fetchall()
        conn.close()

        self.assertEqual(len(rows), 1, "SMALL task must create exactly one reminder")
        expected_date = (self.today_kolkata + datetime.timedelta(days=2)).strftime("%Y-%m-%d")
        self.assertEqual(rows[0][1], expected_date, "SMALL reminder date must be deadline minus 1 day")

    # 2. Medium task creates exactly one reminder
    def test_medium_task_creates_exactly_one_reminder(self):
        """Rule 2: MEDIUM tasks create exactly one reminder, deadline minus 1 day."""
        action_items = [{
            "task": "Medium API endpoint refactoring",
            "assignee": "Ali",
            "deadline": self.three_days_later_str,
            "task_size": "MEDIUM",
            "google_event_id": "cal_med_01"
        }]
        schedule_reminders(action_items, "Sprint Sync", db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT reminder_id, reminder_date, task_size FROM reminders WHERE assignee='Ali'")
        rows = c.fetchall()
        conn.close()

        self.assertEqual(len(rows), 1, "MEDIUM task must create exactly one reminder")
        expected_date = (self.today_kolkata + datetime.timedelta(days=2)).strftime("%Y-%m-%d")
        self.assertEqual(rows[0][1], expected_date, "MEDIUM reminder date must be deadline minus 1 day")

    # 3. Large task creates exactly two reminders
    def test_large_task_creates_exactly_two_reminders(self):
        """Rule 3: LARGE tasks create exactly two separate reminder records."""
        action_items = [{
            "task": "Complete core payment architecture and integration",
            "assignee": "Priya",
            "deadline": self.five_days_later_str,
            "task_size": "LARGE",
            "google_event_id": "cal_large_01"
        }]
        schedule_reminders(action_items, "Sprint Sync", db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT reminder_id, reminder_date, task_size, delivery_identifier FROM reminders WHERE assignee='Priya' ORDER BY reminder_date ASC")
        rows = c.fetchall()
        conn.close()

        self.assertEqual(len(rows), 2, "LARGE task must create exactly two separate reminders")
        self.assertNotEqual(rows[0][0], rows[1][0], "Each reminder must have a distinct reminder_id")
        self.assertNotEqual(rows[0][3], rows[1][3], "Each reminder must have a unique delivery_identifier")

    # 4. Large-task reminder dates are exactly two days and one day before deadline
    def test_large_task_reminder_dates_are_two_days_and_one_day_before(self):
        """Rule 4: Large-task reminder dates must be exactly deadline-2d and deadline-1d."""
        action_items = [{
            "task": "Build recommendation engine pipeline",
            "assignee": "Megashree",
            "deadline": self.five_days_later_str,
            "task_size": "LARGE",
            "google_event_id": "cal_large_02"
        }]
        schedule_reminders(action_items, "Sprint Sync", db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT reminder_date FROM reminders WHERE assignee='Megashree' ORDER BY reminder_date ASC")
        rows = c.fetchall()
        conn.close()

        expected_date_1 = (self.today_kolkata + datetime.timedelta(days=3)).strftime("%Y-%m-%d")  # 5 - 2 = 3
        expected_date_2 = (self.today_kolkata + datetime.timedelta(days=4)).strftime("%Y-%m-%d")  # 5 - 1 = 4
        self.assertEqual(rows[0][0], expected_date_1, "First reminder must be deadline minus 2 days")
        self.assertEqual(rows[1][0], expected_date_2, "Second reminder must be deadline minus 1 day")

    # 5. Reprocessing a transcript does not create duplicate reminders
    def test_reprocessing_transcript_does_not_duplicate_reminders(self):
        """Rule 5: Reprocessing the same meeting action items must not insert duplicate reminders."""
        action_items = [{
            "task": "Database schema migration",
            "assignee": "Ali",
            "deadline": self.five_days_later_str,
            "task_size": "LARGE",
            "google_event_id": "cal_dedup_01"
        }]
        # Process first time
        schedule_reminders(action_items, "Meeting Dedup", db_path=TEST_DB)
        # Process second time (transcript reprocessed)
        schedule_reminders(action_items, "Meeting Dedup", db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM reminders WHERE assignee='Ali'")
        count = c.fetchone()[0]
        conn.close()

        self.assertEqual(count, 2, "Re-running schedule_reminders must keep exactly 2 reminders for LARGE task (0 duplicates)")

    # 6. Reminder is not sent at 08:59 AM IST
    def test_reminder_not_sent_at_0859_am_ist(self):
        """Rule 6: Worker running at 08:59 AM IST must NOT send reminder scheduled for today."""
        self._link_calendar_event("cal_time_01", "Priya", "Deploy feature", self.tomorrow_str)
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, reminder_date, meeting, status, google_event_id)
            VALUES ('Priya', 'jeevadhardhinitv23@gmail.com', 'Deploy feature', ?, ?, 'Sprint Sync', 'PENDING', 'cal_time_01')
        ''', (self.tomorrow_str, self.today_str))
        conn.commit()
        conn.close()

        # Simulate 08:59 AM IST
        time_859_am = datetime.datetime.combine(self.today_kolkata, datetime.time(8, 59, 0), tzinfo=KOLKATA_TZ)

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}), \
             patch("process_reminders.get_gmail_service") as mock_get_gmail:
            mock_service = MagicMock()
            mock_get_gmail.return_value = (mock_service, None)

            res = process_reminders(db_path=TEST_DB, current_time=time_859_am)
            self.assertEqual(res["sent"], 0, "No reminder should be sent before 09:00 AM IST")
            mock_service.users().messages().send.assert_not_called()

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT status FROM reminders WHERE google_event_id='cal_time_01'")
        status = c.fetchone()[0]
        conn.close()
        self.assertEqual(status, "PENDING", "Status must remain PENDING at 08:59 AM IST")

    # 7. Reminder becomes eligible at 09:00 AM IST
    def test_reminder_becomes_eligible_at_0900_am_ist(self):
        """Rule 7: Worker running at 09:00 AM IST sends the reminder scheduled for today."""
        self._link_calendar_event("cal_time_02", "Priya", "Deploy feature", self.tomorrow_str)
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, reminder_date, meeting, status, google_event_id)
            VALUES ('Priya', 'jeevadhardhinitv23@gmail.com', 'Deploy feature', ?, ?, 'Sprint Sync', 'PENDING', 'cal_time_02')
        ''', (self.tomorrow_str, self.today_str))
        conn.commit()
        conn.close()

        # Simulate 09:00 AM IST
        time_900_am = datetime.datetime.combine(self.today_kolkata, datetime.time(9, 0, 0), tzinfo=KOLKATA_TZ)

        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}), \
             patch("process_reminders.get_gmail_service") as mock_get_gmail:
            mock_service = MagicMock()
            mock_get_gmail.return_value = (mock_service, None)
            mock_service.users().messages().list.return_value.execute.return_value = {"messages": []}
            mock_service.users().messages().send.return_value.execute.return_value = {"id": "msg_9am_ok"}

            res = process_reminders(db_path=TEST_DB, current_time=time_900_am)
            self.assertEqual(res["sent"], 1, "Reminder must be sent at 09:00 AM IST")
            mock_service.users().messages().send.assert_called_once()

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT status, gmail_message_id FROM reminders WHERE google_event_id='cal_time_02'")
        row = c.fetchone()
        conn.close()
        self.assertEqual(row[0], "SENT")
        self.assertEqual(row[1], "msg_9am_ok")

    # 8. Repeated 15-minute worker runs do not resend SENT reminders
    def test_repeated_runs_do_not_resend_sent_reminders(self):
        """Rule 8: Subsequent 15-minute runs must exclude already SENT reminders."""
        self._link_calendar_event("cal_repeat_01", "Priya", "Deploy feature", self.tomorrow_str)
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, reminder_date, meeting, status, google_event_id)
            VALUES ('Priya', 'jeevadhardhinitv23@gmail.com', 'Deploy feature', ?, ?, 'Sprint Sync', 'PENDING', 'cal_repeat_01')
        ''', (self.tomorrow_str, self.today_str))
        conn.commit()
        conn.close()

        # Run 1 at 09:00 AM IST
        time_900_am = datetime.datetime.combine(self.today_kolkata, datetime.time(9, 0, 0), tzinfo=KOLKATA_TZ)
        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}), \
             patch("process_reminders.get_gmail_service") as mock_get_gmail:
            mock_service = MagicMock()
            mock_get_gmail.return_value = (mock_service, None)
            mock_service.users().messages().list.return_value.execute.return_value = {"messages": []}
            mock_service.users().messages().send.return_value.execute.return_value = {"id": "msg_run_1"}

            res1 = process_reminders(db_path=TEST_DB, current_time=time_900_am)
            self.assertEqual(res1["sent"], 1)

        # Run 2 at 09:15 AM IST
        time_915_am = datetime.datetime.combine(self.today_kolkata, datetime.time(9, 15, 0), tzinfo=KOLKATA_TZ)
        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}), \
             patch("process_reminders.get_gmail_service") as mock_get_gmail:
            mock_service = MagicMock()
            mock_get_gmail.return_value = (mock_service, None)

            res2 = process_reminders(db_path=TEST_DB, current_time=time_915_am)
            self.assertEqual(res2["sent"], 0, "Run at 09:15 AM must not resend SENT reminder")
            mock_service.users().messages().send.assert_not_called()

    # 9. No deadline-day reminder is generated
    def test_no_deadline_day_reminder_is_generated(self):
        """Rule 9: No upcoming reminder is generated or dispatched on the deadline day itself."""
        self._link_calendar_event("cal_dl_today", "Priya", "Due today task", self.today_str)
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, reminder_date, meeting, status, google_event_id)
            VALUES ('Priya', 'jeevadhardhinitv23@gmail.com', 'Due today task', ?, ?, 'Sprint Sync', 'PENDING', 'cal_dl_today')
        ''', (self.today_str, self.today_str))
        conn.commit()
        conn.close()

        time_1000_am = datetime.datetime.combine(self.today_kolkata, datetime.time(10, 0, 0), tzinfo=KOLKATA_TZ)
        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}), \
             patch("process_reminders.get_gmail_service") as mock_get_gmail:
            mock_service = MagicMock()
            mock_get_gmail.return_value = (mock_service, None)

            res = process_reminders(db_path=TEST_DB, current_time=time_1000_am)
            self.assertEqual(res["sent"], 0, "No reminder should be sent on the deadline day itself")
            mock_service.users().messages().send.assert_not_called()

    # 10. Historical overdue records cannot generate unwanted emails
    def test_historical_overdue_records_cannot_generate_emails(self):
        """Rule 10: Unlinked historical overdue records are strictly excluded from dispatch."""
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, reminder_date, meeting, status, google_event_id)
            VALUES ('Kashif', 'kashifahamad2003@gmail.com', 'Historical task', '2026-10-06', '2026-10-05', 'Past Meeting', 'PENDING', NULL)
        ''')
        conn.commit()
        conn.close()

        time_1000_am = datetime.datetime.combine(self.today_kolkata, datetime.time(10, 0, 0), tzinfo=KOLKATA_TZ)
        with patch.dict(os.environ, {"EMAIL_NOTIFICATIONS_ENABLED": "true"}), \
             patch("process_reminders.get_gmail_service") as mock_get_gmail:
            mock_service = MagicMock()
            mock_get_gmail.return_value = (mock_service, None)

            res = process_reminders(db_path=TEST_DB, current_time=time_1000_am)
            self.assertEqual(res["sent"], 0, "Historical unlinked record must never be sent")
            mock_service.users().messages().send.assert_not_called()

    # 11. Rescheduling preserves sent reminder history
    def test_rescheduling_preserves_sent_reminder_history(self):
        """Rule 11: Rescheduling updates pending reminders while preserving already SENT reminder rows."""
        self._link_calendar_event("cal_resched_01", "Priya", "Large refactor task", self.three_days_later_str)
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        # Insert 2 reminders for LARGE task: Reminder 1 is SENT, Reminder 2 is PENDING
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, sent_at, gmail_message_id, google_event_id)
            VALUES ('Priya', 'jeevadhardhinitv23@gmail.com', 'Large refactor task', ?, 'LARGE', ?, 'Sprint Sync', 'SENT', '2026-11-01T09:00:00', 'sent_msg_historical_1', 'cal_resched_01')
        ''', (self.three_days_later_str, self.today_str))
        rem1_id = c.lastrowid

        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, google_event_id)
            VALUES ('Priya', 'jeevadhardhinitv23@gmail.com', 'Large refactor task', ?, 'LARGE', ?, 'Sprint Sync', 'PENDING', 'cal_resched_01')
        ''', (self.three_days_later_str, self.tomorrow_str))
        rem2_id = c.lastrowid
        conn.commit()
        conn.close()

        # Reschedule deadline by +7 days
        new_deadline = (self.today_kolkata + datetime.timedelta(days=10)).strftime("%Y-%m-%d")
        mock_cal = MagicMock()
        res = update_jira_deadline_and_reschedule("Large refactor task", new_deadline, db_path=TEST_DB, calendar_service=mock_cal)

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["rescheduled_count"], 1, "Only the PENDING reminder should be rescheduled")

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT reminder_id, status, sent_at, gmail_message_id, deadline FROM reminders WHERE reminder_id = ?", (rem1_id,))
        row1 = c.fetchone()
        self.assertEqual(row1[1], "SENT", "Reminder 1 must remain SENT")
        self.assertEqual(row1[2], "2026-11-01T09:00:00", "sent_at timestamp must be preserved")
        self.assertEqual(row1[3], "sent_msg_historical_1", "gmail_message_id must be preserved")

        c.execute("SELECT reminder_id, status, deadline, reminder_date FROM reminders WHERE reminder_id = ?", (rem2_id,))
        row2 = c.fetchone()
        self.assertEqual(row2[1], "PENDING", "Pending reminder must remain PENDING")
        self.assertEqual(row2[2], new_deadline, "Pending reminder deadline must be updated to new deadline")
        expected_new_reminder_date = (self.today_kolkata + datetime.timedelta(days=9)).strftime("%Y-%m-%d") # 10 - 1 = 9
        self.assertEqual(row2[3], expected_new_reminder_date)
        conn.close()

    # 12. Priya has two different LARGE tasks with same deadline and meeting: 4 distinct reminders & identifiers
    def test_two_large_tasks_same_assignee_and_deadline_have_distinct_identifiers(self):
        """Audit Test: Priya has two distinct LARGE tasks with identical deadline and meeting.
        Must generate exactly 4 reminders with 4 completely distinct delivery identifiers.
        """
        task_a = "Implement Core OAuth2 Authorization Server"
        task_b = "Refactor Database Transaction Isolation and Sharding"
        meeting_name = "Sprint Planning Meeting"

        self._link_calendar_event("cal_priya_a", "Priya", task_a, self.five_days_later_str, meeting_name)
        self._link_calendar_event("cal_priya_b", "Priya", task_b, self.five_days_later_str, meeting_name)

        action_items = [
            {
                "task": task_a,
                "assignee": "Priya",
                "deadline": self.five_days_later_str,
                "task_size": "LARGE",
                "google_event_id": "cal_priya_a"
            },
            {
                "task": task_b,
                "assignee": "Priya",
                "deadline": self.five_days_later_str,
                "task_size": "LARGE",
                "google_event_id": "cal_priya_b"
            }
        ]

        # First run: schedule action items
        schedule_reminders(action_items, meeting_name, db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            SELECT reminder_id, task, reminder_date, delivery_identifier 
            FROM reminders 
            WHERE assignee='Priya' 
            ORDER BY task, reminder_date ASC
        ''')
        rows = c.fetchall()
        conn.close()

        # 1. Total records must be exactly 4
        self.assertEqual(len(rows), 4, "Must generate exactly 4 reminders (2 for Task A, 2 for Task B)")

        # 2. Check all delivery_identifiers are completely distinct
        identifiers = [r[3] for r in rows]
        unique_identifiers = set(identifiers)
        self.assertEqual(len(unique_identifiers), 4, "All four delivery identifiers must be completely unique")

        # 3. Check offsets: each task must have one at 2d before and one at 1d before
        expected_2d = (self.today_kolkata + datetime.timedelta(days=3)).strftime("%Y-%m-%d")
        expected_1d = (self.today_kolkata + datetime.timedelta(days=4)).strftime("%Y-%m-%d")
        
        task_a_rows = [r for r in rows if r[1] == task_a]
        task_b_rows = [r for r in rows if r[1] == task_b]
        self.assertEqual(len(task_a_rows), 2)
        self.assertEqual(len(task_b_rows), 2)
        self.assertEqual(task_a_rows[0][2], expected_2d)
        self.assertEqual(task_a_rows[1][2], expected_1d)
        self.assertEqual(task_b_rows[0][2], expected_2d)
        self.assertEqual(task_b_rows[1][2], expected_1d)

        # 4. Check stable task identity in identifier (not identical between tasks)
        self.assertNotEqual(task_a_rows[0][3], task_b_rows[0][3], "2d reminders for Task A and B must differ")
        self.assertNotEqual(task_a_rows[1][3], task_b_rows[1][3], "1d reminders for Task A and B must differ")

        # 5. Reprocessing the same transcript does not duplicate
        schedule_reminders(action_items, meeting_name, db_path=TEST_DB)
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM reminders WHERE assignee='Priya'")
        total_count = c.fetchone()[0]
        conn.close()
        self.assertEqual(total_count, 4, "Reprocessing transcript must produce zero duplicates (count remains 4)")

        # 6. Rescheduling Task A pending reminders creates new valid identifiers
        new_deadline_a = (self.today_kolkata + datetime.timedelta(days=8)).strftime("%Y-%m-%d")
        mock_cal = MagicMock()
        update_jira_deadline_and_reschedule(task_a, new_deadline_a, db_path=TEST_DB, calendar_service=mock_cal)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT reminder_id, reminder_date, delivery_identifier FROM reminders WHERE task=? ORDER BY reminder_date ASC", (task_a,))
        rescheduled_a_rows = c.fetchall()
        conn.close()
        
        self.assertEqual(len(rescheduled_a_rows), 2)
        expected_rescheduled_2d = (self.today_kolkata + datetime.timedelta(days=6)).strftime("%Y-%m-%d")
        expected_rescheduled_1d = (self.today_kolkata + datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        self.assertEqual(rescheduled_a_rows[0][1], expected_rescheduled_2d)
        self.assertEqual(rescheduled_a_rows[1][1], expected_rescheduled_1d)
        self.assertIn(expected_rescheduled_2d, rescheduled_a_rows[0][2])
        self.assertIn(expected_rescheduled_1d, rescheduled_a_rows[1][2])

    # 13. Identical task descriptions across different meetings have distinct task_ids and identifiers
    def test_identical_task_descriptions_across_different_meetings_have_distinct_task_ids(self):
        """Audit Requirement: Different tasks across different meetings must have different task_ids,
        even if their descriptions, assignees and deadlines are identical.
        """
        identical_task_desc = "Review API security protocols and update TLS certificates"
        deadline_str = self.five_days_later_str
        meeting_1 = "Sprint 42 Planning Meeting"
        meeting_2 = "Security Architecture Review"

        self._link_calendar_event("cal_meet1_task", "Priya", identical_task_desc, deadline_str, meeting_1)
        self._link_calendar_event("cal_meet2_task", "Priya", identical_task_desc, deadline_str, meeting_2)

        items_meet1 = [{
            "task": identical_task_desc,
            "assignee": "Priya",
            "deadline": deadline_str,
            "task_size": "LARGE",
            "google_event_id": "cal_meet1_task"
        }]
        items_meet2 = [{
            "task": identical_task_desc,
            "assignee": "Priya",
            "deadline": deadline_str,
            "task_size": "LARGE",
            "google_event_id": "cal_meet2_task"
        }]

        # Schedule Meeting 1
        schedule_reminders(items_meet1, meeting_1, db_path=TEST_DB)
        # Schedule Meeting 2
        schedule_reminders(items_meet2, meeting_2, db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            SELECT reminder_id, meeting, task_id, delivery_identifier, reminder_date 
            FROM reminders 
            WHERE assignee='Priya'
            ORDER BY meeting, reminder_date ASC
        ''')
        rows = c.fetchall()
        conn.close()

        # Must have 4 reminders total (2 for each LARGE task)
        self.assertEqual(len(rows), 4, "Should have 4 reminders total across the 2 meetings")

        m1_rows = [r for r in rows if r[1] == meeting_1]
        m2_rows = [r for r in rows if r[1] == meeting_2]
        self.assertEqual(len(m1_rows), 2)
        self.assertEqual(len(m2_rows), 2)

        # Meeting 1 reminders share the same task_id
        self.assertEqual(m1_rows[0][2], m1_rows[1][2], "Meeting 1 reminders should share one persistent task_id")
        # Meeting 2 reminders share the same task_id
        self.assertEqual(m2_rows[0][2], m2_rows[1][2], "Meeting 2 reminders should share one persistent task_id")

        # Crucial check: Meeting 1 and Meeting 2 must NOT share the same task_id
        self.assertNotEqual(m1_rows[0][2], m2_rows[0][2], "Different meetings must have completely distinct task_ids")

        # Crucial check: All 4 delivery identifiers must be completely distinct
        all_identifiers = [r[3] for r in rows]
        self.assertEqual(len(set(all_identifiers)), 4, "All 4 delivery identifiers must be completely unique")

        # Each delivery identifier contains its respective task_id
        self.assertIn(m1_rows[0][2], m1_rows[0][3])
        self.assertIn(m2_rows[0][2], m2_rows[0][3])

    # 14. Distinct tasks with identical deadlines have distinct task_ids
    def test_distinct_tasks_with_identical_deadlines_have_distinct_task_ids(self):
        """Audit Requirement: Distinct tasks with identical deadlines must have different task_ids."""
        task_1 = "Configure Nginx ingress reverse proxy"
        task_2 = "Optimize PostgreSQL index scan queries"
        deadline_str = self.four_days_later_str = (self.today_kolkata + datetime.timedelta(days=4)).strftime("%Y-%m-%d")
        meeting_name = "Infrastructure Weekly"

        self._link_calendar_event("cal_infra_01", "Ali", task_1, deadline_str, meeting_name)
        self._link_calendar_event("cal_infra_02", "Ali", task_2, deadline_str, meeting_name)

        action_items = [
            {
                "task": task_1,
                "assignee": "Ali",
                "deadline": deadline_str,
                "task_size": "SMALL",
                "google_event_id": "cal_infra_01"
            },
            {
                "task": task_2,
                "assignee": "Ali",
                "deadline": deadline_str,
                "task_size": "SMALL",
                "google_event_id": "cal_infra_02"
            }
        ]

        schedule_reminders(action_items, meeting_name, db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            SELECT reminder_id, task, task_id, delivery_identifier 
            FROM reminders 
            WHERE assignee='Ali' 
            ORDER BY task ASC
        ''')
        rows = c.fetchall()
        conn.close()

        self.assertEqual(len(rows), 2)
        t1_row = [r for r in rows if r[1] == task_1][0]
        t2_row = [r for r in rows if r[1] == task_2][0]

        # Distinct task_ids
        self.assertNotEqual(t1_row[2], t2_row[2], "Distinct tasks must have distinct task_ids")
        # Distinct delivery identifiers
        self.assertNotEqual(t1_row[3], t2_row[3], "Distinct tasks must have distinct delivery identifiers")

    # 15. Jira issue key used as task_id when available and reprocessed idempotently
    def test_jira_issue_key_used_as_task_id_and_reused_on_reprocess(self):
        """Audit Requirement: Use Jira issue key when available, persist in SQLite, and reuse on reprocess."""
        jira_key = "ENG-9042"
        task_desc = "Implement Stripe webhook idempotency layer"
        deadline_str = self.five_days_later_str
        meeting_name = "Backend Architecture"

        self._link_calendar_event("cal_jira_01", "Priya", task_desc, deadline_str, meeting_name)

        action_items = [{
            "task": task_desc,
            "assignee": "Priya",
            "deadline": deadline_str,
            "task_size": "LARGE",
            "jira_issue_key": jira_key,
            "google_event_id": "cal_jira_01"
        }]

        # First scheduling run
        schedule_reminders(action_items, meeting_name, db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT reminder_id, task_id, delivery_identifier FROM reminders WHERE assignee='Priya'")
        rows_run1 = c.fetchall()
        conn.close()

        self.assertEqual(len(rows_run1), 2)
        for r in rows_run1:
            self.assertEqual(r[1], jira_key, "task_id must match Jira issue key")
            self.assertIn(jira_key, r[2], "delivery_identifier must incorporate Jira issue key")

        # Reprocess same transcript
        schedule_reminders(action_items, meeting_name, db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT reminder_id, task_id, delivery_identifier FROM reminders WHERE assignee='Priya'")
        rows_run2 = c.fetchall()
        conn.close()

        self.assertEqual(len(rows_run2), 2, "Reprocessing must not create duplicate reminders")
        for r in rows_run2:
            self.assertEqual(r[1], jira_key, "task_id must remain unchanged after reprocessing")

    # 16. Safe migration backfills task_legacy_id without altering historical records
    def test_safe_migration_backfills_task_legacy_id(self):
        """Audit Requirement: Migrate existing records safely without deleting or modifying historical task data."""
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        # Insert a simulated legacy record with NULL task_id
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, task_id)
            VALUES ('Priya', 'priya@example.com', 'Legacy task description', '2026-11-01', 'MEDIUM', '2026-10-31', 'Old Standup', 'SENT', '2026-10-25T10:00:00', NULL)
        ''')
        legacy_id = c.lastrowid
        conn.commit()
        conn.close()

        # Run init_reminders_db to trigger migration
        init_reminders_db(TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT reminder_id, task, deadline, status, task_id FROM reminders WHERE reminder_id = ?", (legacy_id,))
        row = c.fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row[1], 'Legacy task description', "Task text must remain untouched")
        self.assertEqual(row[2], '2026-11-01', "Deadline must remain untouched")
        self.assertEqual(row[3], 'SENT', "Status must remain untouched")
        self.assertEqual(row[4], f"task_legacy_{legacy_id}", "task_id should be backfilled with stable legacy id")

if __name__ == "__main__":
    unittest.main()
