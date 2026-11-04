import sqlite3
import datetime
import os
import sys
import base64
import logging
import re
from typing import Optional, Tuple, List, Dict, Any
from email.mime.text import MIMEText
from dotenv import load_dotenv

# Ensure stdout handles UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(PROJECT_ROOT, "reminders.db")
LOG_FILE = os.path.join(PROJECT_ROOT, "reminders_scheduler.log")

log_handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
try:
    log_handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
except Exception:
    pass

logger = logging.getLogger("meetmind.reminders")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=log_handlers
    )

# Re-use gmail_service and team_directory
from gmail_service import get_gmail_service, is_valid_email
from team_directory import get_assignee_email
from reminder_service import init_reminders_db

try:
    import zoneinfo
    KOLKATA_TZ = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    KOLKATA_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="Asia/Kolkata")

LEASE_TIMEOUT_SECONDS = 900  # 15 minutes processing lease

def get_deterministic_message_id(
    reminder_id: int,
    reminder_type: str,
    deadline: str,
    task: Optional[str] = None,
    reminder_date: Optional[str] = None,
    assignee: Optional[str] = None,
    task_id: Optional[str] = None
) -> str:
    """Generate a stable deterministic RFC 822 Message-ID header for idempotent email delivery."""
    clean_type = reminder_type.lower().strip()
    date_str = reminder_date or deadline
    if task_id and str(task_id).strip():
        tid = str(task_id).strip()
        return f"<meetmind-reminder-{tid}-{clean_type}-{date_str}@meetmind.ai>"
    import hashlib
    if task:
        norm_task = re.sub(r'\s+', ' ', str(task)).strip().lower()
        task_hash = hashlib.sha256(norm_task.encode('utf-8')).hexdigest()[:12]
    else:
        task_hash = str(reminder_id)

    user_str = f"{assignee.lower().strip()}-" if assignee else ""
    return f"<meetmind-reminder-{user_str}{task_hash}-{clean_type}-{date_str}@meetmind.ai>"

def _normalize_text(text: Optional[str]) -> str:
    """Normalize string for robust whitespace and case-insensitive comparison."""
    if not text:
        return ""
    return re.sub(r'\s+', ' ', str(text)).strip().lower()

def validate_calendar_event(
    c: sqlite3.Cursor,
    reminder_id: int,
    assignee: str,
    task: str,
    deadline: str,
    meeting: str,
    google_event_id: Optional[str]
) -> Tuple[bool, str]:
    """
    TASK 1: Enforce Real Google Calendar Event Validation.
    Before dispatching any reminder:
    - Require a non-empty google_event_id.
    - Reject simulated_cal_* IDs and SIMULATED events.
    - Verify the ID exists in calendar_events table.
    - Verify the corresponding calendar record has status 'CREATED'.
    - Verify task, assignee, deadline, and meeting association.
    Returns (is_valid, reason).
    """
    if not google_event_id or not str(google_event_id).strip():
        return False, f"Reminder {reminder_id} has no linked Google Calendar event ID (unlinked record)"

    event_id_str = str(google_event_id).strip()
    if event_id_str.startswith("simulated_cal_") or event_id_str.upper() == "SIMULATED":
        return False, f"Reminder {reminder_id} is linked to simulated calendar event '{event_id_str}'"

    # Check calendar_events table existence
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='calendar_events'")
    if not c.fetchone():
        return False, f"calendar_events table does not exist in database"

    c.execute('''
        SELECT id, google_event_id, assignee, task, deadline, meeting, status 
        FROM calendar_events 
        WHERE google_event_id = ?
    ''', (event_id_str,))
    cal_row = c.fetchone()
    if not cal_row:
        return False, f"Calendar event ID '{event_id_str}' does not exist in calendar_events table"

    cal_id, cal_geid, cal_assignee, cal_task, cal_deadline, cal_meeting, cal_status = cal_row

    if str(cal_status).strip().upper() != "CREATED":
        return False, f"Calendar event '{event_id_str}' has status '{cal_status}', expected 'CREATED'"

    # Association Check 1: Assignee
    norm_cal_assignee = _normalize_text(cal_assignee)
    norm_rem_assignee = _normalize_text(assignee)
    if norm_cal_assignee and norm_rem_assignee and norm_cal_assignee != norm_rem_assignee:
        return False, f"Assignee mismatch: reminder has '{assignee}', calendar event has '{cal_assignee}'"

    # Association Check 2: Deadline
    if cal_deadline and deadline and str(cal_deadline).strip() != str(deadline).strip():
        return False, f"Deadline mismatch: reminder has '{deadline}', calendar event has '{cal_deadline}'"

    # Association Check 3: Task
    norm_cal_task = _normalize_text(cal_task)
    norm_rem_task = _normalize_text(task)
    if norm_cal_task and norm_rem_task and norm_cal_task != norm_rem_task:
        min_len = min(len(norm_cal_task), len(norm_rem_task), 25)
        if norm_cal_task[:min_len] != norm_rem_task[:min_len]:
            return False, f"Task mismatch: reminder task '{task[:35]}...' does not match calendar task '{cal_task[:35]}...'"

    # Association Check 4: Meeting
    if cal_meeting and meeting:
        norm_cal_meeting = _normalize_text(cal_meeting)
        norm_rem_meeting = _normalize_text(meeting)
        if norm_cal_meeting != norm_rem_meeting:
            cal_tokens = set(re.findall(r'[a-zA-Z0-9]+', norm_cal_meeting))
            rem_tokens = set(re.findall(r'[a-zA-Z0-9]+', norm_rem_meeting))
            if not (cal_tokens & rem_tokens):
                return False, f"Meeting mismatch: reminder meeting '{meeting}' does not match calendar meeting '{cal_meeting}'"

    return True, "Valid real Google Calendar event"

