import os
import sys
import re
import datetime
import sqlite3
import logging
from typing import Optional, Dict, Any, List, Tuple
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=False)

logger = logging.getLogger("meetmind.calendar")

# Required Google Calendar OAuth Scope
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
DEFAULT_DB = "reminders.db"

# ---------------------------------------------------------------------------
# 1. DATABASE & SCHEMA MANAGEMENT
# ---------------------------------------------------------------------------

def init_calendar_db(db_path: str = DEFAULT_DB) -> None:
    """
    Ensure SQLite database has required tables and columns for Google Calendar tracking.
    Safely adds 'google_event_id' column to 'reminders' table if present, and creates
    the 'calendar_events' table for independent tracking.
    """
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()
        
        # 1. Extend existing 'reminders' table with google_event_id if it exists
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='reminders'")
        if c.fetchone():
            c.execute("PRAGMA table_info(reminders)")
            columns = [col[1] for col in c.fetchall()]
            if "google_event_id" not in columns:
                c.execute("ALTER TABLE reminders ADD COLUMN google_event_id TEXT")
        
        # 2. Create standalone 'calendar_events' table for full event history
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
        conn.commit()
    finally:
        conn.close()

def is_duplicate_calendar_event(
    assignee: str,
    task: str,
    deadline: str,
    meeting: Optional[str] = None,
    db_path: str = DEFAULT_DB
) -> Optional[str]:
    """
    Check if a Google Calendar event was already created for this action item.
    Returns the existing google_event_id if found, otherwise None.
    """
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()
        clean_assignee = (assignee or "").strip()
        clean_task = (task or "").strip()
        clean_deadline = (deadline or "").strip()
        clean_meeting = (meeting or "")[:100].strip()

        # Check calendar_events table (ignore SIMULATED records and simulated IDs)
        c.execute('''
            SELECT google_event_id FROM calendar_events
            WHERE assignee=? AND task=? AND deadline=? AND meeting=?
              AND google_event_id IS NOT NULL
              AND (status IS NULL OR UPPER(status) != 'SIMULATED')
              AND google_event_id NOT LIKE 'simulated_cal_%'
        ''', (clean_assignee, clean_task, clean_deadline, clean_meeting))
        row = c.fetchone()
        if row and row[0]:
            return row[0]

        # Also check reminders table if it exists (ignore simulated IDs)
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='reminders'")
        if c.fetchone():
            c.execute("PRAGMA table_info(reminders)")
            columns = [col[1] for col in c.fetchall()]
            if "google_event_id" in columns:
                c.execute('''
                    SELECT google_event_id FROM reminders
                    WHERE assignee=? AND task=? AND deadline=?
                      AND google_event_id IS NOT NULL
                      AND google_event_id NOT LIKE 'simulated_cal_%'
                ''', (clean_assignee, clean_task, clean_deadline))
                row = c.fetchone()
                if row and row[0]:
                    return row[0]

        return None
    finally:
        conn.close()

