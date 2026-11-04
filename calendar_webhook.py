import os
import sys
import hmac
import hashlib
import json
import logging
import secrets
import datetime
from typing import Dict, Any, Tuple, Optional, List
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(PROJECT_ROOT, "reminders.db")

logger = logging.getLogger("meetmind.calendar_webhook")

from reminder_service import init_reminders_db
from task_lifecycle import (
    handle_calendar_event_deleted,
    update_calendar_event_deadline_and_reschedule
)

def get_calendar_webhook_secret() -> str:
    """Retrieve configured Calendar webhook secret token safely from environment."""
    secret = os.environ.get("CALENDAR_WEBHOOK_SECRET") or os.environ.get("GOOGLE_WEBHOOK_SECRET") or ""
    return secret.strip().strip("\"'")

def register_calendar_watch_channel(
    service: Any,
    webhook_url: str,
    db_path: str = DEFAULT_DB,
    custom_token: Optional[str] = None,
    expiration_hours: int = 168  # 7 days default
) -> Dict[str, Any]:
    """
    Register a push notification watch channel using Google Calendar API events.watch().
    Persists channel details and initial sync token in SQLite.
    """
    init_reminders_db(db_path)
    import uuid
    import sqlite3

    channel_id = f"meetmind_cal_{uuid.uuid4().hex}"
    token = custom_token or get_calendar_webhook_secret() or secrets.token_urlsafe(32)

    now = datetime.datetime.now(datetime.timezone.utc)
    expiration_dt = now + datetime.timedelta(hours=expiration_hours)
    expiration_ms = int(expiration_dt.timestamp() * 1000)

    watch_body = {
        "id": channel_id,
        "type": "web_hook",
        "address": webhook_url,
        "token": token,
        "params": {
            "ttl": str(expiration_hours * 3600)
        }
    }

    logger.info(f"[CALENDAR WATCH] Registering watch channel {channel_id} at {webhook_url}")
    watch_resp = service.events().watch(calendarId="primary", body=watch_body).execute()

    resource_id = watch_resp.get("resourceId")
    resource_uri = watch_resp.get("resourceUri")
    expiration_str = str(watch_resp.get("expiration") or expiration_ms)

    # Initial full synchronization to establish synchronization baseline and retrieve nextSyncToken
    initial_sync_token = None
    page_token = None
    while True:
        list_kwargs = {"calendarId": "primary"}
        if page_token:
            list_kwargs["pageToken"] = page_token
        list_resp = service.events().list(**list_kwargs).execute()
        if list_resp.get("nextSyncToken"):
            initial_sync_token = list_resp["nextSyncToken"]
            break
        page_token = list_resp.get("nextPageToken")
        if not page_token:
            break

    try:
        conn = sqlite3.connect(db_path, timeout=30.0)
        try:
            c = conn.cursor()
            c.execute('''
                INSERT OR REPLACE INTO calendar_watch_channels (channel_id, resource_id, resource_uri, token, expiration, created_at, status, webhook_url)
                VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?)
            ''', (channel_id, resource_id, resource_uri, token, expiration_str, now.isoformat(), webhook_url))

            if initial_sync_token:
                c.execute('''
                    INSERT OR REPLACE INTO calendar_sync_state (channel_id, sync_token, last_synced_at)
                    VALUES (?, ?, ?)
                ''', (channel_id, initial_sync_token, now.isoformat()))

            conn.commit()
        finally:
            conn.close()
    except Exception as db_err:
        logger.error(f"[CALENDAR WATCH] Failed to persist channel {channel_id} in SQLite: {db_err}")
        # Clean up registered watch channel in Google Calendar so it is not orphaned
        try:
            service.channels().stop(body={"id": channel_id, "resourceId": resource_id}).execute()
        except Exception:
            pass
        raise db_err

    return {
        "status": "SUCCESS",
        "channel_id": channel_id,
        "resource_id": resource_id,
        "expiration": expiration_str,
        "sync_token_stored": bool(initial_sync_token)
    }