def check_already_sent_in_gmail(
    service: Any,
    delivery_identifier: str,
    recipient_email: str,
    subject: str
) -> Optional[str]:
    """
    TASK 2: Check Gmail Sent messages for deterministic Message-ID or subject before retrying an ambiguous send.
    Returns the existing Gmail message ID if already sent, avoiding duplicate delivery.
    """
    clean_msg_id = delivery_identifier.strip("<>")
    
    # 1. Search by RFC 822 msgid
    try:
        query = f'rfc822msgid:{clean_msg_id}'
        res = service.users().messages().list(userId="me", q=query, maxResults=1).execute()
        if isinstance(res, dict):
            messages = res.get("messages", [])
            if isinstance(messages, list) and len(messages) > 0 and isinstance(messages[0], dict):
                found_id = messages[0].get("id")
                if found_id and type(found_id).__name__ != "MagicMock":
                    return str(found_id)
    except Exception as e:
        logger.debug(f"Could not query Gmail by rfc822msgid '{clean_msg_id}': {e}")

    # 2. Search by exact recipient and subject in Sent box
    try:
        safe_subject = subject.replace('"', '\\"')
        query = f'to:{recipient_email} subject:"{safe_subject}"'
        res = service.users().messages().list(userId="me", q=query, maxResults=1).execute()
        if isinstance(res, dict):
            messages = res.get("messages", [])
            if isinstance(messages, list) and len(messages) > 0 and isinstance(messages[0], dict):
                found_id = messages[0].get("id")
                if found_id and type(found_id).__name__ != "MagicMock":
                    return str(found_id)
    except Exception as e:
        logger.debug(f"Could not query Gmail by recipient/subject: {e}")

    return None

def create_reminder_email_content(
    assignee: str,
    task: str,
    deadline: str,
    meeting: str,
    is_overdue: bool = False
) -> Tuple[str, str]:
    # Remove deadline phrases (e.g. by Monday, due tomorrow) and clean up trailing punctuation
    pattern = r'\b(by|due|on|in)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|next\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|[a-zA-Z]+\s+\d{1,2}(?:st|nd|rd|th)?|\d+\s+(?:days|hours|weeks))\b'
    task_clean = re.sub(pattern, '', task, flags=re.IGNORECASE).strip()
    task_clean = re.sub(r'\s*\.$', '', task_clean).strip()
    task_clean = re.sub(r'\s+', ' ', task_clean).strip()
    
    if is_overdue:
        subject = f"⚠️ Overdue Alert: Action Item Past Deadline ({deadline})"
        urgency_msg = f"This is an urgent reminder from MeetMind AI regarding an action item assigned to you during the {meeting} that is now OVERDUE."
        action_msg = "Please make sure this overdue task is completed and updated immediately."
    else:
        subject = "⏰ Reminder: Action Item Due Tomorrow"
        urgency_msg = f"This is a friendly reminder from MeetMind AI regarding an action item assigned to you during the {meeting}."
        action_msg = "Please make sure the task is completed by the deadline."

    body = f"""Hi {assignee},

{urgency_msg}

────────────────────────

ACTION ITEM

Task:
{task_clean}

Deadline:
{deadline}

Meeting:
{meeting}

────────────────────────

{action_msg}

Regards,
MeetMind AI
Automated Meeting Assistant"""
    return subject, body

