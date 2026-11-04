import os
import sys
import hmac
import hashlib
import json
import logging
from typing import Dict, Any, Tuple, Optional
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=False)
DEFAULT_DB = os.path.join(PROJECT_ROOT, "reminders.db")

logger = logging.getLogger("meetmind.jira_webhook")

from reminder_service import init_reminders_db
from task_lifecycle import (
    cancel_reminders_for_jira_task,
    update_jira_deadline_and_reschedule,
    update_reminder_assignee
)

def get_jira_webhook_secret() -> str:
    """
    Retrieve configured Jira webhook secret safely from environment.
    Strictly accepts ONLY JIRA_WEBHOOK_SECRET with NO fallback to JIRA_API_TOKEN
    or any other credential.
    Never exposes secret in logs.
    """
    secret = os.environ.get("JIRA_WEBHOOK_SECRET") or ""
    return secret.strip().strip("\"'")

def verify_jira_webhook_signature(
    raw_body: bytes,
    signature_header: Optional[str],
    secret: Optional[str] = None
) -> bool:
    """
    Verify Jira Cloud HMAC-SHA256 signature from X-Hub-Signature header.
    Standard Jira Cloud format: sha256=<hex_digest>
    Uses constant-time comparison to prevent timing attacks.
    Never logs secrets or signatures.
    """
    expected_secret = secret if secret is not None else get_jira_webhook_secret()
    if not expected_secret or not expected_secret.strip():
        logger.warning("[JIRA WEBHOOK] Missing or empty JIRA_WEBHOOK_SECRET. Request rejected.")
        return False

    if not signature_header or not str(signature_header).strip():
        return False

    sig_clean = signature_header.strip()
    if sig_clean.startswith("sha256="):
        received_hex = sig_clean[7:].strip()
    else:
        received_hex = sig_clean

    try:
        computed_hex = hmac.new(
            expected_secret.encode("utf-8"),
            msg=raw_body,
            digestmod=hashlib.sha256
        ).hexdigest()
    except Exception as e:
        logger.warning(f"[JIRA WEBHOOK] Error computing HMAC signature: {e}")
        return False

    return hmac.compare_digest(computed_hex, received_hex)

def verify_jira_webhook_auth(
    raw_body: bytes,
    signature_header: Optional[str] = None,
    auth_header: Optional[str] = None,
    custom_header: Optional[str] = None,
    secret_param: Optional[str] = None
) -> bool:
    """
    Authenticate Jira Cloud webhook request:
    1. Primary production method: X-Hub-Signature HMAC-SHA256 signature verification.
    2. Header fallbacks (X-Atlassian-Webhook-Secret or Authorization Bearer) for proxy/gateway setups.
    3. URL query-string secret authentication is removed from recommended production configuration.
       (Only permitted if ALLOW_INSECURE_QUERY_WEBHOOK_SECRET=true is explicitly set in env).
    Uses constant-time comparison and never logs secrets or signatures.
    """
    # 1. Primary: Genuine Jira Cloud X-Hub-Signature HMAC-SHA256
    if signature_header and str(signature_header).strip():
        return verify_jira_webhook_signature(raw_body, signature_header)

    expected_secret = get_jira_webhook_secret()
    if not expected_secret:
        logger.warning("[JIRA WEBHOOK] No JIRA_WEBHOOK_SECRET configured in environment.")
        return False

    # 2. Header fallbacks for reverse proxies / gateways
    candidate = None
    if custom_header and custom_header.strip():
        candidate = custom_header.strip()
    elif auth_header and auth_header.strip():
        parts = auth_header.strip().split(" ", 1)
        if len(parts) == 2 and parts[0].lower() in ("bearer", "token"):
            candidate = parts[1].strip()
        else:
            candidate = auth_header.strip()

    if candidate:
        return hmac.compare_digest(candidate.encode("utf-8"), expected_secret.encode("utf-8"))

    # 3. URL query-string secret: explicitly disallowed in production by default
    allow_query = os.environ.get("ALLOW_INSECURE_QUERY_WEBHOOK_SECRET", "false").lower() == "true"
    if allow_query and secret_param and secret_param.strip():
        logger.warning("[JIRA WEBHOOK] Insecure query-string authentication accepted via legacy override.")
        return hmac.compare_digest(secret_param.strip().encode("utf-8"), expected_secret.encode("utf-8"))

    return False

