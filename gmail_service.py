import os
import base64
import re
import logging
from email.mime.text import MIMEText
from typing import Dict, Any, Optional, Tuple
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
    return bool(re.match(pattern, email))

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
    summary: Optional[str] = None
) -> Tuple[str, str]:
    """Generate subject and body for action item notification."""
    subject = f"[MeetMind AI] Action Item Assigned: {task}"
    body = f"""Hi {assignee_name},

You have been assigned a new action item from the recent meeting:

Task: {task}
Deadline: {deadline or 'Not specified'}

Meeting Summary:
{summary or 'N/A'}

---
Generated automatically by MeetMind AI
"""
    return subject, body

def send_action_item_notification(
    to_email: Optional[str],
    assignee_name: str,
    task: str,
    deadline: Optional[str] = None,
    summary: Optional[str] = None,
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
            "status": "skipped_unknown_assignee",
            "assignee": assignee_name or "Unknown",
            "email": to_email or "None",
            "message": status_msg
        }

    if not to_email or to_email == "email not found":
        status_msg = "SKIPPED (no registered email address)"
        logger.info(f"{assignee_name} -> email not found -> {status_msg}")
        return {
            "status": "skipped_no_email",
            "assignee": assignee_name,
            "email": "email not found",
            "message": status_msg
        }

    # 2. Validate email recipient format
    if not is_valid_email(to_email):
        status_msg = f"SKIPPED (invalid recipient email format: '{to_email}')"
        logger.warning(f"{assignee_name} -> {to_email} -> {status_msg}")
        return {
            "status": "skipped_invalid_recipient",
            "assignee": assignee_name,
            "email": to_email,
            "message": status_msg
        }

    subject, body = create_email_content(assignee_name, task, deadline, summary)

    # 3. Check if notifications are enabled
    notifications_enabled = os.environ.get("EMAIL_NOTIFICATIONS_ENABLED", "false").lower() == "true"
    if force_simulate or not notifications_enabled:
        status_msg = "PREVIEW_GENERATED (EMAIL_NOTIFICATIONS_ENABLED=false)"
        logger.info(f"{assignee_name} -> {to_email} -> {status_msg}")
        return {
            "status": "preview",
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
            "status": "failed",
            "assignee": assignee_name,
            "email": to_email,
            "error": error_msg
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
        status_msg = f"SENT (Message ID: {msg_id})"
        logger.info(f"{assignee_name} -> {to_email} -> {status_msg}")
        return {
            "status": "sent",
            "assignee": assignee_name,
            "email": to_email,
            "message_id": msg_id,
            "message": status_msg
        }
    except HttpError as error:
        err = f"Gmail API HTTP error: {error}"
        logger.error(f"{assignee_name} -> {to_email} -> FAILED ({err})")
        return {
            "status": "failed_api",
            "assignee": assignee_name,
            "email": to_email,
            "error": err
        }
    except Exception as e:
        err = f"Failed to send email: {e}"
        logger.error(f"{assignee_name} -> {to_email} -> FAILED ({err})")
        return {
            "status": "failed",
            "assignee": assignee_name,
            "email": to_email,
            "error": err
        }