def save_calendar_event_record(
    google_event_id: str,
    assignee: str,
    email: str,
    task: str,
    deadline: str,
    meeting: Optional[str] = None,
    task_size: Optional[str] = None,
    html_link: str = "",
    status: str = "CREATED",
    db_path: str = DEFAULT_DB
) -> None:
    """
    Save the created Google Calendar event ID in SQLite to prevent duplicate events.
    Updates both 'calendar_events' and 'reminders' tables.
    """
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()
        now_str = datetime.datetime.now().isoformat()
        clean_assignee = (assignee or "").strip()
        clean_task = (task or "").strip()
        clean_deadline = (deadline or "").strip()
        clean_meeting = (meeting or "")[:100].strip()

        # 1. Insert into calendar_events
        c.execute('''
            INSERT OR REPLACE INTO calendar_events 
            (google_event_id, assignee, email, task, deadline, meeting, task_size, html_link, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            google_event_id,
            clean_assignee,
            email or "",
            clean_task,
            clean_deadline,
            clean_meeting,
            task_size or "MEDIUM",
            html_link or "",
            status,
            now_str
        ))

        # 2. Update matching row in reminders table if real event (do not link simulated events)
        if status != "SIMULATED" and not google_event_id.startswith("simulated_cal_"):
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='reminders'")
            if c.fetchone():
                c.execute("PRAGMA table_info(reminders)")
                columns = [col[1] for col in c.fetchall()]
                if "google_event_id" in columns:
                    c.execute('''
                        UPDATE reminders 
                        SET google_event_id = ? 
                        WHERE assignee=? AND task=? AND deadline=?
                    ''', (google_event_id, clean_assignee, clean_task, clean_deadline))
                    c.execute('''
                        SELECT reminder_id FROM reminders 
                        WHERE assignee=? AND task=? AND deadline=?
                    ''', (clean_assignee, clean_task, clean_deadline))
                    if not c.fetchone():
                        from reminder_service import schedule_reminders
                        schedule_reminders([{
                            "task": clean_task,
                            "assignee": clean_assignee,
                            "deadline": clean_deadline,
                            "task_size": task_size or "MEDIUM",
                            "google_event_id": google_event_id
                        }], meeting_summary=clean_meeting, db_path=db_path)

        conn.commit()
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# 2. OAUTH 2.0 AUTHENTICATION & SERVICE INITIALIZATION
# ---------------------------------------------------------------------------

def get_calendar_service() -> Tuple[Optional[Any], str]:
    """
    Initialize Google Calendar API service with OAuth 2.0.
    Handles token retrieval, expiration, and refresh safely without exposing tokens.
    
    Returns:
        Tuple of (service_instance or None, status_message)
    """
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as e:
        return None, f"Google API dependencies not installed: {e}"

    creds_file = (
        os.environ.get("GOOGLE_CREDENTIALS_FILE") or 
        os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
    )
    
    # Priority: dedicated token_calendar.json, then token.json
    calendar_token_file = os.environ.get("GOOGLE_CALENDAR_TOKEN_FILE", "token_calendar.json")
    primary_token_file = os.environ.get("GMAIL_TOKEN_FILE", "token.json")

    chosen_token_file = None
    if os.path.exists(calendar_token_file):
        chosen_token_file = calendar_token_file
    elif os.path.exists(primary_token_file):
        chosen_token_file = primary_token_file

    creds = None

    if chosen_token_file and os.path.exists(chosen_token_file):
        try:
            creds = Credentials.from_authorized_user_file(chosen_token_file, CALENDAR_SCOPES)
        except Exception as e:
            logger.warning(f"Failed to load Calendar credentials from {chosen_token_file}: {e}")
            creds = None

    # Handle token refresh if expired
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Save back refreshed token safely
            save_path = chosen_token_file or calendar_token_file
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
            logger.info("Successfully refreshed Google Calendar OAuth credentials.")
        except Exception as e:
            return None, f"OAuth token refresh failed: {str(e)}"

    if not creds or not creds.valid:
        if not os.path.exists(creds_file):
            return None, f"Google credentials missing: client secrets file '{creds_file}' not found."
        return None, (
            f"Google Calendar OAuth token missing or expired. "
            f"Run 'python authorize_calendar.py' to generate '{calendar_token_file}'."
        )

    try:
        service = build("calendar", "v3", credentials=creds)
        return service, "Google Calendar service ready"
    except Exception as e:
        return None, f"Google Calendar API service initialization failed: {str(e)}"

# ---------------------------------------------------------------------------
# 3. DEADLINE & FIELD PARSING UTILITIES
# ---------------------------------------------------------------------------

WEEKDAY_NAME_MAP = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6
}

def resolve_deterministic_deadline(
    task_text: Optional[str] = None,
    ref_date: Optional[Any] = None,
    extracted_deadline: Optional[str] = None
) -> Optional[str]:
    """
    Deterministically resolve deadlines using a reference date and temporal keywords.
    Never relies on LLM arithmetic for weekday calculations.
    
    Priority:
    1. Deterministic weekday / relative expression in task_text (e.g. 'Friday', 'Thursday', 'Monday', 'tomorrow').
    2. Valid ISO YYYY-MM-DD in task_text or extracted_deadline (validated against ref_date).
    3. None if no deadline or missing/filler values.
    """
    # 1. Resolve reference date
    reference: datetime.date
    if ref_date is None:
        reference = datetime.date.today()
    elif isinstance(ref_date, datetime.datetime):
        reference = ref_date.date()
    elif isinstance(ref_date, datetime.date):
        reference = ref_date
    elif isinstance(ref_date, str):
        try:
            reference = datetime.datetime.strptime(ref_date.strip()[:10], "%Y-%m-%d").date()
        except Exception:
            reference = datetime.date.today()
    else:
        reference = datetime.date.today()

    text_lower = (task_text or "").lower()

    # 2. Check for explicit ISO format (YYYY-MM-DD) inside task_text
    iso_match = re.search(r'\b(20\d{2}-\d{2}-\d{2})\b', text_lower)
    if iso_match:
        try:
            dt = datetime.datetime.strptime(iso_match.group(1), "%Y-%m-%d").date()
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    # 3. Check for 'tomorrow' / 'today'
    if re.search(r'\btomorrow\b', text_lower):
        return (reference + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    if re.search(r'\btoday\b', text_lower):
        return reference.strftime("%Y-%m-%d")

    # 4. Check for weekday keywords (e.g. 'by Friday', 'on Thursday', 'finish by Monday')
    # Order matters: check 'next <weekday>' first
    for w_name, w_idx in WEEKDAY_NAME_MAP.items():
        # Match 'next <weekday>'
        if re.search(rf'\bnext\s+{w_name}\b', text_lower):
            cur_idx = reference.weekday()
            diff = w_idx - cur_idx
            if diff <= 0:
                diff += 7
            # 'next weekday' is typically next week
            diff += 7
            return (reference + datetime.timedelta(days=diff)).strftime("%Y-%m-%d")

        # Match '<weekday>' (optionally preceded by 'by', 'on', 'this', 'due', 'before')
        if re.search(rf'\b(?:by|on|this|due|before)?\s*{w_name}\b', text_lower):
            cur_idx = reference.weekday()
            diff = w_idx - cur_idx
            if diff <= 0:
                diff += 7
            return (reference + datetime.timedelta(days=diff)).strftime("%Y-%m-%d")

    # 5. If no weekday in text, validate and fallback to extracted_deadline if provided
    if extracted_deadline and isinstance(extracted_deadline, str):
        cleaned = extracted_deadline.strip()
        banned = {"none", "n/a", "null", "not specified", "undefined", ""}
        if cleaned.lower() not in banned:
            try:
                dt = datetime.datetime.strptime(cleaned[:10], "%Y-%m-%d").date()
                return dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                # Try relative weekday matching on extracted_deadline string itself
                ext_lower = cleaned.lower()
                for w_name, w_idx in WEEKDAY_NAME_MAP.items():
                    if w_name in ext_lower:
                        cur_idx = reference.weekday()
                        diff = w_idx - cur_idx
                        if diff <= 0:
                            diff += 7
                        return (reference + datetime.timedelta(days=diff)).strftime("%Y-%m-%d")

    return None

def parse_deadline(deadline: Any) -> Optional[datetime.date]:
    """
    Safely validate and parse a deadline string in YYYY-MM-DD format.
    Returns datetime.date or None if invalid or missing.
    """
    if not deadline or not isinstance(deadline, str):
        return None
    cleaned = deadline.strip()
    if not cleaned or cleaned.lower() in ("none", "n/a", "null", "not specified", "undefined"):
        return None
    try:
        return datetime.datetime.strptime(cleaned[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None

def clean_task_title(task: str) -> str:
    """Strip temporal words from task string for a clean event title."""
    if not task:
        return "Untitled Action Item"
    pattern = r'\b(by|due|on|in)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|next\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|[a-zA-Z]+\s+\d{1,2}(?:st|nd|rd|th)?|\d+\s+(?:days|hours|weeks))\b'
    cleaned = re.sub(pattern, '', task, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r'\s*\.$', '', cleaned).strip()
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned or task.strip()

# ---------------------------------------------------------------------------
# 4. CALENDAR EVENT CREATION
# ---------------------------------------------------------------------------

def create_calendar_event(
    task: str,
    assignee: str,
    deadline: str,
    task_size: Optional[str] = None,
    meeting: Optional[str] = None,
    context: Optional[str] = None,
    db_path: str = DEFAULT_DB,
    force_simulate: Optional[bool] = None
) -> Dict[str, Any]:
    """
    Create a single Google Calendar event for an action item with automatic reminders.
    Never exposes OAuth tokens.
    
    Returns structured dict with:
      - status: 'SUCCESS' | 'FAILED' | 'SKIPPED' | 'SIMULATED'
      - event_id: Optional[str]
      - html_link: Optional[str]
      - task: str
      - assignee: str
      - deadline: Optional[str]
      - error: Optional[str]
      - message: str
    """
    init_calendar_db(db_path)

    clean_task = clean_task_title(task)
    clean_assignee = (assignee or "").strip() or "Unassigned"
    meeting_name = (meeting or "MeetMind AI Meeting").strip()
    size = str(task_size).upper() if task_size else "MEDIUM"

    result: Dict[str, Any] = {
        "status": "FAILED",
        "event_id": None,
        "html_link": None,
        "task": clean_task,
        "assignee": clean_assignee,
        "deadline": deadline,
        "meeting": meeting_name,
        "error": None,
        "message": ""
    }

    # 1. Requirement 7: Validate Deadline - do not create events for tasks without valid deadlines
    deadline_date = parse_deadline(deadline)
    if not deadline_date:
        result["status"] = "SKIPPED"
        result["message"] = f"Skipped: Missing or invalid deadline '{deadline}'"
        print(f"[CALENDAR] Skipping action item without valid deadline: '{clean_task[:30]}...'")
        return result

    deadline_str = deadline_date.strftime("%Y-%m-%d")
    result["deadline"] = deadline_str
    # In RFC 5545 / Google Calendar API, all-day event end dates are exclusive:
    # A one-day event on 2026-11-10 has start=2026-11-10 and end=2026-11-11
    next_day = deadline_date + datetime.timedelta(days=1)
    next_day_str = next_day.strftime("%Y-%m-%d")

    # 2. Look up assignee email from team directory
    from team_directory import get_assignee_email
    assignee_email = get_assignee_email(clean_assignee)
    from gmail_service import is_valid_email
    has_valid_email = is_valid_email(assignee_email)

    # 3. Requirement 6: Check for duplicate events in SQLite
    existing_event_id = is_duplicate_calendar_event(
        assignee=clean_assignee,
        task=task,
        deadline=deadline_str,
        meeting=meeting_name,
        db_path=db_path
    )
    if existing_event_id:
        result["status"] = "SKIPPED"
        result["event_id"] = existing_event_id
        result["message"] = f"Duplicate event already exists (Event ID: {existing_event_id})"
        print(f"[CALENDAR] Duplicate event already exists for '{clean_task[:30]}...' (ID: {existing_event_id})")
        return result

    # 4. Check whether Calendar integration is enabled
    calendar_enabled = os.environ.get("CALENDAR_NOTIFICATIONS_ENABLED", "false").lower() == "true"
    if force_simulate is not None:
        calendar_enabled = not force_simulate

    # If disabled or forced simulation: return simulated event
    if not calendar_enabled:
        simulated_id = f"simulated_cal_{abs(hash(clean_task + clean_assignee + deadline_str)) % 1000000}"
        save_calendar_event_record(
            google_event_id=simulated_id,
            assignee=clean_assignee,
            email=assignee_email if has_valid_email else "",
            task=task,
            deadline=deadline_str,
            meeting=meeting_name,
            task_size=size,
            html_link="https://calendar.google.com/calendar/r",
            status="SIMULATED",
            db_path=db_path
        )
        result["status"] = "SIMULATED"
        result["event_id"] = simulated_id
        result["html_link"] = "https://calendar.google.com/calendar/r"
        result["message"] = "Simulated successfully (CALENDAR_NOTIFICATIONS_ENABLED=false)"
        print(f"[CALENDAR] (SIMULATED) Event created for '{clean_task[:30]}...' on {deadline_str}")
        return result

    # 5. Initialize Google Calendar service
    service, error_msg = get_calendar_service()
    if not service:
        result["status"] = "FAILED"
        result["error"] = error_msg
        result["message"] = f"Google Calendar service unavailable: {error_msg}"
        print(f"[CALENDAR] Error initializing Calendar service: {error_msg}")
        return result

    # 6. Build event reminders based on task_size (Requirement 5)
    # LARGE tasks: 48h and 24h reminders; SMALL/MEDIUM tasks: 24h and 9h reminders
    if size == "LARGE":
        reminder_overrides = [
            {"method": "popup", "minutes": 48 * 60},  # 2 days before
            {"method": "email", "minutes": 48 * 60},
            {"method": "popup", "minutes": 24 * 60},  # 1 day before
            {"method": "popup", "minutes": 9 * 60},   # Morning of deadline
        ]
    else:
        reminder_overrides = [
            {"method": "popup", "minutes": 24 * 60},  # 1 day before
            {"method": "email", "minutes": 24 * 60},
            {"method": "popup", "minutes": 9 * 60},   # Morning of deadline
        ]

    # 7. Build Google Calendar Event Payload (Requirement 4)
    description_parts = [
        "MEETMIND AI DEADLINE ALERT",
        "────────────────────────",
        f"Task: {task}",
        f"Assigned to: {clean_assignee}" + (f" ({assignee_email})" if has_valid_email else ""),
        f"Deadline: {deadline_str}",
        f"Task Size: {size}",
        f"Meeting: {meeting_name}",
    ]
    if context:
        description_parts.append(f"Context: {context}")
    description_parts.extend([
        "────────────────────────",
        "Created automatically by MeetMind AI."
    ])
    description_text = "\n".join(description_parts)

    event_body = {
        "summary": f"[MeetMind AI] Deadline: {clean_task[:80]}",
        "description": description_text,
        "start": {
            "date": deadline_str,
        },
        "end": {
            "date": next_day_str,
        },
        "reminders": {
            "useDefault": False,
            "overrides": reminder_overrides,
        },
        "extendedProperties": {
            "private": {
                "source": "MeetMind AI",
                "assignee": clean_assignee,
                "deadline": deadline_str,
                "meeting": meeting_name[:100]
            }
        }
    }

    # Add attendee if valid email found
    if has_valid_email:
        event_body["attendees"] = [{"email": assignee_email, "displayName": clean_assignee}]

    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
    send_updates = os.environ.get("GOOGLE_CALENDAR_SEND_UPDATES", "none")

    # 8. Execute Google Calendar API Call
    try:
        from googleapiclient.errors import HttpError
        created_event = service.events().insert(
            calendarId=calendar_id,
            body=event_body,
            sendUpdates=send_updates
        ).execute()

        event_id = created_event.get("id")
        html_link = created_event.get("htmlLink", "")

        # Save to SQLite (Requirement 6)
        save_calendar_event_record(
            google_event_id=event_id,
            assignee=clean_assignee,
            email=assignee_email if has_valid_email else "",
            task=task,
            deadline=deadline_str,
            meeting=meeting_name,
            task_size=size,
            html_link=html_link,
            status="CREATED",
            db_path=db_path
        )

        result["status"] = "SUCCESS"
        result["event_id"] = event_id
        result["html_link"] = html_link
        result["message"] = f"Event created successfully on Google Calendar (ID: {event_id})"
        print(f"[CALENDAR] Created event for {clean_assignee} on {deadline_str} (ID: {event_id})")
        return result

    except HttpError as error:
        err_msg = f"Google Calendar API error (HTTP {error.resp.status}): {error._get_reason()}"
        result["status"] = "FAILED"
        result["error"] = err_msg
        result["message"] = err_msg
        print(f"[CALENDAR] API HttpError: {err_msg}")
        return result
    except Exception as e:
        err_msg = f"Unexpected exception creating calendar event: {str(e)}"
        result["status"] = "FAILED"
        result["error"] = err_msg
        result["message"] = err_msg
        print(f"[CALENDAR] Exception: {err_msg}")
        return result

# ---------------------------------------------------------------------------
# 5. BATCH ACTION ITEM PROCESSING
# ---------------------------------------------------------------------------

def create_calendar_events_for_action_items(
    action_items: List[Any],
    meeting_title: Optional[str] = None,
    db_path: str = DEFAULT_DB
) -> List[Dict[str, Any]]:
    """
    Iterate over meeting action items, extract tasks, assignees, deadlines,
    and create Google Calendar events automatically.
    """
    results = []
    if not action_items:
        return results

    for item in action_items:
        if isinstance(item, dict):
            task = item.get("task", "")
            assignee = item.get("assignee")
            deadline = item.get("deadline")
            task_size = item.get("task_size")
            context = item.get("context")
        else:
            task = getattr(item, "task", "")
            assignee = getattr(item, "assignee", None)
            deadline = getattr(item, "deadline", None)
            task_size = getattr(item, "task_size", None)
            context = getattr(item, "context", None)

        if not task or not assignee:
            continue

        res = create_calendar_event(
            task=task,
            assignee=assignee,
            deadline=deadline,
            task_size=task_size,
            meeting=meeting_title,
            context=context,
            db_path=db_path
        )
        results.append(res)

    return results

# ---------------------------------------------------------------------------
# 6. CONNECTION & DIAGNOSTIC VERIFICATION
# ---------------------------------------------------------------------------

def verify_calendar_connection() -> Dict[str, Any]:
    """
    Diagnostic verification of Google Calendar API connection and primary calendar access.
    DOES NOT create any events. Never exposes tokens.
    """
    result = {
        "status": "FAILED",
        "calendar_id": None,
        "time_zone": None,
        "summary": None,
        "error": None
    }

    service, error_msg = get_calendar_service()
    if not service:
        result["error"] = error_msg
        return result

    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

    try:
        from googleapiclient.errors import HttpError
        cal = service.calendars().get(calendarId=calendar_id).execute()
        result["status"] = "SUCCESS"
        result["calendar_id"] = cal.get("id")
        result["time_zone"] = cal.get("timeZone")
        result["summary"] = cal.get("summary")
        return result
    except HttpError as error:
        result["error"] = f"Calendar access check failed (HTTP {error.resp.status}): {error._get_reason()}"
        return result
    except Exception as e:
        result["error"] = f"Unexpected exception checking Google Calendar: {str(e)}"
        return result