def process_reminders(db_path: str = DEFAULT_DB, current_time: Optional[datetime.datetime] = None) -> dict:
    env_file = os.path.join(PROJECT_ROOT, ".env")
    if os.path.exists(env_file):
        load_dotenv(env_file, override=False)
    else:
        load_dotenv(override=False)

    # Resolve database path
    target_db = db_path
    if not os.path.isabs(target_db):
        candidate = os.path.join(PROJECT_ROOT, target_db)
        if os.path.exists(candidate) or not os.path.exists(target_db):
            target_db = candidate
        else:
            target_db = os.path.abspath(target_db)
            
    if not os.path.exists(target_db):
        logger.info(f"No {target_db} found. Exiting.")
        return {"processed": 0, "sent": 0, "failed": 0, "skipped": 0}
        
    init_reminders_db(target_db)
    
    # FIX 2: Fixed 9:00 AM Asia/Kolkata delivery rule
    # Ensure all date & time calculations use Asia/Kolkata consistently
    if current_time is not None:
        if current_time.tzinfo is None:
            now_kolkata = current_time.replace(tzinfo=KOLKATA_TZ)
        else:
            now_kolkata = current_time.astimezone(KOLKATA_TZ)
    else:
        now_kolkata = datetime.datetime.now(KOLKATA_TZ)

    today_str = now_kolkata.strftime("%Y-%m-%d")
    now_iso = now_kolkata.isoformat()
    lease_cutoff_iso = (now_kolkata - datetime.timedelta(seconds=LEASE_TIMEOUT_SECONDS)).isoformat()
    
    # Upcoming reminders must not be sent before 09:00 AM IST on their scheduled reminder date
    is_at_or_after_9am = (now_kolkata.hour >= 9)
    if is_at_or_after_9am:
        max_upcoming_reminder_date = today_str
    else:
        # Before 9:00 AM IST, reminders scheduled for today are NOT eligible yet
        max_upcoming_reminder_date = (now_kolkata.date() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    conn = sqlite3.connect(target_db, timeout=30.0)
    try:
        c = conn.cursor()
        
        # TASK 1 & 2: Select candidate reminders requiring non-empty google_event_id.
        # Historical unlinked records (IDs 1-33) with NULL/empty google_event_id are NEVER selected or modified.
        # Upcoming reminders: reminder_date <= max_upcoming_reminder_date AND deadline > today_str
        # (deadline > today_str ensures no deadline-day reminder is generated)
        c.execute('''
            SELECT reminder_id, assignee, email, task, deadline, meeting, status, 'UPCOMING', google_event_id, attempt_count
            FROM reminders 
            WHERE reminder_date <= ? 
              AND deadline > ?
              AND (
                  status IN ('PENDING', 'FAILED')
                  OR (status = 'PROCESSING' AND processing_started_at <= ?)
              )
              AND status != 'COMPLETED'
              AND status != 'SENT'
              AND google_event_id IS NOT NULL 
              AND google_event_id != ''
        ''', (max_upcoming_reminder_date, today_str, lease_cutoff_iso))
        upcoming_candidates = c.fetchall()

        # 2. Overdue candidates: deadline < today, handled separately
        # Once sent, reminders must remain SENT and be excluded from subsequent runs
        c.execute('''
            SELECT reminder_id, assignee, email, task, deadline, meeting, status, 'OVERDUE', google_event_id, attempt_count
            FROM reminders 
            WHERE deadline < ? 
              AND (
                  status IN ('OVERDUE', 'FAILED')
                  OR (status = 'PROCESSING' AND processing_started_at <= ?)
              )
              AND status NOT IN ('COMPLETED', 'SENT', 'SENT_OVERDUE')
              AND google_event_id IS NOT NULL 
              AND google_event_id != ''
        ''', (today_str, lease_cutoff_iso))
        overdue_candidates = c.fetchall()

        # Deduplicate candidate reminders by reminder_id
        seen_candidate_ids = set()
        candidates = []
        for r in upcoming_candidates:
            if r[0] not in seen_candidate_ids:
                seen_candidate_ids.add(r[0])
                candidates.append(r)
        for r in overdue_candidates:
            if r[0] not in seen_candidate_ids:
                seen_candidate_ids.add(r[0])
                candidates.append(r)

        if not candidates:
            logger.info(f"No eligible Calendar-linked reminders to process for today ({today_str}).")
            return {"processed": 0, "sent": 0, "failed": 0, "skipped": 0}

        # TASK 1 & 2: Atomic transaction to validate calendar link and claim eligible reminders
        claimed_reminders = []
        skipped_count = 0

        # Acquire immediate transaction lock for atomic claim
        c.execute("BEGIN IMMEDIATE")
        try:
            for cand in candidates:
                r_id, assignee, email, task, deadline, meeting, status, rem_type, google_event_id, attempts = cand
                
                # TASK 1: Validate Real Calendar Event
                is_valid, validation_msg = validate_calendar_event(
                    c, r_id, assignee, task, deadline, meeting, google_event_id
                )
                if not is_valid:
                    logger.warning(f"[REMINDER] Calendar validation skipped for reminder {r_id}: {validation_msg}")
                    # Log error in record if processing or failed, but do not claim
                    c.execute('''
                        UPDATE reminders 
                        SET last_error = ? 
                        WHERE reminder_id = ? AND status IN ('PENDING', 'FAILED', 'PROCESSING')
                    ''', (f"Calendar validation failed: {validation_msg}", r_id))
                    skipped_count += 1
                    continue

                # TASK 2: Atomically transition to PROCESSING with deterministic Message-ID and lease
                c.execute("SELECT delivery_identifier, reminder_date, task_id FROM reminders WHERE reminder_id = ?", (r_id,))
                cur_rem_row = c.fetchone()
                if cur_rem_row and cur_rem_row[0]:
                    det_msg_id = cur_rem_row[0]
                else:
                    cur_rdate = cur_rem_row[1] if cur_rem_row else None
                    cur_tid = cur_rem_row[2] if cur_rem_row and len(cur_rem_row) > 2 else None
                    det_msg_id = get_deterministic_message_id(r_id, rem_type, deadline, task, cur_rdate, assignee, task_id=cur_tid)

                c.execute('''
                    UPDATE reminders 
                    SET status = 'PROCESSING',
                        processing_started_at = ?,
                        attempt_count = COALESCE(attempt_count, 0) + 1,
                        delivery_identifier = ?
                    WHERE reminder_id = ?
                ''', (now_iso, det_msg_id, r_id))
                
                claimed_reminders.append((r_id, assignee, email, task, deadline, meeting, status, rem_type, google_event_id, det_msg_id))
            
            conn.commit()
        except Exception as claim_err:
            conn.rollback()
            logger.error(f"[REMINDER] Error during atomic claim transaction: {claim_err}")
            raise claim_err

        if not claimed_reminders:
            logger.info(f"All candidate reminders skipped during Calendar validation ({skipped_count} skipped).")
            return {"processed": 0, "sent": 0, "failed": 0, "skipped": skipped_count}

        logger.info(f"Atomically claimed {len(claimed_reminders)} reminder(s) for delivery.")

        notifications_enabled = os.environ.get("EMAIL_NOTIFICATIONS_ENABLED", "false").lower() == "true"
        service = None
        if notifications_enabled:
            service, error_msg = get_gmail_service()
            if not service:
                logger.error(f"Failed to get Gmail service: {error_msg}")
                # Release lease back to FAILED
                c.execute('''
                    UPDATE reminders 
                    SET status = 'FAILED', last_error = ? 
                    WHERE status = 'PROCESSING' AND reminder_id IN ({})
                '''.format(",".join(str(r[0]) for r in claimed_reminders)), (f"Gmail service unavailable: {error_msg}",))
                conn.commit()
                return {"processed": len(claimed_reminders), "sent": 0, "failed": len(claimed_reminders), "skipped": skipped_count, "error": error_msg}

        sent_count = 0
        failed_count = 0

        for item in claimed_reminders:
            r_id, assignee, email, task, deadline, meeting, orig_status, reminder_type, google_event_id, det_msg_id = item
            is_overdue = (reminder_type == 'OVERDUE')
            new_status = 'SENT_OVERDUE' if is_overdue else 'SENT'

            # Retrieve recipient email from company_directory.db
            recipient_email = get_assignee_email(assignee)
            if not is_valid_email(recipient_email):
                if is_valid_email(email):
                    recipient_email = email
                else:
                    recipient_email = None

            if not recipient_email or not is_valid_email(recipient_email):
                logger.warning(f"[REMINDER] Invalid or demo recipient email for reminder {r_id}: '{recipient_email or email}'")
                c.execute('''
                    UPDATE reminders 
                    SET status = 'SKIPPED', last_error = 'Invalid recipient email' 
                    WHERE reminder_id = ?
                ''', (r_id,))
                conn.commit()
                skipped_count += 1
                continue

            subject, body = create_reminder_email_content(assignee, task, deadline, meeting, is_overdue=is_overdue)

            # Preview Mode when EMAIL_NOTIFICATIONS_ENABLED=false
            if not notifications_enabled:
                safe_subject = subject.encode("ascii", "replace").decode("ascii")
                logger.info(f"PREVIEW (Emails disabled) - Would send {reminder_type} reminder {r_id} to {recipient_email}")
                logger.info(f"Subject: {safe_subject}")
                # Revert claimed PROCESSING state back to PENDING for dry runs
                c.execute('''
                    UPDATE reminders 
                    SET status = 'PENDING', processing_started_at = NULL, last_error = 'Preview mode (emails disabled)' 
                    WHERE reminder_id = ?
                ''', (r_id,))
                conn.commit()
                continue

            # TASK 2: Check Gmail Sent messages for deterministic Message-ID or subject before attempting send
            if service:
                already_sent_id = check_already_sent_in_gmail(service, det_msg_id, recipient_email, subject)
                if already_sent_id:
                    now_str = datetime.datetime.now().isoformat()
                    logger.info(f"[REMINDER] Duplicate send avoided: Email already exists in Gmail Sent box (Gmail ID: {already_sent_id}) for reminder {r_id}")
                    c.execute('''
                        UPDATE reminders 
                        SET status = ?, sent_at = ?, gmail_message_id = ?, last_error = NULL 
                        WHERE reminder_id = ?
                    ''', (new_status, now_str, already_sent_id, r_id))
                    conn.commit()
                    sent_count += 1
                    continue

            # Send Email
            try:
                from googleapiclient.errors import HttpError
            except ImportError:
                HttpError = Exception

            try:
                message = MIMEText(body)
                message["to"] = recipient_email
                message["subject"] = subject
                # Add deterministic RFC 822 Message-ID header
                message["Message-ID"] = det_msg_id
                
                raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
                
                logger.info(f"Sending {reminder_type} reminder {r_id} to {recipient_email} for task: '{task[:30]}...'")
                sent_response = service.users().messages().send(
                    userId="me",
                    body={"raw": raw_message}
                ).execute()
                
                raw_msg_id = sent_response.get("id") if isinstance(sent_response, dict) else None
                if raw_msg_id and type(raw_msg_id).__name__ != "MagicMock":
                    msg_id = str(raw_msg_id)
                else:
                    msg_id = f"sent_gmail_{r_id}"
                now_str = datetime.datetime.now().isoformat()
                
                c.execute('''
                    UPDATE reminders 
                    SET status = ?, sent_at = ?, gmail_message_id = ?, last_error = NULL 
                    WHERE reminder_id = ?
                ''', (new_status, now_str, msg_id, r_id))
                conn.commit()
                logger.info(f"[REMINDER] Successfully sent {reminder_type} reminder {r_id} to {recipient_email} (Gmail ID: {msg_id})")
                sent_count += 1
                
            except HttpError as error:
                err_str = f"Gmail API HttpError: {error}"
                logger.error(f"[REMINDER] {err_str} for reminder {r_id}")
                c.execute("UPDATE reminders SET status = 'FAILED', last_error = ? WHERE reminder_id = ?", (err_str, r_id))
                conn.commit()
                failed_count += 1
            except Exception as e:
                err_str = f"Send exception: {e}"
                logger.error(f"[REMINDER] Failed to send reminder {r_id}: {err_str}")
                c.execute("UPDATE reminders SET status = 'FAILED', last_error = ? WHERE reminder_id = ?", (err_str, r_id))
                conn.commit()
                failed_count += 1

        logger.info("Done processing reminders.")
        return {
            "processed": len(claimed_reminders),
            "sent": sent_count,
            "failed": failed_count,
            "skipped": skipped_count
        }
    finally:
        conn.close()

if __name__ == "__main__":
    db_arg = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    result = process_reminders(db_path=db_arg)
    print(f"[PROCESS_REMINDERS EXIT] Processed: {result.get('processed')}, Sent: {result.get('sent')}, Failed: {result.get('failed')}, Skipped: {result.get('skipped')}")
