import sqlite3
import datetime
import os
import re
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger("meetmind.lifecycle")
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(PROJECT_ROOT, "reminders.db")

from team_directory import get_assignee_email
from reminder_service import init_reminders_db

def _normalize_text(text: Optional[str]) -> str:
    if not text:
        return ""
    return re.sub(r'\s+', ' ', str(text)).strip().lower()

def cancel_reminders_for_jira_task(
    task_identifier: str,
    db_path: str = DEFAULT_DB
) -> Dict[str, Any]:
    """
    TASK 4.1: Connect Jira task completion -> Cancel pending reminders.
    When a Jira issue transitions to 'Done'/'Closed'/'Completed',
    cancel any active or pending reminders for that task.
    """
    init_reminders_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()
        norm_id = _normalize_text(task_identifier)
        
        c.execute('''
            SELECT reminder_id, task, status, google_event_id, task_id 
            FROM reminders 
            WHERE status IN ('PENDING', 'PROCESSING', 'FAILED', 'OVERDUE')
        ''')
        rows = c.fetchall()
        
        cancelled_ids = []
        for r_id, r_task, r_status, r_geid, r_tid in rows:
            norm_task = _normalize_text(r_task)
            tid_match = bool(r_tid and str(r_tid).strip().upper() == task_identifier.strip().upper())
            text_match = bool(norm_id in norm_task or norm_task in norm_id or norm_id[:25] == norm_task[:25])
            if tid_match or text_match:
                c.execute('''
                    UPDATE reminders 
                    SET status = 'CANCELLED', last_error = 'Task marked completed in Jira' 
                    WHERE reminder_id = ?
                ''', (r_id,))
                cancelled_ids.append(r_id)
                
        conn.commit()
        logger.info(f"[LIFECYCLE] Cancelled {len(cancelled_ids)} reminder(s) for completed Jira task '{task_identifier}'")
        return {
            "status": "SUCCESS",
            "task_identifier": task_identifier,
            "cancelled_count": len(cancelled_ids),
            "cancelled_reminder_ids": cancelled_ids
        }
    finally:
        conn.close()

def update_jira_deadline_and_reschedule(
    task_identifier: str,
    new_deadline: str,
    db_path: str = DEFAULT_DB,
    calendar_service: Any = None
) -> Dict[str, Any]:
    """
    TASK 4.2: Connect Jira deadline change -> Update Google Calendar event and reschedule pending reminders.
    When a Jira issue due date changes:
    - Update deadline in reminders table.
    - Recalculate deterministic reminder_date based on task_size (LARGE: 2 days, SMALL/MEDIUM: 1 day).
    - If calendar event exists, update Google Calendar event and calendar_events record.
    """
    # Validate deadline format YYYY-MM-DD
    try:
        new_deadline_date = datetime.datetime.strptime(new_deadline.strip(), "%Y-%m-%d").date()
    except Exception as e:
        return {"status": "FAILED", "error": f"Invalid new_deadline date '{new_deadline}': {e}"}

    init_reminders_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()
        norm_id = _normalize_text(task_identifier)
        
        c.execute('''
            SELECT reminder_id, task, task_size, status, google_event_id, reminder_date, task_id 
            FROM reminders 
            WHERE status IN ('PENDING', 'PROCESSING', 'FAILED', 'OVERDUE')
            ORDER BY reminder_id ASC
        ''')
        rows = c.fetchall()
        
        try:
            import zoneinfo
            kolkata_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
        except Exception:
            kolkata_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="Asia/Kolkata")
        today = datetime.datetime.now(kolkata_tz).date()

        updated_ids = []
        updated_calendar_events = []

        # Group matching rows by task
        matching_rows = []
        for r_id, r_task, r_size, r_status, r_geid, r_date, r_tid in rows:
            norm_task = _normalize_text(r_task)
            tid_match = bool(r_tid and str(r_tid).strip().upper() == task_identifier.strip().upper())
            text_match = bool(norm_id in norm_task or norm_task in norm_id or norm_id[:25] == norm_task[:25])
            if tid_match or text_match:
                matching_rows.append((r_id, r_task, r_size, r_status, r_geid, r_date, r_tid))

        # Reschedule matching pending reminders
        for idx, (r_id, r_task, r_size, r_status, r_geid, r_date, r_tid) in enumerate(matching_rows):
            size_str = str(r_size).upper() if r_size else "MEDIUM"
            if size_str == "LARGE":
                # If 2 pending reminders exist for this large task, assign 2-day to first and 1-day to second
                if len(matching_rows) >= 2:
                    offset = 2 if idx == 0 else 1
                else:
                    offset = 1
            else:
                offset = 1

            new_reminder_date = new_deadline_date - datetime.timedelta(days=offset)
            new_reminder_date_str = new_reminder_date.strftime("%Y-%m-%d")
            
            # Re-evaluate status against today in Asia/Kolkata
            if new_deadline_date < today:
                updated_status = "OVERDUE"
            else:
                updated_status = "PENDING"
                
            task_id = r_tid or f"task_{r_id}"
            new_delivery_id = f"<meetmind-reminder-{task_id}-{offset}d-{new_reminder_date_str}@meetmind.ai>"

            c.execute('''
                UPDATE reminders 
                SET deadline = ?,
                    reminder_date = ?,
                    status = ?,
                    delivery_identifier = ?,
                    task_id = COALESCE(task_id, ?),
                    last_error = NULL
                WHERE reminder_id = ?
            ''', (new_deadline, new_reminder_date_str, updated_status, new_delivery_id, task_id, r_id))
            updated_ids.append(r_id)
            
            # Update calendar_events table and Google Calendar if linked (only patch once per unique calendar event)
            if r_geid and not r_geid.startswith("simulated_cal_") and r_geid not in updated_calendar_events:
                c.execute('''
                    UPDATE calendar_events 
                    SET deadline = ? 
                    WHERE google_event_id = ?
                ''', (new_deadline, r_geid))
                    
                if calendar_service:
                    try:
                        # Update Google Calendar API event
                        event_body = {
                            "start": {"date": new_deadline},
                            "end": {"date": new_deadline}
                        }
                        calendar_service.events().patch(
                            calendarId="primary",
                            eventId=r_geid,
                            body=event_body
                        ).execute()
                        updated_calendar_events.append(r_geid)
                        logger.info(f"[LIFECYCLE] Patched Google Calendar event {r_geid} with new deadline {new_deadline}")
                    except Exception as cal_err:
                        logger.warning(f"[LIFECYCLE] Failed to patch Google Calendar event {r_geid}: {cal_err}")
                            
        conn.commit()
        return {
            "status": "SUCCESS",
            "task_identifier": task_identifier,
            "new_deadline": new_deadline,
            "rescheduled_count": len(updated_ids),
            "updated_reminder_ids": updated_ids,
            "updated_calendar_events": updated_calendar_events
        }
    finally:
        conn.close()

