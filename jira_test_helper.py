"""
MeetMind AI - Jira Live Webhook Test & Inspection Helper

Provides safe commands to seed, inspect, and clean up test records for Jira issue SCRUM-105
in an isolated test database (test_jira_helper.db) without modifying production records.

Usage:
    python jira_test_helper.py seed       # Seeds test reminders in test_jira_helper.db
    python jira_test_helper.py inspect    # Displays records in test_jira_helper.db
    python jira_test_helper.py clean      # Removes test records from test_jira_helper.db
"""

import sys
import os
import sqlite3
import datetime
import argparse

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEST_DB = os.path.join(PROJECT_ROOT, "test_jira_helper.db")
PROD_DB = os.path.join(PROJECT_ROOT, "reminders.db")

from reminder_service import init_reminders_db

def is_production_db(db_path: str) -> bool:
    """Check if the provided path resolves to the production reminders database."""
    try:
        abs_target = os.path.abspath(db_path)
        abs_prod = os.path.abspath(PROD_DB)
        if abs_target == abs_prod:
            return True
        base = os.path.basename(abs_target).lower()
        if base in ("reminders.db", "reminders.sqlite", "reminders.sqlite3"):
            return True
    except Exception:
        pass
    return False

def validate_db_for_write(db_path: str, allow_custom_non_prod: bool = False):
    """
    Enforce strict database isolation:
    1. Reject production database paths unconditionally for any mutating operation.
    2. Require explicit confirmation flag for any non-default test database.
    """
    if is_production_db(db_path):
        raise PermissionError(
            f"SAFETY VIOLATION: Mutating operations (seed/clean) on production database '{db_path}' "
            f"are strictly prohibited. Production records are protected."
        )

    abs_target = os.path.abspath(db_path)
    abs_default = os.path.abspath(DEFAULT_TEST_DB)
    if abs_target != abs_default and not allow_custom_non_prod:
        raise PermissionError(
            f"SAFETY CHECK: Target database '{db_path}' is not the default test database ({DEFAULT_TEST_DB}). "
            f"Pass --allow-custom-test-db to confirm this non-production target."
        )