def stop_calendar_watch_channel(
    service: Any,
    channel_id: str,
    resource_id: str,
    db_path: str = DEFAULT_DB
) -> Dict[str, Any]:
    """Stop an active Google Calendar push notification watch channel."""
    init_reminders_db(db_path)
    import sqlite3
    try:
        service.channels().stop(body={"id": channel_id, "resourceId": resource_id}).execute()
        logger.info(f"[CALENDAR WATCH] Successfully stopped channel {channel_id}")
    except Exception as e:
        logger.warning(f"[CALENDAR WATCH] Exception stopping channel {channel_id}: {e}")

    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        c = conn.cursor()
        c.execute("UPDATE calendar_watch_channels SET status = 'STOPPED' WHERE channel_id = ?", (channel_id,))
        conn.commit()
    finally:
        conn.close()

    return {"status": "SUCCESS", "channel_id": channel_id}

def renew_expired_calendar_watch_channels(
    service: Any,
    webhook_url: Optional[str] = None,
    db_path: str = DEFAULT_DB,
    lead_time_hours: int = 24
) -> List[Dict[str, Any]]:
    """
    Find active channels nearing expiration (within lead_time_hours) and renew them safely:
    - If new channel registration fails, the existing channel remains ACTIVE.
    - If registration succeeds but persistence fails, cleanup is safely handled.
    - Old channels are stopped ONLY AFTER successful registration and persistence of the new channel.
    - Logs renewal failures without exposing tokens.
    """
    init_reminders_db(db_path)
    import sqlite3
    now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    lead_ms = lead_time_hours * 3600 * 1000
    cutoff_ms = now_ms + lead_ms

    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        c = conn.cursor()
        c.execute('''
            SELECT channel_id, resource_id, expiration, token, webhook_url 
            FROM calendar_watch_channels 
            WHERE status = 'ACTIVE'
        ''')
        rows = c.fetchall()
    finally:
        conn.close()

    renewed = []
    for row in rows:
        ch_id = row[0]
        res_id = row[1]
        exp_str = row[2]
        ch_webhook_url = row[4] if len(row) > 4 else None

        try:
            exp_val = int(exp_str)
        except Exception:
            exp_val = 0

        if exp_val <= cutoff_ms:
            target_url = webhook_url or ch_webhook_url or os.environ.get("CALENDAR_WEBHOOK_URL", "")
            if not target_url:
                logger.error(f"[CALENDAR WATCH] Renewal skipped for channel {ch_id}: No webhook URL provided or stored.")
                continue

            logger.info(f"[CALENDAR WATCH] Channel {ch_id} expiring soon (exp: {exp_val}, cutoff: {cutoff_ms}). Renewing at {target_url}.")
            try:
                # 1. Register new channel FIRST and persist it (guaranteeing continuous listening)
                fresh_token = secrets.token_urlsafe(32)
                reg_res = register_calendar_watch_channel(service, target_url, db_path=db_path, custom_token=fresh_token)
                new_channel_id = reg_res.get("channel_id")
                
                # Copy forward latest sync token to the new channel ID in calendar_sync_state
                if new_channel_id:
                    conn_sync = sqlite3.connect(db_path, timeout=10.0)
                    try:
                        c_sync = conn_sync.cursor()
                        c_sync.execute("SELECT sync_token FROM calendar_sync_state WHERE channel_id = ?", (ch_id,))
                        tok_row = c_sync.fetchone()
                        if tok_row and tok_row[0]:
                            c_sync.execute('''
                                INSERT OR REPLACE INTO calendar_sync_state (channel_id, sync_token, last_synced_at)
                                VALUES (?, ?, ?)
                            ''', (new_channel_id, tok_row[0], datetime.datetime.now().isoformat()))
                            conn_sync.commit()
                    finally:
                        conn_sync.close()

                # 2. Stop old channel ONLY AFTER new channel is successfully registered and persisted
                stop_calendar_watch_channel(service, ch_id, res_id, db_path=db_path)
                renewed.append({"old_channel_id": ch_id, "new_registration": reg_res})

            except Exception as renew_err:
                # Log renewal failure safely without exposing secret tokens
                logger.error(f"[CALENDAR WATCH] Renewal failed for channel {ch_id}: {renew_err}. Old channel remains ACTIVE.")

    return renewed