def handle_calendar_event_deleted(
    google_event_id: str,
    db_path: str = DEFAULT_DB
) -> Dict[str, Any]:
    """
    TASK 4.3: Connect Calendar event deletion -> Prevent reminder delivery.
    When a Google Calendar event is deleted in Calendar:
    - Mark status in calendar_events as 'DELETED'.
    - Cancel pending reminder so it fails validation and is never dispatched.
    """
    init_reminders_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()
        
        c.execute('''
            UPDATE calendar_events 
            SET status = 'DELETED' 
            WHERE google_event_id = ?
        ''', (google_event_id,))
        cal_affected = c.rowcount
        
        c.execute('''
            UPDATE reminders 
            SET status = 'CANCELLED', last_error = 'Google Calendar event was deleted' 
            WHERE google_event_id = ? AND status IN ('PENDING', 'PROCESSING', 'FAILED', 'OVERDUE')
        ''', (google_event_id,))
        rem_affected = c.rowcount
        
        conn.commit()
        logger.info(f"[LIFECYCLE] Calendar event {google_event_id} marked DELETED, cancelled {rem_affected} reminder(s)")
        return {
            "status": "SUCCESS",
            "google_event_id": google_event_id,
            "calendar_events_updated": cal_affected,
            "reminders_cancelled": rem_affected
        }
    finally:
        conn.close()

