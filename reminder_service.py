import sqlite3
import datetime
from typing import Optional, List, Dict, Any
from team_directory import get_assignee_email

DB_NAME = "reminders.db"

def init_reminders_db(db_path: str = DB_NAME):
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                reminder_id INTEGER PRIMARY KEY AUTOINCREMENT,
                assignee TEXT,
                email TEXT,
                task TEXT,
                deadline TEXT,
                task_size TEXT,
                reminder_date TEXT,
                meeting TEXT,
                status TEXT,
                created_at TEXT,
                sent_at TEXT,
                google_event_id TEXT,
                gmail_message_id TEXT
            )
        ''')
        # Check and migrate columns if table already exists
        c.execute("PRAGMA table_info(reminders)")
        columns = [col[1] for col in c.fetchall()]
        if "google_event_id" not in columns:
            c.execute("ALTER TABLE reminders ADD COLUMN google_event_id TEXT")
        if "gmail_message_id" not in columns:
            c.execute("ALTER TABLE reminders ADD COLUMN gmail_message_id TEXT")
        if "processing_started_at" not in columns:
            c.execute("ALTER TABLE reminders ADD COLUMN processing_started_at TEXT")
        if "attempt_count" not in columns:
            c.execute("ALTER TABLE reminders ADD COLUMN attempt_count INTEGER DEFAULT 0")
        if "delivery_identifier" not in columns:
            c.execute("ALTER TABLE reminders ADD COLUMN delivery_identifier TEXT")
        if "last_error" not in columns:
            c.execute("ALTER TABLE reminders ADD COLUMN last_error TEXT")
        if "task_id" not in columns:
            c.execute("ALTER TABLE reminders ADD COLUMN task_id TEXT")

        # Safe migration for existing legacy records: assign stable task_legacy_{id} if task_id is NULL
        c.execute('''
            UPDATE reminders 
            SET task_id = 'task_legacy_' || reminder_id 
            WHERE task_id IS NULL
        ''')

        c.execute('''
            CREATE TABLE IF NOT EXISTS calendar_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                google_event_id TEXT UNIQUE,
                assignee TEXT,
                email TEXT,
                task TEXT,
                deadline TEXT,
                meeting TEXT,
                task_size TEXT,
                html_link TEXT,
                status TEXT,
                created_at TEXT
            )
        ''')

        # Webhook event deduplication and audit trail table
        c.execute('''
            CREATE TABLE IF NOT EXISTS webhook_events (
                event_id TEXT PRIMARY KEY,
                source TEXT,
                event_type TEXT,
                payload_hash TEXT,
                received_at TEXT,
                processed_at TEXT,
                status TEXT,
                error TEXT
            )
        ''')

        # Google Calendar push notification watch channels table
        c.execute('''
            CREATE TABLE IF NOT EXISTS calendar_watch_channels (
                channel_id TEXT PRIMARY KEY,
                resource_id TEXT,
                resource_uri TEXT,
                token TEXT,
                expiration TEXT,
                created_at TEXT,
                status TEXT,
                webhook_url TEXT
            )
        ''')
        c.execute("PRAGMA table_info(calendar_watch_channels)")
        ch_columns = [col[1] for col in c.fetchall()]
        if "webhook_url" not in ch_columns:
            c.execute("ALTER TABLE calendar_watch_channels ADD COLUMN webhook_url TEXT")

        # Google Calendar incremental synchronization state table
        c.execute('''
            CREATE TABLE IF NOT EXISTS calendar_sync_state (
                channel_id TEXT PRIMARY KEY,
                sync_token TEXT,
                last_synced_at TEXT
            )
        ''')
        conn.commit()
    finally:
        conn.close()

def schedule_reminders(action_items: List[Any], meeting_summary: Optional[str] = None, db_path: str = DB_NAME, google_event_id: Optional[str] = None):
    init_reminders_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()
        now_str = datetime.datetime.now().isoformat()
        
        for item in action_items:
            if isinstance(item, dict):
                task = item.get("task", "")
                assignee = item.get("assignee")
                deadline = item.get("deadline")
                task_size = item.get("task_size")
                item_event_id = item.get("google_event_id")
            else:
                task = getattr(item, "task", "")
                assignee = getattr(item, "assignee", None)
                deadline = getattr(item, "deadline", None)
                task_size = getattr(item, "task_size", None)
                item_event_id = getattr(item, "google_event_id", None)
                
            if not task or not assignee or not deadline:
                continue
                
            # Parse deadline (YYYY-MM-DD)
            try:
                deadline_date = datetime.datetime.strptime(deadline, "%Y-%m-%d").date()
            except Exception as e:
                print(f"[REMINDER] Could not parse deadline '{deadline}': {e}")
                continue
                
            event_id = item_event_id or google_event_id
            if event_id and (event_id.startswith("simulated_cal_") or event_id == "SIMULATED"):
                print(f"[REMINDER] Skipping simulated calendar event for task '{task[:30]}...'")
                continue

            assignee_name = assignee.strip()
            meeting = (meeting_summary or "Unknown Meeting")[:100]

            # Check if matching calendar event is simulated in calendar_events table
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='calendar_events'")
            if c.fetchone():
                c.execute('''
                    SELECT google_event_id, status FROM calendar_events 
                    WHERE assignee=? AND task=? AND deadline=?
                ''', (assignee_name, task, deadline))
                cal_row = c.fetchone()
                if cal_row:
                    cal_id, cal_status = cal_row
                    if cal_status == "SIMULATED" or (cal_id and cal_id.startswith("simulated_cal_")):
                        print(f"[REMINDER] Skipping reminder scheduling for simulated Calendar event: '{task[:30]}...'")
                        continue
                    if cal_status == "CREATED" and cal_id and not event_id:
                        event_id = cal_id

            # Deterministic reminder calculation using Asia/Kolkata timezone
            # SMALL/MEDIUM: 1 reminder, deadline minus 1 day
            # LARGE: 2 separate reminders: deadline minus 2 days AND deadline minus 1 day
            # No deadline-day reminder is ever generated
            try:
                import zoneinfo
                kolkata_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
            except Exception:
                kolkata_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="Asia/Kolkata")
            today = datetime.datetime.now(kolkata_tz).date()

            size = str(task_size).upper() if task_size else "MEDIUM"
            if size == "LARGE":
                day_offsets = [2, 1]
            else:
                day_offsets = [1]

            email = get_assignee_email(assignee_name)

            # Persistent task_id resolution:
            # 1. Use Jira issue key if provided in action item
            jira_key = None
            if isinstance(item, dict):
                jira_key = item.get("jira_issue_key") or item.get("jira_key") or item.get("issue_key") or item.get("task_id")
            else:
                jira_key = getattr(item, "jira_issue_key", None) or getattr(item, "jira_key", None) or getattr(item, "issue_key", None) or getattr(item, "task_id", None)

            if jira_key and str(jira_key).strip():
                task_id = str(jira_key).strip().upper()
            else:
                task_id = None
                # Check if calendar event was previously mapped to a task_id
                if event_id:
                    c.execute('''
                        SELECT task_id FROM reminders 
                        WHERE google_event_id=? AND task_id IS NOT NULL
                        LIMIT 1
                    ''', (event_id,))
                    existing_by_cal = c.fetchone()
                    if existing_by_cal and existing_by_cal[0]:
                        task_id = existing_by_cal[0]

                # 2. Check if this task was previously inserted in the same meeting (reuse same task_id when transcript reprocessed)
                if not task_id:
                    c.execute('''
                        SELECT task_id FROM reminders 
                        WHERE assignee=? AND task=? AND deadline=? AND meeting=? AND task_id IS NOT NULL
                        LIMIT 1
                    ''', (assignee_name, task, deadline, meeting))
                    existing_tid_row = c.fetchone()
                    if existing_tid_row and existing_tid_row[0]:
                        task_id = existing_tid_row[0]
                    else:
                        # 3. Generate unique UUID once upon first insertion
                        import uuid
                        task_id = f"task_{uuid.uuid4().hex[:12]}"

            for offset in day_offsets:
                reminder_date = deadline_date - datetime.timedelta(days=offset)
                reminder_date_str = reminder_date.strftime("%Y-%m-%d")
                
                # Check status against today in Asia/Kolkata
                if deadline_date < today:
                    status = "OVERDUE"
                    reminder_date_str = today.strftime("%Y-%m-%d")
                elif deadline_date == today:
                    status = "PENDING"
                    reminder_date_str = today.strftime("%Y-%m-%d")
                elif reminder_date < today:
                    status = "PENDING"
                    reminder_date_str = today.strftime("%Y-%m-%d")
                else:
                    status = "PENDING"

                # Generate deterministic delivery identifier based on persistent task_id, reminder offset, and scheduled date
                delivery_id = f"<meetmind-reminder-{task_id}-{offset}d-{reminder_date_str}@meetmind.ai>"

                # Avoid duplicate reminders for the same meeting action item and specific reminder date
                c.execute('''
                    SELECT reminder_id, google_event_id, status FROM reminders 
                    WHERE assignee=? AND task=? AND deadline=? AND meeting=? AND reminder_date=?
                ''', (assignee_name, task, deadline, meeting, reminder_date_str))
                existing_row = c.fetchone()
                if existing_row:
                    # Update google_event_id and task_id if previously missing
                    if event_id and not existing_row[1]:
                        c.execute('''
                            UPDATE reminders 
                            SET google_event_id = ? 
                            WHERE reminder_id = ?
                        ''', (event_id, existing_row[0]))
                    c.execute('''
                        UPDATE reminders 
                        SET task_id = ? 
                        WHERE reminder_id = ? AND task_id IS NULL
                    ''', (task_id, existing_row[0]))
                    continue

                c.execute('''
                    INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at, google_event_id, delivery_identifier, task_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (assignee_name, email, task, deadline, size, reminder_date_str, meeting, status, now_str, event_id, delivery_id, task_id))

                print(f"[REMINDER] Scheduled reminder for {assignee_name} on {reminder_date_str} ({offset}d before deadline) for task: '{task[:30]}...' (task_id: {task_id}, calendar_id: {event_id})")
            
        conn.commit()
    finally:
        conn.close()