def verify_calendar_webhook_headers(
    headers: Dict[str, str],
    db_path: str = DEFAULT_DB
) -> Tuple[bool, int, str, Optional[Dict[str, Any]]]:
    """
    Validate Google Calendar push notification headers against database:
    - X-Goog-Channel-ID
    - X-Goog-Channel-Token
    - X-Goog-Resource-ID
    Returns (is_valid, http_code, message, channel_record).
    """
    init_reminders_db(db_path)
    import sqlite3

    # Case-insensitive header lookup
    norm_headers = {k.lower(): v for k, v in headers.items()}
    channel_id = norm_headers.get("x-goog-channel-id")
    channel_token = norm_headers.get("x-goog-channel-token")
    resource_id = norm_headers.get("x-goog-resource-id")
    resource_state = norm_headers.get("x-goog-resource-state", "").lower()

    if not channel_id:
        return False, 400, "Missing X-Goog-Channel-ID header", None

    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        c = conn.cursor()
        c.execute('''
            SELECT channel_id, resource_id, token, expiration, status 
            FROM calendar_watch_channels 
            WHERE channel_id = ?
        ''', (channel_id,))
        row = c.fetchone()
    finally:
        conn.close()

    if not row:
        return False, 404, f"Channel ID '{channel_id}' not found", None

    stored_ch_id, stored_res_id, stored_token, stored_exp, stored_status = row

    if stored_status != "ACTIVE":
        return False, 403, f"Channel '{channel_id}' is {stored_status}", None

    # Constant-time comparison for security token
    if stored_token:
        if not channel_token or not hmac.compare_digest(channel_token.encode("utf-8"), stored_token.encode("utf-8")):
            return False, 401, "Invalid X-Goog-Channel-Token", None

    # Verify resource ID (skip check on initial 'sync' if resource_id not yet provided)
    if resource_id and stored_res_id:
        if resource_id != stored_res_id:
            return False, 403, "X-Goog-Resource-ID mismatch", None

    channel_info = {
        "channel_id": stored_ch_id,
        "resource_id": stored_res_id,
        "resource_state": resource_state,
        "token": stored_token
    }
    return True, 200, "Valid Calendar notification headers", channel_info