def extract_jira_event_id(
    payload: Dict[str, Any],
    raw_body: bytes,
    headers: Optional[Dict[str, str]] = None
) -> str:
    """
    Generate or extract a unique, deterministic event ID for Jira webhook deduplication.
    1. Primary: Genuine Jira Cloud 'X-Atlassian-Webhook-Identifier' header.
       - Retries with the same identifier are safely ignored as duplicates.
       - Separate legitimate events with identical payloads have distinct identifiers and are processed.
    2. Secondary: Jira payload event timestamp + issue ID.
    3. Fallback: SHA-256 hash of payload content.
    """
    if headers:
        norm_headers = {k.lower(): v for k, v in headers.items()}
        webhook_id = norm_headers.get("x-atlassian-webhook-identifier")
        if webhook_id and str(webhook_id).strip():
            return f"jira_wh_{webhook_id.strip()}"

    timestamp = payload.get("timestamp")
    issue = payload.get("issue") or {}
    issue_id = issue.get("id") or issue.get("key")
    event_type = payload.get("webhookEvent", "unknown")

    if timestamp and issue_id:
        return f"jira_{event_type}_{issue_id}_{timestamp}"

    # Fallback to payload content hash
    payload_hash = hashlib.sha256(raw_body).hexdigest()[:24]
    return f"jira_hash_{payload_hash}"

