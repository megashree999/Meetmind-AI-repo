import os
import base64
import re
import logging
from email.mime.text import MIMEText
from typing import Dict, Any, Optional, Tuple, List
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("meetmind.gmail")

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

def is_valid_email(email: Optional[str]) -> bool:
    """Validate email format."""
    if not email or not isinstance(email, str):
        return False
    email = email.strip()
    if email == "email not found":
        return False
    pattern = r"^[\w\.-]+@[\w\.-]+\.\w+$"
    if not bool(re.match(pattern, email)):
        return False
    
    # Block demo addresses
    if email.endswith("@company.com") or email.endswith("@example.com"):
        return False
        
    return True

def get_gmail_service() -> Tuple[Optional[Any], str]:
    """
    Initialize Gmail API service with OAuth 2.0.
    
    Returns:
        Tuple of (service_instance or None, status_message)
    """
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as e:
        return None, f"Gmail API dependencies not installed: {e}"

    creds_file = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.environ.get("GMAIL_TOKEN_FILE", "token.json")

    creds = None

    # Check for existing user token
    if os.path.exists(token_file):
        try:
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        except Exception as e:
            return None, f"OAuth token invalid or corrupt in {token_file}: {e}"

    # If no valid credentials, check refresh or availability
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(token_file, "w", encoding="utf-8") as token:
                token.write(creds.to_json())
        except Exception as e:
            return None, f"OAuth token refresh failed: {e}"

    if not creds or not creds.valid:
        if not os.path.exists(creds_file):
            return None, f"Gmail credentials missing: '{creds_file}' not found."
        return None, f"OAuth token missing or expired: '{token_file}' not found or needs authorization."

    try:
        service = build("gmail", "v1", credentials=creds)
        return service, "Gmail service ready"
    except Exception as e:
        return None, f"Gmail API service initialization failed: {e}"

def create_email_content(
    assignee_name: str,
    task: str,
    deadline: Optional[str] = None,
    summary: Optional[str] = None,
    context: Optional[str] = None,
    progress: Optional[str] = None,
    expected_outcome: Optional[str] = None,
    decisions: Optional[str] = None,
    next_steps: Optional[str] = None,
    blockers: Optional[str] = None
) -> Tuple[str, str]:
    """Generate subject and body for action item notification."""
    # Ensure subject is short
    short_task = task[:50] + "..." if len(task) > 50 else task
    subject = f"[MeetMind AI] Action Item Assigned: {short_task}"
    
    def format_val(val):
        if not val:
            return "Not specified"
        if isinstance(val, list):
            if len(val) == 0: return "Not specified"
            val = ", ".join(str(v) for v in val)
        lower_val = str(val).lower().strip()
        banned = {"none", "n/a", "null", "", "not specified", "to-do", "pending", "not started", "in progress", "no blockers", "no decisions"}
        return str(val) if lower_val not in banned else "Not specified"

    clean_task = format_val(task)
    clean_deadline = format_val(deadline)
    clean_context = format_val(context)
    clean_summary = format_val(summary)
    
    deadline_section = f"DEADLINE:\n{clean_deadline}\n\n" if clean_deadline != "Not specified" else ""
    
    body = f"""Hi {assignee_name},

You have been assigned an action item from the meeting.

────────────────────────

TASK:
{clean_task}

{deadline_section}CONTEXT:
{clean_context}

MEETING SUMMARY:
{clean_summary}

────────────────────────

Please complete the above task by the discussed deadline.

Regards,
MeetMind AI"""
    return subject, body