def process_calendar_push_notification(
    headers: Dict[str, str],
    db_path: str = DEFAULT_DB,
    calendar_service: Any = None
) -> Tuple[int, Dict[str, Any]]:
    """
    Process Google Calendar push notification using incremental synchronization.
    Handles:
    - Initial 'sync' state (acknowledges without running diff).
    - Event updates & incremental sync.
    - Deleted / cancelled events -> cancels pending reminders.
    - Event date changes -> updates SQLite & pending reminders (WITHOUT patching Calendar again).
    - HTTP 410 Gone recovery by performing full resynchronization.
    - Idempotency & deduplication via webhook_events.
    """
    init_reminders_db(db_path)
    import sqlite3

    is_valid, code, msg, ch_info = verify_calendar_webhook_headers(headers, db_path=db_path)
    if not is_valid:
        logger.warning(f"[CALENDAR WEBHOOK] Header verification failed: {msg} (HTTP {code})")
        return code, {"error": msg}

    channel_id = ch_info["channel_id"]
    resource_state = ch_info["resource_state"]
    
    norm_headers = {k.lower(): v for k, v in headers.items()}
    message_number = norm_headers.get("x-goog-message-number", "0")
    event_id = f"gcal_{channel_id}_msg_{message_number}"

    # Idempotency check via webhook_events table
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        c = conn.cursor()
        c.execute("SELECT status FROM webhook_events WHERE event_id = ?", (event_id,))
        dup_row = c.fetchone()
        if dup_row and dup_row[0] == "PROCESSED":
            logger.info(f"[CALENDAR WEBHOOK] Duplicate notification '{event_id}' already processed. Acknowledging with 200.")
            return 200, {"status": "DUPLICATE_IGNORED", "event_id": event_id}

        now_iso = datetime.datetime.now().isoformat()
        c.execute('''
            INSERT OR REPLACE INTO webhook_events (event_id, source, event_type, received_at, status)
            VALUES (?, 'GOOGLE_CALENDAR', ?, ?, 'PROCESSING')
        ''', (event_id, resource_state, now_iso))
        conn.commit()
    finally:
        conn.close()

    # Initial sync handshake notification
    if resource_state == "sync":
        logger.info(f"[CALENDAR WEBHOOK] Received initial sync notification for channel {channel_id}. Handshake acknowledged.")
        # Ensure a valid synchronization baseline exists for this channel
        conn = sqlite3.connect(db_path, timeout=10.0)
        try:
            c = conn.cursor()
            c.execute("SELECT sync_token FROM calendar_sync_state WHERE channel_id = ?", (channel_id,))
            token_row = c.fetchone()
            has_token = bool(token_row and token_row[0])
        finally:
            conn.close()

        # If sync token is missing, establish full sync baseline now so the app is never left without one
        if not has_token and calendar_service:
            logger.info(f"[CALENDAR WEBHOOK] Establishing initial sync baseline for channel {channel_id}.")
            try:
                page_token = None
                while True:
                    kwargs = {"calendarId": "primary"}
                    if page_token:
                        kwargs["pageToken"] = page_token
                    list_resp = calendar_service.events().list(**kwargs).execute()
                    if list_resp.get("nextSyncToken"):
                        fresh_token = list_resp["nextSyncToken"]
                        conn = sqlite3.connect(db_path, timeout=10.0)
                        c = conn.cursor()
                        c.execute('''
                            INSERT OR REPLACE INTO calendar_sync_state (channel_id, sync_token, last_synced_at)
                            VALUES (?, ?, ?)
                        ''', (channel_id, fresh_token, datetime.datetime.now().isoformat()))
                        conn.commit()
                        conn.close()
                        break
                    page_token = list_resp.get("nextPageToken")
                    if not page_token:
                        break
            except Exception as init_err:
                logger.warning(f"[CALENDAR WEBHOOK] Warning establishing sync baseline: {init_err}")

        _mark_event_processed(db_path, event_id)
        return 200, {
            "status": "HANDSHAKE_ACKNOWLEDGED",
            "channel_id": channel_id,
            "resource_state": "sync"
        }

    if not calendar_service:
        logger.error("[CALENDAR WEBHOOK] Calendar service instance required to synchronize events.")
        _mark_event_failed(db_path, event_id, "Missing calendar_service instance")
        return 500, {"error": "Calendar service unavailable"}

    # Fetch stored sync token for incremental synchronization
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        c = conn.cursor()
        c.execute("SELECT sync_token FROM calendar_sync_state WHERE channel_id = ?", (channel_id,))
        token_row = c.fetchone()
        sync_token = token_row[0] if token_row else None
    finally:
        conn.close()

    items = []
    new_sync_token = None
    recovered_from_410 = False

    try:
        # Perform incremental synchronization
        if sync_token:
            try:
                resp = calendar_service.events().list(
                    calendarId="primary",
                    syncToken=sync_token
                ).execute()
                items = resp.get("items", [])
                new_sync_token = resp.get("nextSyncToken")
            except Exception as sync_err:
                # Handle HTTP 410: sync token expired / invalidated
                err_str = str(sync_err)
                if "410" in err_str or "full sync required" in err_str.lower():
                    logger.warning(f"[CALENDAR WEBHOOK] Sync token expired (HTTP 410). Performing full resynchronization.")
                    recovered_from_410 = True
                    resp = calendar_service.events().list(calendarId="primary").execute()
                    items = resp.get("items", [])
                    new_sync_token = resp.get("nextSyncToken")
                else:
                    raise sync_err
        else:
            # First sync without stored sync token
            resp = calendar_service.events().list(calendarId="primary").execute()
            items = resp.get("items", [])
            new_sync_token = resp.get("nextSyncToken")

    except Exception as api_err:
        logger.error(f"[CALENDAR WEBHOOK] Calendar API sync failed: {api_err}", exc_info=True)
        _mark_event_failed(db_path, event_id, str(api_err))
        return 500, {"error": f"Calendar API sync error: {api_err}"}

    # Process retrieved changed events
    actions_taken = []
    events_processed = 0

    for event in items:
        cal_id = event.get("id")
        if not cal_id:
            continue

        events_processed += 1
        status = event.get("status", "")

        # 1. Event Deleted or Cancelled
        if status == "cancelled":
            logger.info(f"[CALENDAR WEBHOOK] Event {cal_id} was deleted/cancelled. Cancelling linked reminders.")
            del_res = handle_calendar_event_deleted(cal_id, db_path=db_path)
            actions_taken.append({
                "action": "CANCEL_DELETED_EVENT",
                "google_event_id": cal_id,
                "details": del_res
            })
            continue

        # 2. Event Date / Deadline Changed
        # Check start date from Google event: 'date' for all-day or 'dateTime'[:10]
        start_obj = event.get("start") or {}
        event_date = start_obj.get("date")
        if not event_date and start_obj.get("dateTime"):
            event_date = str(start_obj.get("dateTime"))[:10]

        if event_date:
            # Check if stored deadline in calendar_events differs
            conn = sqlite3.connect(db_path, timeout=10.0)
            try:
                c = conn.cursor()
                c.execute("SELECT deadline FROM calendar_events WHERE google_event_id = ?", (cal_id,))
                cur_cal_row = c.fetchone()
            finally:
                conn.close()

            if cur_cal_row and cur_cal_row[0] != event_date:
                logger.info(f"[CALENDAR WEBHOOK] Event {cal_id} deadline changed from '{cur_cal_row[0]}' to '{event_date}'. Rescheduling reminders without re-patching Calendar.")
                # Requirement C: separate function that does NOT patch Calendar again
                resched_res = update_calendar_event_deadline_and_reschedule(
                    google_event_id=cal_id,
                    new_deadline=event_date,
                    db_path=db_path
                )
                actions_taken.append({
                    "action": "RESCHEDULE_CALENDAR_DEADLINE",
                    "google_event_id": cal_id,
                    "new_deadline": event_date,
                    "details": resched_res
                })

    # Update stored sync token
    if new_sync_token:
        conn = sqlite3.connect(db_path, timeout=10.0)
        try:
            c = conn.cursor()
            c.execute('''
                INSERT OR REPLACE INTO calendar_sync_state (channel_id, sync_token, last_synced_at)
                VALUES (?, ?, ?)
            ''', (channel_id, new_sync_token, datetime.datetime.now().isoformat()))
            conn.commit()
        finally:
            conn.close()

    _mark_event_processed(db_path, event_id)

    return 200, {
        "status": "SUCCESS",
        "event_id": event_id,
        "channel_id": channel_id,
        "events_examined": events_processed,
        "recovered_from_410": recovered_from_410,
        "actions_taken": actions_taken
    }