def process_jira_webhook_event(
    payload: Dict[str, Any],
    raw_body: bytes,
    db_path: str = DEFAULT_DB,
    calendar_service: Any = None,
    headers: Optional[Dict[str, str]] = None
) -> Tuple[int, Dict[str, Any]]:
    """
    Process incoming Jira Cloud webhook event with idempotency and transaction safety.
    Returns (status_code, response_data).
    """
    init_reminders_db(db_path)
    event_id = extract_jira_event_id(payload, raw_body, headers=headers)
    event_type = payload.get("webhookEvent", "unknown")
    payload_hash = hashlib.sha256(raw_body).hexdigest()

    import sqlite3
    import datetime
    now_iso = datetime.datetime.now().isoformat()

    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        c = conn.cursor()
        # 1. Idempotency Check: check if event already processed
        c.execute("SELECT status FROM webhook_events WHERE event_id = ?", (event_id,))
        row = c.fetchone()
        if row:
            if row[0] == "PROCESSED":
                logger.info(f"[JIRA WEBHOOK] Duplicate event '{event_id}' already processed. Acknowledging with 200.")
                return 200, {
                    "status": "DUPLICATE_IGNORED",
                    "event_id": event_id,
                    "message": "Event already processed successfully"
                }

        # 2. Insert event record in PROCESSING status
        c.execute('''
            INSERT OR REPLACE INTO webhook_events (event_id, source, event_type, payload_hash, received_at, status)
            VALUES (?, 'JIRA', ?, ?, ?, 'PROCESSING')
        ''', (event_id, event_type, payload_hash, now_iso))
        conn.commit()
    finally:
        conn.close()

    # 3. Validate issue structure
    issue = payload.get("issue")
    if not isinstance(issue, dict):
        _mark_event_failed(db_path, event_id, "Missing 'issue' dictionary in payload")
        return 400, {"error": "Invalid payload: missing 'issue' object"}

    issue_key = (issue.get("key") or "").strip().upper()
    if not issue_key:
        _mark_event_failed(db_path, event_id, "Missing 'issue.key' in payload")
        return 400, {"error": "Invalid payload: missing 'issue.key'"}

    changelog = payload.get("changelog") or {}
    items = changelog.get("items") or []

    results = []
    actions_taken = []

    try:
        # A. Detect Transition to Done / Closed / Completed
        is_done = False
        # Check changelog items for status change
        for item in items:
            field = str(item.get("field", "")).lower()
            field_id = str(item.get("fieldId", "")).lower()
            if field == "status" or field_id == "status":
                to_val = str(item.get("toString", "")).strip().lower()
                if to_val in ("done", "closed", "completed", "resolved"):
                    is_done = True
                    break

        # Also inspect current issue status category
        issue_fields = issue.get("fields") or {}
        status_obj = issue_fields.get("status") or {}
        status_name = str(status_obj.get("name", "")).strip().lower()
        category_key = str((status_obj.get("statusCategory") or {}).get("key", "")).strip().lower()
        if category_key == "done" or status_name in ("done", "closed", "completed", "resolved"):
            is_done = True

        if is_done:
            logger.info(f"[JIRA WEBHOOK] Issue '{issue_key}' transitioned to Done. Cancelling pending reminders.")
            cancel_res = cancel_reminders_for_jira_task(issue_key, db_path=db_path)
            actions_taken.append("CANCELLED_REMINDERS")
            results.append({"action": "CANCEL_REMINDERS", "details": cancel_res})

        # B. Detect Due-Date Changes
        new_deadline = None
        for item in items:
            field = str(item.get("field", "")).lower()
            field_id = str(item.get("fieldId", "")).lower()
            if field in ("duedate", "due date") or field_id == "duedate":
                to_val = item.get("toString")
                if to_val and str(to_val).strip():
                    new_deadline = str(to_val).strip()
                    break

        if new_deadline:
            logger.info(f"[JIRA WEBHOOK] Issue '{issue_key}' due date changed to {new_deadline}. Rescheduling reminders.")
            resched_res = update_jira_deadline_and_reschedule(
                task_identifier=issue_key,
                new_deadline=new_deadline,
                db_path=db_path,
                calendar_service=calendar_service
            )
            actions_taken.append("RESCHEDULED_REMINDERS")
            results.append({"action": "RESCHEDULE_REMINDERS", "details": resched_res})

        # C. Detect Assignee Changes
        new_assignee_name = None
        for item in items:
            field = str(item.get("field", "")).lower()
            field_id = str(item.get("fieldId", "")).lower()
            if field == "assignee" or field_id == "assignee":
                to_val = item.get("toString")
                if to_val and str(to_val).strip():
                    new_assignee_name = str(to_val).strip()
                    break

        if new_assignee_name:
            logger.info(f"[JIRA WEBHOOK] Issue '{issue_key}' reassigned to '{new_assignee_name}'. Updating recipient mapping.")
            reassign_res = update_reminder_assignee(
                task_identifier=issue_key,
                new_assignee=new_assignee_name,
                db_path=db_path
            )
            actions_taken.append("UPDATED_ASSIGNEE")
            results.append({"action": "UPDATE_ASSIGNEE", "details": reassign_res})

        # 4. Mark event as successfully PROCESSED in SQLite
        conn = sqlite3.connect(db_path, timeout=30.0)
        try:
            c = conn.cursor()
            c.execute('''
                UPDATE webhook_events 
                SET status = 'PROCESSED', processed_at = ? 
                WHERE event_id = ?
            ''', (datetime.datetime.now().isoformat(), event_id))
            conn.commit()
        finally:
            conn.close()

        return 200, {
            "status": "SUCCESS",
            "event_id": event_id,
            "issue_key": issue_key,
            "actions_taken": actions_taken,
            "results": results
        }

    except Exception as e:
        logger.error(f"[JIRA WEBHOOK] Exception processing event '{event_id}': {e}", exc_info=True)
        _mark_event_failed(db_path, event_id, str(e))
        return 500, {
            "status": "ERROR",
            "event_id": event_id,
            "error": f"Internal error processing webhook: {e}"
        }

def _mark_event_failed(db_path: str, event_id: str, error_msg: str):
    """Safely mark event as FAILED in webhook_events audit table."""
    try:
        import sqlite3
        import datetime
        conn = sqlite3.connect(db_path, timeout=10.0)
        c = conn.cursor()
        c.execute('''
            UPDATE webhook_events 
            SET status = 'FAILED', error = ?, processed_at = ? 
            WHERE event_id = ?
        ''', (error_msg[:400], datetime.datetime.now().isoformat(), event_id))
        conn.commit()
        conn.close()
    except Exception as db_err:
        logger.error(f"[JIRA WEBHOOK] Failed to update webhook_events status for '{event_id}': {db_err}")