def send_action_item_notification(
    to_email: Optional[str],
    assignee_name: str,
    task: str,
    deadline: Optional[str] = None,
    summary: Optional[str] = None,
    context: Optional[str] = None,
    progress: Optional[str] = None,
    expected_outcome: Optional[str] = None,
    decisions: Optional[str] = None,
    next_steps: Optional[str] = None,
    blockers: Optional[str] = None,
    force_simulate: Optional[bool] = None
) -> Dict[str, Any]:
    """
    Send an email notification for an assigned action item.
    
    If EMAIL_NOTIFICATIONS_ENABLED is false (or force_simulate=True),
    generates a preview without sending.
    
    Returns:
        Dict containing status, assignee, email, and details.
    """
    # 1. Check assignee and email presence
    if not assignee_name or assignee_name.strip() in ["", "Unassigned", "None"]:
        status_msg = "SKIPPED (unknown or unassigned person)"
        logger.info(f"{assignee_name} -> {to_email} -> {status_msg}")
        return {
            "status": "SKIPPED",
            "assignee": assignee_name or "Unknown",
            "email": to_email or "None",
            "message": status_msg
        }

    if not to_email or to_email == "email not found":
        status_msg = f"SKIPPED (Assignee {assignee_name} not found in company directory. Email notification skipped.)"
        logger.info(f"{assignee_name} -> email not found -> {status_msg}")
        return {
            "status": "SKIPPED",
            "assignee": assignee_name,
            "email": "email not found",
            "message": status_msg
        }

    # 2. Validate email recipient format
    if not is_valid_email(to_email):
        status_msg = f"SKIPPED (invalid recipient email format: '{to_email}')"
        logger.warning(f"{assignee_name} -> {to_email} -> {status_msg}")
        return {
            "status": "SKIPPED",
            "assignee": assignee_name,
            "email": to_email,
            "message": status_msg
        }

    subject, body = create_email_content(
        assignee_name, task, deadline, summary, 
        context, progress, expected_outcome, decisions, next_steps, blockers
    )

    # 3. Check if notifications are enabled
    notifications_enabled = os.environ.get("EMAIL_NOTIFICATIONS_ENABLED", "false").lower() == "true"
    if force_simulate or not notifications_enabled:
        status_msg = "PREVIEW (Email prepared but EMAIL_NOTIFICATIONS_ENABLED=false)"
        logger.info(f"{assignee_name} -> {to_email} -> {status_msg}")
        return {
            "status": "PREVIEW",
            "assignee": assignee_name,
            "email": to_email,
            "subject": subject,
            "body": body,
            "message": status_msg
        }

    # 4. Live sending with Gmail API
    service, error_msg = get_gmail_service()
    if not service:
        logger.error(f"{assignee_name} -> {to_email} -> FAILED ({error_msg})")
        return {
            "status": "FAILED",
            "assignee": assignee_name,
            "email": to_email,
            "error": error_msg,
            "message": f"FAILED (Gmail API service failed: {error_msg})"
        }

    try:
        from googleapiclient.errors import HttpError
        message = MIMEText(body)
        message["to"] = to_email
        message["subject"] = subject
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        sent_response = service.users().messages().send(
            userId="me",
            body={"raw": raw_message}
        ).execute()
        
        msg_id = sent_response.get("id")
        status_msg = f"SENT (Email successfully sent by Gmail. Message ID: {msg_id})"
        logger.info(f"{assignee_name} -> {to_email} -> {status_msg}")
        return {
            "status": "SENT",
            "assignee": assignee_name,
            "email": to_email,
            "message_id": msg_id,
            "message": status_msg,
            "body": body
        }
    except HttpError as error:
        err = f"Gmail API HTTP error: {error}"
        logger.error(f"{assignee_name} -> {to_email} -> FAILED ({err})")
        return {
            "status": "FAILED",
            "assignee": assignee_name,
            "email": to_email,
            "error": err,
            "message": f"FAILED ({err})"
        }
    except Exception as e:
        err = f"Failed to send email: {e}"
        logger.error(f"{assignee_name} -> {to_email} -> FAILED ({err})")
        return {
            "status": "FAILED",
            "assignee": assignee_name,
            "email": to_email,
            "error": err,
            "message": f"FAILED ({err})"
        }