def seed_test_record(db_path: str = DEFAULT_TEST_DB, allow_custom: bool = False):
    """Seed test reminders for SCRUM-105 in an isolated test database."""
    validate_db_for_write(db_path, allow_custom_non_prod=allow_custom)
    init_reminders_db(db_path)

    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT reminder_id, status FROM reminders WHERE task_id = 'SCRUM-105'")
    existing = c.fetchall()
    if existing:
        print(f"[INFO] Test reminders for SCRUM-105 already exist in {db_path}: {existing}")
        conn.close()
        return

    today = datetime.date.today()
    deadline = (today + datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    rem_2d = (today + datetime.timedelta(days=5)).strftime("%Y-%m-%d")
    rem_1d = (today + datetime.timedelta(days=6)).strftime("%Y-%m-%d")
    now_iso = datetime.datetime.now().isoformat()
    cal_id = "test_cal_scrum_105"

    # Seed calendar_events table
    c.execute('''
        INSERT OR REPLACE INTO calendar_events (google_event_id, assignee, email, task, deadline, meeting, status, created_at)
        VALUES (?, 'Priya', 'jeevadhardhinitv23@gmail.com', 'SCRUM-105: Live Jira Webhook Verification', ?, 'Jira Live Test', 'CREATED', ?)
    ''', (cal_id, deadline, now_iso))

    # Seed 2 reminders (LARGE task)
    deliv_1 = f"<meetmind-reminder-SCRUM-105-2d-{rem_2d}@meetmind.ai>"
    c.execute('''
        INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, google_event_id, delivery_identifier, task_id)
        VALUES ('Priya', 'jeevadhardhinitv23@gmail.com', 'SCRUM-105: Live Jira Webhook Verification', ?, 'LARGE', ?, 'Jira Live Test', 'PENDING', ?, ?, ?, 'SCRUM-105')
    ''', (deadline, rem_2d, now_iso, cal_id, deliv_1))

    deliv_2 = f"<meetmind-reminder-SCRUM-105-1d-{rem_1d}@meetmind.ai>"
    c.execute('''
        INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, google_event_id, delivery_identifier, task_id)
        VALUES ('Priya', 'jeevadhardhinitv23@gmail.com', 'SCRUM-105: Live Jira Webhook Verification', ?, 'LARGE', ?, 'Jira Live Test', 'PENDING', ?, ?, ?, 'SCRUM-105')
    ''', (deadline, rem_1d, now_iso, cal_id, deliv_2))

    conn.commit()
    conn.close()
    print(f"[SUCCESS] Seeded 2 test reminders for SCRUM-105 in {db_path} (Deadline: {deadline}, Reminders: {rem_2d}, {rem_1d})")

def inspect_records(db_path: str = DEFAULT_TEST_DB):
    """Display current status of SCRUM-105 and recent webhook activity from target database."""
    if not os.path.exists(db_path):
        print(f"[INFO] Database file does not exist yet: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    print("=" * 70)
    print(f"MEETMIND AI - JIRA WEBHOOK INSPECTION REPORT [{os.path.basename(db_path)}]")
    print(f"Path: {db_path}")
    print("=" * 70)

    print("\n--- [1] Reminders for SCRUM-105 ---")
    c.execute('''
        SELECT reminder_id, assignee, email, deadline, reminder_date, status, task_id, delivery_identifier, last_error
        FROM reminders
        WHERE task_id = 'SCRUM-105' OR task LIKE '%SCRUM-105%'
        ORDER BY reminder_id ASC
    ''')
    rows = c.fetchall()
    if not rows:
        print("  (No reminders found for SCRUM-105)")
    else:
        for r in rows:
            print(f"  ID: {r[0]} | Assignee: {r[1]} ({r[2]}) | Deadline: {r[3]} | RemindDate: {r[4]} | Status: {r[5]}")
            if r[8]:
                print(f"       Note/Error: {r[8]}")

    print("\n--- [2] Calendar Events for SCRUM-105 ---")
    c.execute('''
        SELECT id, google_event_id, assignee, email, deadline, status
        FROM calendar_events
        WHERE task LIKE '%SCRUM-105%' OR google_event_id = 'test_cal_scrum_105'
    ''')
    cal_rows = c.fetchall()
    if not cal_rows:
        print("  (No calendar events found for SCRUM-105)")
    else:
        for cr in cal_rows:
            print(f"  ID: {cr[0]} | GCalID: {cr[1]} | Assignee: {cr[2]} | Deadline: {cr[4]} | Status: {cr[5]}")

    print("\n--- [3] Recent Jira Webhook Audit Log (webhook_events) ---")
    c.execute('''
        SELECT event_id, source, event_type, received_at, processed_at, status, error
        FROM webhook_events
        WHERE source = 'JIRA'
        ORDER BY received_at DESC
        LIMIT 10
    ''')
    wh_rows = c.fetchall()
    if not wh_rows:
        print("  (No Jira webhook events recorded yet)")
    else:
        for wr in wh_rows:
            print(f"  Event: {wr[0]} | Type: {wr[2]} | Received: {wr[3]} | Status: {wr[5]}")
            if wr[6]:
                print(f"       Error: {wr[6]}")

    conn.close()
    print("=" * 70)

def clean_test_records(db_path: str = DEFAULT_TEST_DB, allow_custom: bool = False):
    """Safely delete only SCRUM-105 test records from the isolated test database."""
    validate_db_for_write(db_path, allow_custom_non_prod=allow_custom)
    if not os.path.exists(db_path):
        print(f"[INFO] Database file does not exist: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("DELETE FROM reminders WHERE task_id = 'SCRUM-105'")
    rem_count = c.rowcount
    c.execute("DELETE FROM calendar_events WHERE google_event_id = 'test_cal_scrum_105' OR task LIKE '%SCRUM-105%'")
    cal_count = c.rowcount
    conn.commit()
    conn.close()
    print(f"[SUCCESS] Cleaned up {rem_count} test reminder(s) and {cal_count} test calendar event(s) from {db_path}.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jira Webhook Testing Helper")
    parser.add_argument("action", choices=["seed", "inspect", "clean"], help="Action to perform")
    parser.add_argument("--db", default=DEFAULT_TEST_DB, help=f"Path to database (default: {DEFAULT_TEST_DB})")
    parser.add_argument("--allow-custom-test-db", action="store_true", help="Explicitly allow a non-default test database")
    args = parser.parse_args()

    if args.action == "seed":
        seed_test_record(args.db, allow_custom=args.allow_custom_test_db)
    elif args.action == "inspect":
        inspect_records(args.db)
    elif args.action == "clean":
        clean_test_records(args.db, allow_custom=args.allow_custom_test_db)
