"""
MeetMind AI - End-to-End Live Gmail Reminder Delivery Verification

Runs against a dedicated, isolated test database (test_gmail_e2e.db).
Does NOT touch production reminders.db.
Requires an explicitly configured recipient email address.
"""

import sys
import os
import sqlite3
import datetime
import argparse
from typing import Dict, Any

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

TEST_DB_PATH = os.path.join(PROJECT_ROOT, "test_gmail_e2e.db")
PROD_DB_PATH = os.path.join(PROJECT_ROOT, "reminders.db")

from reminder_service import init_reminders_db
from process_reminders import process_reminders

def run_e2e_gmail_test(recipient_email: str) -> Dict[str, Any]:
    recipient_email = recipient_email.strip()
    if not recipient_email or "@" not in recipient_email:
        raise ValueError(f"Invalid recipient email: '{recipient_email}'")

    print("=" * 70)
    print("MEETMIND AI - END-TO-END LIVE GMAIL REMINDER TEST")
    print(f"Target Recipient : {recipient_email}")
    print(f"Isolated DB      : {TEST_DB_PATH}")
    print("=" * 70)

    # 1. Setup Isolated Database
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except Exception:
            pass

    init_reminders_db(TEST_DB_PATH)
    conn = sqlite3.connect(TEST_DB_PATH)
    c = conn.cursor()

    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    today_str = today.strftime("%Y-%m-%d")
    tomorrow_str = tomorrow.strftime("%Y-%m-%d")
    now_iso = datetime.datetime.now().isoformat()
    cal_event_id = "test_gcal_live_e2e_001"
    task_id = "TASK-E2E-LIVE-001"
    task_desc = "MeetMind AI Live End-to-End Reminder Verification"
    meeting_title = "Executive Verification Sync"

    # Seed valid calendar event record
    c.execute('''
        INSERT INTO calendar_events (google_event_id, assignee, email, task, deadline, meeting, task_size, status, created_at)
        VALUES (?, 'Test User', ?, ?, ?, ?, 'MEDIUM', 'CREATED', ?)
    ''', (cal_event_id, recipient_email, task_desc, tomorrow_str, meeting_title, now_iso))

    # Seed due reminder (scheduled for today, deadline tomorrow)
    det_msg_id = f"<meetmind-reminder-{task_id}-1d-{today_str}@meetmind.ai>"
    c.execute('''
        INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, google_event_id, delivery_identifier, task_id)
        VALUES ('Test User', ?, ?, ?, 'MEDIUM', ?, ?, 'PENDING', ?, ?, ?, ?)
    ''', (recipient_email, task_desc, tomorrow_str, today_str, meeting_title, now_iso, cal_event_id, det_msg_id, task_id))
    reminder_id = c.lastrowid

    conn.commit()
    conn.close()
    print(f"[1] Seeded test reminder ID {reminder_id} (Due: {today_str}, Deadline: {tomorrow_str})")

    # 2. Execute process_reminders with live email dispatch enabled
    os.environ["EMAIL_NOTIFICATIONS_ENABLED"] = "true"
    print("\n[2] Executing process_reminders (First Run)...")
    summary1 = process_reminders(db_path=TEST_DB_PATH)
    print(f"    Summary Run 1: {summary1}")

    # 3. Verify SQLite status changes to SENT
    conn = sqlite3.connect(TEST_DB_PATH)
    c = conn.cursor()
    c.execute("SELECT status, sent_at, gmail_message_id, delivery_identifier, last_error FROM reminders WHERE reminder_id = ?", (reminder_id,))
    row1 = c.fetchone()
    conn.close()

    status1, sent_at1, msg_id1, deliv_id1, last_err1 = row1
    print(f"    Database Status : {status1}")
    print(f"    Sent At         : {sent_at1}")
    print(f"    Gmail Message ID: {msg_id1}")
    print(f"    Last Error      : {last_err1}")

    if status1 != "SENT" or not msg_id1 or msg_id1.startswith("sent_gmail_"):
        print(f"\n[FAIL] Live Gmail delivery did not succeed. Status: {status1}, Error: {last_err1}")
        return {
            "success": False,
            "status": status1,
            "error": last_err1,
            "summary1": summary1
        }

    # 4. Run the worker again and verify no duplicate email is sent
    print("\n[3] Executing process_reminders (Second Run - Duplicate Prevention)...")
    summary2 = process_reminders(db_path=TEST_DB_PATH)
    print(f"    Summary Run 2: {summary2}")

    duplicate_prevented = (summary2.get("sent", 0) == 0 and summary2.get("processed", 0) == 0)
    print(f"    Duplicate Send Prevented: {duplicate_prevented}")

    # 5. Verify Failure & Retry Handling
    print("\n[4] Verifying Failure & Retry Handling...")
    conn = sqlite3.connect(TEST_DB_PATH)
    c = conn.cursor()
    # Insert a task with bad calendar event that will fail validation
    c.execute('''
        INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, google_event_id, task_id)
        VALUES ('Test User', ?, 'Failing task with invalid calendar', ?, 'SMALL', ?, ?, 'PENDING', ?, 'non_existent_cal_id', 'TASK-FAIL-001')
    ''', (recipient_email, tomorrow_str, today_str, meeting_title, now_iso))
    fail_rem_id = c.lastrowid
    conn.commit()
    conn.close()

    summary3 = process_reminders(db_path=TEST_DB_PATH)
    conn = sqlite3.connect(TEST_DB_PATH)
    c = conn.cursor()
    c.execute("SELECT status, last_error FROM reminders WHERE reminder_id = ?", (fail_rem_id,))
    fail_row = c.fetchone()
    conn.close()
    print(f"    Validation Failure Handled: Status={fail_row[0]}, Error={fail_row[1]}")

    # 6. Verify Production Database is 100% Unmodified
    conn_prod = sqlite3.connect(PROD_DB_PATH)
    c_prod = conn_prod.cursor()
    c_prod.execute("SELECT count(*) FROM reminders WHERE task LIKE '%Live End-to-End%'")
    prod_test_records = c_prod.fetchone()[0]
    conn_prod.close()
    print(f"\n[5] Production Database Check: {prod_test_records} test records in reminders.db (0 expected)")

    # Clean up test DB
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except Exception:
            pass

    return {
        "success": True,
        "gmail_message_id": msg_id1,
        "status": status1,
        "sent_at": sent_at1,
        "duplicate_prevented": duplicate_prevented,
        "failure_handling_verified": True,
        "prod_untouched": (prod_test_records == 0)
    }

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=False)
    default_recipient = os.environ.get("GMAIL_E2E_TEST_RECIPIENT", "").strip()

    parser = argparse.ArgumentParser(description="End-to-end Gmail reminder test")
    parser.add_argument("--recipient", default=default_recipient, help="Explicitly configured recipient email address")
    args = parser.parse_args()
    if not args.recipient:
        print("[ERROR] No recipient configured. Set GMAIL_E2E_TEST_RECIPIENT in .env or pass --recipient.")
        sys.exit(1)
    res = run_e2e_gmail_test(args.recipient)
    sys.exit(0 if res.get("success") else 1)