def update_reminder_assignee(
    task_identifier: str,
    new_assignee: str,
    db_path: str = DEFAULT_DB
) -> Dict[str, Any]:
    """
    TASK 4.4: Connect Assignee change -> Update recipient mapping and reminder ownership.
    When task owner changes:
    - Look up new recipient email from company_directory.db.
    - Update reminders record with new assignee and verified email.
    - Update calendar_events record with new assignee and verified email.
    """
    new_email = get_assignee_email(new_assignee)
    init_reminders_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()
        norm_id = _normalize_text(task_identifier)
        
        c.execute('''
            SELECT reminder_id, task, google_event_id, task_id 
            FROM reminders 
            WHERE status NOT IN ('COMPLETED', 'CANCELLED', 'SENT_OVERDUE')
        ''')
        rows = c.fetchall()
        
        updated_reminders = []
        for r_id, r_task, r_geid, r_tid in rows:
            norm_task = _normalize_text(r_task)
            tid_match = bool(r_tid and str(r_tid).strip().upper() == task_identifier.strip().upper())
            text_match = bool(norm_id in norm_task or norm_task in norm_id or norm_id[:25] == norm_task[:25])
            if tid_match or text_match:
                c.execute('''
                    UPDATE reminders 
                    SET assignee = ?, email = ? 
                    WHERE reminder_id = ?
                ''', (new_assignee, new_email, r_id))
                updated_reminders.append(r_id)
                
                if r_geid:
                    c.execute('''
                        UPDATE calendar_events 
                        SET assignee = ?, email = ? 
                        WHERE google_event_id = ?
                    ''', (new_assignee, new_email, r_geid))
                    
        conn.commit()
        logger.info(f"[LIFECYCLE] Reassigned task '{task_identifier}' to {new_assignee} ({new_email}) for {len(updated_reminders)} reminder(s)")
        return {
            "status": "SUCCESS",
            "task_identifier": task_identifier,
            "new_assignee": new_assignee,
            "new_email": new_email,
            "updated_reminders": updated_reminders
        }
    finally:
        conn.close()

def update_calendar_event_deadline_and_reschedule(
    google_event_id: str,
    new_deadline: str,
    db_path: str = DEFAULT_DB
) -> Dict[str, Any]:
    """
    REQUIREMENT C: Calendar-originated deadline change.
    When Google Calendar notifies of an updated event deadline/date:
    - Update deadline in calendar_events table.
    - Update deadline and recalculate reminder_date in reminders table.
    - IMPORTANT: DO NOT patch Google Calendar API again (prevents infinite sync loops).
    - Preserves previously SENT reminder history.
    """
    # Validate deadline format YYYY-MM-DD
    try:
        new_deadline_date = datetime.datetime.strptime(new_deadline.strip(), "%Y-%m-%d").date()
    except Exception as e:
        return {"status": "FAILED", "error": f"Invalid new_deadline date '{new_deadline}': {e}"}

    init_reminders_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()
        
        # 1. Update calendar_events
        c.execute('''
            UPDATE calendar_events 
            SET deadline = ? 
            WHERE google_event_id = ?
        ''', (new_deadline, google_event_id))
        cal_affected = c.rowcount

        # 2. Find pending reminders linked to this calendar event
        c.execute('''
            SELECT reminder_id, task, task_size, status, reminder_date, task_id 
            FROM reminders 
            WHERE google_event_id = ? AND status IN ('PENDING', 'PROCESSING', 'FAILED', 'OVERDUE')
            ORDER BY reminder_id ASC
        ''', (google_event_id,))
        matching_rows = c.fetchall()

        try:
            import zoneinfo
            kolkata_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
        except Exception:
            kolkata_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="Asia/Kolkata")
        today = datetime.datetime.now(kolkata_tz).date()

        updated_ids = []
        for idx, (r_id, r_task, r_size, r_status, r_date, r_tid) in enumerate(matching_rows):
            size_str = str(r_size).upper() if r_size else "MEDIUM"
            if size_str == "LARGE":
                if len(matching_rows) >= 2:
                    offset = 2 if idx == 0 else 1
                else:
                    offset = 1
            else:
                offset = 1

            new_reminder_date = new_deadline_date - datetime.timedelta(days=offset)
            new_reminder_date_str = new_reminder_date.strftime("%Y-%m-%d")

            if new_deadline_date < today:
                updated_status = "OVERDUE"
            else:
                updated_status = "PENDING"

            task_id = r_tid or f"task_{r_id}"
            new_delivery_id = f"<meetmind-reminder-{task_id}-{offset}d-{new_reminder_date_str}@meetmind.ai>"

            c.execute('''
                UPDATE reminders 
                SET deadline = ?,
                    reminder_date = ?,
                    status = ?,
                    delivery_identifier = ?,
                    task_id = COALESCE(task_id, ?),
                    last_error = NULL
                WHERE reminder_id = ?
            ''', (new_deadline, new_reminder_date_str, updated_status, new_delivery_id, task_id, r_id))
            updated_ids.append(r_id)

        conn.commit()
        logger.info(f"[LIFECYCLE] Calendar event {google_event_id} rescheduled to {new_deadline} ({len(updated_ids)} reminder(s) updated, loop prevented)")
        return {
            "status": "SUCCESS",
            "google_event_id": google_event_id,
            "new_deadline": new_deadline,
            "calendar_events_updated": cal_affected,
            "rescheduled_count": len(updated_ids),
            "updated_reminder_ids": updated_ids
        }
    finally:
        conn.close()