def _mark_event_processed(db_path: str, event_id: str):
    """Mark webhook event as successfully PROCESSED."""
    try:
        import sqlite3
        conn = sqlite3.connect(db_path, timeout=10.0)
        c = conn.cursor()
        c.execute('''
            UPDATE webhook_events 
            SET status = 'PROCESSED', processed_at = ? 
            WHERE event_id = ?
        ''', (datetime.datetime.now().isoformat(), event_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[CALENDAR WEBHOOK] Failed to mark event '{event_id}' processed: {e}")

def _mark_event_failed(db_path: str, event_id: str, error_msg: str):
    """Safely mark event as FAILED in webhook_events audit table."""
    try:
        import sqlite3
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
        logger.error(f"[CALENDAR WEBHOOK] Failed to update webhook_events status for '{event_id}': {db_err}")

def build_cli_parser():
    import argparse
    parser = argparse.ArgumentParser(description="MeetMind AI Google Calendar Webhook Maintenance")
    parser.add_argument("--register", type=str, help="Public HTTPS webhook URL to register")
    parser.add_argument("--renew", action="store_true", help="Renew expiring channels")
    parser.add_argument("--url", type=str, default="", help="Webhook URL for renewal (optional if already stored in channel record)")
    parser.add_argument("--lead-hours", type=int, default=24, help="Lead hours before expiration to renew")
    return parser

if __name__ == "__main__":
    from google_calendar_service import get_calendar_service

    parser = build_cli_parser()
    args = parser.parse_args()

    svc, err = get_calendar_service()
    if not svc:
        print(f"Error: Calendar service unavailable: {err}")
        sys.exit(1)

    if args.register:
        res = register_calendar_watch_channel(svc, args.register)
        print("Registration result:", res)
    elif args.renew:
        url = args.url or os.environ.get("CALENDAR_WEBHOOK_URL", "")
        res = renew_expired_calendar_watch_channels(svc, webhook_url=url or None, lead_time_hours=args.lead_hours)
        print(f"Renewed {len(res)} channel(s):", res)
    else:
        parser.print_help()

