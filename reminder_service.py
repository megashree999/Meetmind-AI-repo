import sqlite3
import datetime
from team_directory import get_assignee_email

DB_NAME = "reminders.db"

def init_reminders_db():
    conn = sqlite3.connect(DB_NAME)
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
            sent_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

def schedule_reminders(action_items, meeting_summary):
    init_reminders_db()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    now_str = datetime.datetime.now().isoformat()
    
    for item in action_items:
        if isinstance(item, dict):
            task = item.get("task", "")
            assignee = item.get("assignee")
            deadline = item.get("deadline")
            task_size = item.get("task_size")
        else:
            task = getattr(item, "task", "")
            assignee = getattr(item, "assignee", None)
            deadline = getattr(item, "deadline", None)
            task_size = getattr(item, "task_size", None)
            
        if not task or not assignee or not deadline:
            continue
            
        # Parse deadline (YYYY-MM-DD)
        try:
            deadline_date = datetime.datetime.strptime(deadline, "%Y-%m-%d").date()
        except Exception as e:
            print(f"[REMINDER] Could not parse deadline '{deadline}': {e}")
            continue
            
        # Deterministic reminder calculation
        size = str(task_size).upper()
        if size == "LARGE":
            days_before = 2
        else:
            days_before = 1
            
        reminder_date = deadline_date - datetime.timedelta(days=days_before)
        
        assignee_name = assignee.strip()
        email = get_assignee_email(assignee_name)
        
        meeting = meeting_summary[:100] if meeting_summary else "Unknown Meeting"
        
        # Avoid duplicates
        c.execute('''
            SELECT reminder_id FROM reminders 
            WHERE assignee=? AND task=? AND deadline=? AND meeting=?
        ''', (assignee_name, task, deadline, meeting))
        if c.fetchone():
            continue
            
        # Check if reminder date is in the past
        today = datetime.datetime.now().date()
        if reminder_date < today:
            print(f"[REMINDER] Reminder date {reminder_date} for '{task}' is in the past. Skipping.")
            status = "SKIPPED"
        else:
            status = "PENDING"
            
        c.execute('''
            INSERT INTO reminders (assignee, email, task, deadline, task_size, reminder_date, meeting, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (assignee_name, email, task, deadline, size, reminder_date.strftime("%Y-%m-%d"), meeting, status, now_str))
        
        print(f"[REMINDER] Scheduled reminder for {assignee_name} on {reminder_date} for task: '{task[:30]}...'")
        
    conn.commit()
    conn.close()
