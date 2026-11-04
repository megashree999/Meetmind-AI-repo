import os
import sys
import sqlite3
import datetime
import json
import hmac
import hashlib
import unittest
from unittest.mock import MagicMock

# Ensure stdout handles UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(PROJECT_ROOT, "test_webhooks_sandbox.db")
TEST_SECRET = "test_jira_secret_abc123"
os.environ["JIRA_WEBHOOK_SECRET"] = TEST_SECRET
os.environ["CALENDAR_WEBHOOK_SECRET"] = "test_cal_token_xyz789"

from reminder_service import init_reminders_db, schedule_reminders
from jira_webhook import (
    verify_jira_webhook_signature,
    verify_jira_webhook_auth,
    process_jira_webhook_event
)
from calendar_webhook import (
    verify_calendar_webhook_headers,
    process_calendar_push_notification,
    register_calendar_watch_channel,
    renew_expired_calendar_watch_channels,
    build_cli_parser
)
from webhook_server import create_app

from jira_test_helper import (
    seed_test_record,
    clean_test_records,
    inspect_records,
    is_production_db,
    DEFAULT_TEST_DB,
    PROD_DB
)

class TestWebhookIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_env = os.environ.copy()
        os.environ["JIRA_WEBHOOK_SECRET"] = TEST_SECRET
        os.environ["CALENDAR_WEBHOOK_SECRET"] = "test_cal_token_xyz789"
        init_reminders_db(TEST_DB)

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls._original_env)
        if os.path.exists(TEST_DB):
            try:
                os.remove(TEST_DB)
            except Exception:
                pass

    def setUp(self):
        self._test_env = os.environ.copy()
        os.environ["JIRA_WEBHOOK_SECRET"] = TEST_SECRET
        os.environ["CALENDAR_WEBHOOK_SECRET"] = "test_cal_token_xyz789"
        init_reminders_db(TEST_DB)
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("DELETE FROM reminders")
        c.execute("DELETE FROM calendar_events")
        c.execute("DELETE FROM webhook_events")
        c.execute("DELETE FROM calendar_watch_channels")
        c.execute("DELETE FROM calendar_sync_state")
        conn.commit()
        conn.close()

        self.mock_calendar_service = MagicMock()
        self.today = datetime.date.today()
        self.tomorrow_str = (self.today + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        self.five_days_str = (self.today + datetime.timedelta(days=5)).strftime("%Y-%m-%d")
        self.ten_days_str = (self.today + datetime.timedelta(days=10)).strftime("%Y-%m-%d")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._test_env)

    def _compute_jira_sig(self, raw_body: bytes) -> str:
        h = hmac.new(TEST_SECRET.encode("utf-8"), msg=raw_body, digestmod=hashlib.sha256).hexdigest()
        return f"sha256={h}"

    def _seed_calendar_event(self, event_id: str, assignee: str, task: str, deadline: str, meeting: str = "Sprint Sync"):
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT OR REPLACE INTO calendar_events (google_event_id, assignee, email, task, deadline, meeting, status, created_at)
            VALUES (?, ?, 'priya@example.com', ?, ?, ?, 'CREATED', ?)
        ''', (event_id, assignee, task, deadline, meeting, datetime.datetime.now().isoformat()))
        conn.commit()
        conn.close()

    def _seed_reminder(self, assignee: str, task: str, deadline: str, task_size: str, google_event_id: str, task_id: str):
        action_item = {
            "task": task,
            "assignee": assignee,
            "deadline": deadline,
            "task_size": task_size,
            "google_event_id": google_event_id,
            "jira_issue_key": task_id
        }
        schedule_reminders([action_item], "Sprint Sync", db_path=TEST_DB)

    # 1. Route definitions in Starlette app
    def test_webhook_server_route_definitions(self):
        """Starlette app must define /health, /webhooks/jira and /webhooks/google-calendar routes."""
        app = create_app(db_path=TEST_DB)
        paths = [route.path for route in app.routes]
        self.assertIn("/health", paths)
        self.assertIn("/webhooks/jira", paths)
        self.assertIn("/webhooks/google-calendar", paths)

    # 2. Jira HMAC-SHA256 signature verification (genuine Jira Cloud X-Hub-Signature header)
    def test_jira_hmac_sha256_signature_verification(self):
        """verify_jira_webhook_signature correctly verifies X-Hub-Signature HMAC-SHA256 digests."""
        payload = {"issue": {"key": "PROJ-101"}, "timestamp": 12345678}
        raw_body = json.dumps(payload).encode("utf-8")
        valid_sig = self._compute_jira_sig(raw_body)

        # 1. Valid signature with sha256= prefix
        self.assertTrue(verify_jira_webhook_signature(raw_body, valid_sig))

        # 2. Valid signature without prefix
        raw_hex = valid_sig.split("=")[1]
        self.assertTrue(verify_jira_webhook_signature(raw_body, raw_hex))

        # 3. Tampered payload
        tampered_body = json.dumps({"issue": {"key": "PROJ-999"}}).encode("utf-8")
        self.assertFalse(verify_jira_webhook_signature(tampered_body, valid_sig))

        # 4. Tampered signature / invalid signature
        self.assertFalse(verify_jira_webhook_signature(raw_body, "sha256=0123456789abcdef0123456789abcdef"))

        # 5. Missing signature
        self.assertFalse(verify_jira_webhook_signature(raw_body, None))
        self.assertFalse(verify_jira_webhook_signature(raw_body, ""))

        # 6. Missing webhook secret -> MUST be rejected
        del os.environ["JIRA_WEBHOOK_SECRET"]
        self.assertFalse(verify_jira_webhook_signature(raw_body, valid_sig))

        # 7. Empty webhook secret -> MUST be rejected
        os.environ["JIRA_WEBHOOK_SECRET"] = "   "
        self.assertFalse(verify_jira_webhook_signature(raw_body, valid_sig))

        # 8. NO fallback to JIRA_API_TOKEN -> MUST be rejected even if JIRA_API_TOKEN exists
        os.environ["JIRA_WEBHOOK_SECRET"] = ""
        os.environ["JIRA_API_TOKEN"] = "some_api_token_123"
        self.assertFalse(verify_jira_webhook_signature(raw_body, valid_sig))

        # Restore valid test secret
        os.environ["JIRA_WEBHOOK_SECRET"] = TEST_SECRET

    # 3. Jira authentication header rules & query parameter deprecation
    def test_jira_auth_headers_and_query_deprecation(self):
        """Production Jira authentication requires signature or proxy headers; query-string secret is deprecated."""
        payload = {"issue": {"key": "PROJ-101"}}
        raw_body = json.dumps(payload).encode("utf-8")
        valid_sig = self._compute_jira_sig(raw_body)

        # 1. Valid X-Hub-Signature succeeds
        self.assertTrue(verify_jira_webhook_auth(raw_body, signature_header=valid_sig))

        # 2. Proxy header fallback succeeds
        self.assertTrue(verify_jira_webhook_auth(raw_body, custom_header=TEST_SECRET))
        self.assertTrue(verify_jira_webhook_auth(raw_body, auth_header=f"Bearer {TEST_SECRET}"))

        # 3. Query string secret is REJECTED by default in production
        if "ALLOW_INSECURE_QUERY_WEBHOOK_SECRET" in os.environ:
            del os.environ["ALLOW_INSECURE_QUERY_WEBHOOK_SECRET"]
        self.assertFalse(verify_jira_webhook_auth(raw_body, secret_param=TEST_SECRET))

        # 4. Query string secret accepted only if explicit override is set
        os.environ["ALLOW_INSECURE_QUERY_WEBHOOK_SECRET"] = "true"
        try:
            self.assertTrue(verify_jira_webhook_auth(raw_body, secret_param=TEST_SECRET))
        finally:
            del os.environ["ALLOW_INSECURE_QUERY_WEBHOOK_SECRET"]

    # 4. Jira task completion cancels only relevant pending reminders (preserves SENT and unrelated tasks)
    def test_jira_completion_cancels_only_pending_reminders(self):
        """Jira status transition to Done cancels pending reminders for matching task, preserving SENT rows."""
        target_task_id = "ENG-401"
        unrelated_task_id = "ENG-999"

        self._seed_calendar_event("cal_eng_401", "Priya", "Target task", self.five_days_str)
        self._seed_reminder("Priya", "Target task", self.five_days_str, "LARGE", "cal_eng_401", target_task_id)

        self._seed_calendar_event("cal_eng_999", "Ali", "Unrelated task", self.five_days_str)
        self._seed_reminder("Ali", "Unrelated task", self.five_days_str, "SMALL", "cal_eng_999", unrelated_task_id)

        # Simulate one reminder already SENT
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT reminder_id FROM reminders WHERE task_id=? ORDER BY reminder_id ASC", (target_task_id,))
        rem_ids = [r[0] for r in c.fetchall()]
        first_rem_id = rem_ids[0]
        second_rem_id = rem_ids[1]

        c.execute("UPDATE reminders SET status='SENT', sent_at='2026-11-01T09:00:00' WHERE reminder_id=?", (first_rem_id,))
        conn.commit()
        conn.close()

        payload = {
            "timestamp": 1700000001,
            "webhookEvent": "jira:issue_updated",
            "issue": {
                "key": target_task_id,
                "fields": {
                    "summary": "Target task",
                    "status": {"name": "Done", "statusCategory": {"key": "done", "name": "Done"}}
                }
            },
            "changelog": {"items": [{"field": "status", "toString": "Done"}]}
        }
        raw_body = json.dumps(payload).encode("utf-8")

        code, resp = process_jira_webhook_event(payload, raw_body, db_path=TEST_DB)
        self.assertEqual(code, 200)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        # First row (SENT) must remain SENT
        c.execute("SELECT status, sent_at FROM reminders WHERE reminder_id=?", (first_rem_id,))
        row1 = c.fetchone()
        self.assertEqual(row1[0], "SENT")
        self.assertEqual(row1[1], "2026-11-01T09:00:00")

        # Second row (PENDING) must be CANCELLED
        c.execute("SELECT status FROM reminders WHERE reminder_id=?", (second_rem_id,))
        row2 = c.fetchone()
        self.assertEqual(row2[0], "CANCELLED")

        # Row 3 (Unrelated task) must remain PENDING
        c.execute("SELECT status FROM reminders WHERE task_id=?", (unrelated_task_id,))
        row3 = c.fetchone()
        self.assertEqual(row3[0], "PENDING")
        conn.close()

    # 5. Jira deadline update reschedules pending reminders and patches Google Calendar
    def test_jira_deadline_update_reschedules_reminders_and_patches_calendar(self):
        """Jira duedate change patches Google Calendar and updates reminder_date in SQLite."""
        issue_key = "ENG-402"
        task_desc = "Setup Kubernetes autoscaler"
        self._seed_calendar_event("cal_eng_402", "Priya", task_desc, self.five_days_str)
        self._seed_reminder("Priya", task_desc, self.five_days_str, "LARGE", "cal_eng_402", issue_key)

        new_deadline = self.ten_days_str
        payload = {
            "timestamp": 1700000002,
            "webhookEvent": "jira:issue_updated",
            "issue": {
                "key": issue_key,
                "fields": {"summary": task_desc, "duedate": new_deadline}
            },
            "changelog": {"items": [{"field": "duedate", "fromString": self.five_days_str, "toString": new_deadline}]}
        }
        raw_body = json.dumps(payload).encode("utf-8")

        code, resp = process_jira_webhook_event(
            payload=payload,
            raw_body=raw_body,
            db_path=TEST_DB,
            calendar_service=self.mock_calendar_service
        )
        self.assertEqual(code, 200)
        self.assertIn("RESCHEDULED_REMINDERS", resp["actions_taken"])

        # Google Calendar event patched exactly once (deduplicated across the 2 large task reminders)
        self.mock_calendar_service.events().patch.assert_called_once()

        # Reminders updated
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT reminder_date, deadline, status FROM reminders WHERE task_id=? ORDER BY reminder_date ASC", (issue_key,))
        rows = c.fetchall()
        conn.close()

        self.assertEqual(len(rows), 2)
        expected_2d = (self.today + datetime.timedelta(days=8)).strftime("%Y-%m-%d")
        expected_1d = (self.today + datetime.timedelta(days=9)).strftime("%Y-%m-%d")
        self.assertEqual(rows[0][0], expected_2d)
        self.assertEqual(rows[1][0], expected_1d)
        self.assertEqual(rows[0][1], new_deadline)

    # 6. Jira assignee update changes reminder recipient
    def test_jira_assignee_change_updates_ownership_and_email(self):
        """Jira assignee change updates assignee and verified recipient email in SQLite."""
        issue_key = "ENG-403"
        task_desc = "Refactor auth middleware"
        self._seed_calendar_event("cal_eng_403", "Priya", task_desc, self.five_days_str)
        self._seed_reminder("Priya", task_desc, self.five_days_str, "MEDIUM", "cal_eng_403", issue_key)

        payload = {
            "timestamp": 1700000003,
            "webhookEvent": "jira:issue_updated",
            "issue": {
                "key": issue_key,
                "fields": {"summary": task_desc, "assignee": {"displayName": "Ali"}}
            },
            "changelog": {"items": [{"field": "assignee", "fromString": "Priya", "toString": "Ali"}]}
        }
        raw_body = json.dumps(payload).encode("utf-8")

        code, resp = process_jira_webhook_event(payload, raw_body, db_path=TEST_DB)
        self.assertEqual(code, 200)
        self.assertIn("UPDATED_ASSIGNEE", resp["actions_taken"])

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT assignee, email FROM reminders WHERE task_id=?", (issue_key,))
        row = c.fetchone()
        conn.close()

        self.assertEqual(row[0], "Ali")
        self.assertIn("@", row[1])

    # 7. Jira event deduplication with X-Atlassian-Webhook-Identifier
    def test_jira_atlassian_webhook_identifier_deduplication(self):
        """Uses X-Atlassian-Webhook-Identifier as primary key: retries ignored, separate events processed."""
        issue_key = "ENG-404"
        task_desc = "Configure TLS termination"
        self._seed_calendar_event("cal_eng_404", "Priya", task_desc, self.five_days_str)
        self._seed_reminder("Priya", task_desc, self.five_days_str, "MEDIUM", "cal_eng_404", issue_key)

        payload = {
            "timestamp": 1700000004,
            "webhookEvent": "jira:issue_updated",
            "issue": {"id": "10044", "key": issue_key, "fields": {"status": {"name": "Done"}}},
            "changelog": {"items": [{"field": "status", "toString": "Done"}]}
        }
        raw_body = json.dumps(payload).encode("utf-8")

        # 1. First delivery with X-Atlassian-Webhook-Identifier
        event_uuid_1 = "550e8400-e29b-41d4-a716-446655440000"
        headers_1 = {"X-Atlassian-Webhook-Identifier": event_uuid_1}
        code1, resp1 = process_jira_webhook_event(payload, raw_body, db_path=TEST_DB, headers=headers_1)
        self.assertEqual(code1, 200)
        self.assertEqual(resp1["status"], "SUCCESS")
        self.assertEqual(resp1["event_id"], f"jira_wh_{event_uuid_1}")

        # 2. Retry delivery with SAME X-Atlassian-Webhook-Identifier -> MUST be ignored as duplicate
        code2, resp2 = process_jira_webhook_event(payload, raw_body, db_path=TEST_DB, headers=headers_1)
        self.assertEqual(code2, 200)
        self.assertEqual(resp2["status"], "DUPLICATE_IGNORED")
        self.assertEqual(resp2["event_id"], f"jira_wh_{event_uuid_1}")

        # 3. Separate legitimate event with IDENTICAL payload but DIFFERENT identifier -> MUST be processed
        event_uuid_2 = "660e8400-e29b-41d4-a716-446655440001"
        headers_2 = {"X-Atlassian-Webhook-Identifier": event_uuid_2}
        code3, resp3 = process_jira_webhook_event(payload, raw_body, db_path=TEST_DB, headers=headers_2)
        self.assertEqual(code3, 200)
        self.assertEqual(resp3["status"], "SUCCESS")
        self.assertEqual(resp3["event_id"], f"jira_wh_{event_uuid_2}")

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT event_id, status FROM webhook_events WHERE event_id IN (?, ?)", (f"jira_wh_{event_uuid_1}", f"jira_wh_{event_uuid_2}"))
        rows = c.fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        for _, status in rows:
            self.assertEqual(status, "PROCESSED")

    # 7b. Jira SCRUM-105 full lifecycle simulation (Status, Deadline, Assignee, Deduplication)
    def test_jira_scrum_105_full_lifecycle_simulation(self):
        """End-to-end verification of SCRUM-105: deadline update, reassignment, completion, and deduplication."""
        issue_key = "SCRUM-105"
        task_desc = "Verify Jira webhook integration for SCRUM-105"
        self._seed_calendar_event("cal_scrum_105", "Priya", task_desc, self.five_days_str)
        self._seed_reminder("Priya", task_desc, self.five_days_str, "LARGE", "cal_scrum_105", issue_key)

        # 1. Deadline Update to ten_days_str
        new_deadline = self.ten_days_str
        payload_deadline = {
            "timestamp": 1700000101,
            "webhookEvent": "jira:issue_updated",
            "issue": {
                "id": "10105",
                "key": issue_key,
                "fields": {"summary": task_desc, "duedate": new_deadline}
            },
            "changelog": {"items": [{"field": "duedate", "fromString": self.five_days_str, "toString": new_deadline}]}
        }
        body_deadline = json.dumps(payload_deadline).encode("utf-8")
        sig_deadline = self._compute_jira_sig(body_deadline)
        self.assertTrue(verify_jira_webhook_auth(raw_body=body_deadline, signature_header=sig_deadline))

        code, resp = process_jira_webhook_event(
            payload=payload_deadline,
            raw_body=body_deadline,
            db_path=TEST_DB,
            calendar_service=self.mock_calendar_service,
            headers={"X-Atlassian-Webhook-Identifier": "delivery-scrum105-deadline-001"}
        )
        self.assertEqual(code, 200)
        self.assertIn("RESCHEDULED_REMINDERS", resp["actions_taken"])

        # Verify reminders rescheduled
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT reminder_date, deadline, status FROM reminders WHERE task_id=? ORDER BY reminder_date ASC", (issue_key,))
        rows = c.fetchall()
        self.assertEqual(len(rows), 2)
        expected_2d = (self.today + datetime.timedelta(days=8)).strftime("%Y-%m-%d")
        expected_1d = (self.today + datetime.timedelta(days=9)).strftime("%Y-%m-%d")
        self.assertEqual(rows[0][0], expected_2d)
        self.assertEqual(rows[1][0], expected_1d)
        self.assertEqual(rows[0][1], new_deadline)

        # 2. Assignee Change to Megashree
        payload_assignee = {
            "timestamp": 1700000102,
            "webhookEvent": "jira:issue_updated",
            "issue": {
                "id": "10105",
                "key": issue_key,
                "fields": {"summary": task_desc, "assignee": {"displayName": "Megashree"}}
            },
            "changelog": {"items": [{"field": "assignee", "fromString": "Priya", "toString": "Megashree"}]}
        }
        body_assignee = json.dumps(payload_assignee).encode("utf-8")
        code, resp = process_jira_webhook_event(
            payload=payload_assignee,
            raw_body=body_assignee,
            db_path=TEST_DB,
            headers={"X-Atlassian-Webhook-Identifier": "delivery-scrum105-assignee-002"}
        )
        self.assertEqual(code, 200)
        self.assertIn("UPDATED_ASSIGNEE", resp["actions_taken"])

        c.execute("SELECT assignee, email FROM reminders WHERE task_id=?", (issue_key,))
        assignee_rows = c.fetchall()
        for a_name, a_email in assignee_rows:
            self.assertEqual(a_name, "Megashree")
            self.assertEqual(a_email, "megashree999@gmail.com")

        # 3. Status Change to Done
        payload_done = {
            "timestamp": 1700000103,
            "webhookEvent": "jira:issue_updated",
            "issue": {
                "id": "10105",
                "key": issue_key,
                "fields": {"status": {"name": "Done", "statusCategory": {"key": "done"}}}
            },
            "changelog": {"items": [{"field": "status", "fromString": "In Progress", "toString": "Done"}]}
        }
        body_done = json.dumps(payload_done).encode("utf-8")
        done_headers = {"X-Atlassian-Webhook-Identifier": "delivery-scrum105-done-003"}
        code, resp = process_jira_webhook_event(
            payload=payload_done,
            raw_body=body_done,
            db_path=TEST_DB,
            headers=done_headers
        )
        self.assertEqual(code, 200)
        self.assertIn("CANCELLED_REMINDERS", resp["actions_taken"])

        c.execute("SELECT status FROM reminders WHERE task_id=?", (issue_key,))
        statuses = [r[0] for r in c.fetchall()]
        self.assertTrue(all(s == "CANCELLED" for s in statuses))

        # 4. Idempotency test: duplicate delivery of the Done event
        code_dup, resp_dup = process_jira_webhook_event(
            payload=payload_done,
            raw_body=body_done,
            db_path=TEST_DB,
            headers=done_headers
        )
        self.assertEqual(code_dup, 200)
        self.assertEqual(resp_dup["status"], "DUPLICATE_IGNORED")
        conn.close()

    # 8. Calendar initial synchronization establishes baseline across pages
    def test_calendar_initial_sync_establishes_baseline(self):
        """register_calendar_watch_channel pages until nextSyncToken is retrieved and stored."""
        # Multi-page response mock: page 1 returns nextPageToken, page 2 returns nextSyncToken
        page1_req = MagicMock()
        page1_req.execute.return_value = {
            "items": [{"id": "evt_p1"}],
            "nextPageToken": "page_token_2"
        }
        page2_req = MagicMock()
        page2_req.execute.return_value = {
            "items": [{"id": "evt_p2"}],
            "nextSyncToken": "final_sync_token_baseline"
        }

        def list_mock(**kwargs):
            if kwargs.get("pageToken") == "page_token_2":
                return page2_req
            return page1_req

        self.mock_calendar_service.events().list.side_effect = list_mock
        self.mock_calendar_service.events().watch.return_value.execute.return_value = {
            "resourceId": "res_init_sync",
            "expiration": "1999999999999"
        }

        res = register_calendar_watch_channel(
            service=self.mock_calendar_service,
            webhook_url="https://meetmind.example.com/webhooks/google-calendar",
            db_path=TEST_DB
        )
        self.assertEqual(res["status"], "SUCCESS")
        self.assertTrue(res["sync_token_stored"])

        # Check sync token saved in SQLite
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT sync_token FROM calendar_sync_state WHERE channel_id=?", (res["channel_id"],))
        stored = c.fetchone()[0]
        conn.close()
        self.assertEqual(stored, "final_sync_token_baseline")

    # 9. Calendar initial sync handshake guarantees baseline even if missing
    def test_calendar_initial_sync_handshake_baseline_recovery(self):
        """Initial sync handshake establishes baseline if channel somehow lacked one."""
        ch_id = "test_handshake_recovery_ch"
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO calendar_watch_channels (channel_id, resource_id, token, expiration, status)
            VALUES (?, 'res_rec', 'test_cal_token_xyz789', '9999999999999', 'ACTIVE')
        ''', (ch_id,))
        conn.commit()
        conn.close()

        # Mock list returning nextSyncToken
        self.mock_calendar_service.events().list.side_effect = None
        self.mock_calendar_service.events().list.return_value.execute.return_value = {
            "items": [],
            "nextSyncToken": "handshake_recovered_baseline"
        }

        headers = {
            "X-Goog-Channel-ID": ch_id,
            "X-Goog-Channel-Token": "test_cal_token_xyz789",
            "X-Goog-Resource-ID": "res_rec",
            "X-Goog-Resource-State": "sync",
            "X-Goog-Message-Number": "1"
        }
        code, resp = process_calendar_push_notification(headers, db_path=TEST_DB, calendar_service=self.mock_calendar_service)
        self.assertEqual(code, 200)
        self.assertEqual(resp["status"], "HANDSHAKE_ACKNOWLEDGED")

        # Baseline persisted in SQLite
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT sync_token FROM calendar_sync_state WHERE channel_id=?", (ch_id,))
        stored = c.fetchone()[0]
        conn.close()
        self.assertEqual(stored, "handshake_recovered_baseline")

    # 10. Calendar channel renewal registers new channel BEFORE stopping old channel
    def test_calendar_channel_renewal_registers_before_stopping_old(self):
        """Channel renewal persists new channel and copies sync token BEFORE stopping old channel."""
        old_ch_id = "old_expiring_ch_001"
        now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
        exp_ms = now_ms + (2 * 3600 * 1000)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO calendar_watch_channels (channel_id, resource_id, token, expiration, status)
            VALUES (?, 'res_old_001', 'tok_old_001', ?, 'ACTIVE')
        ''', (old_ch_id, str(exp_ms)))
        c.execute('''
            INSERT INTO calendar_sync_state (channel_id, sync_token, last_synced_at)
            VALUES (?, 'sync_token_v99', '2026-11-01T00:00:00')
        ''', (old_ch_id,))
        conn.commit()
        conn.close()

        # Track execution order: watch() must be called before stop()
        call_order = []
        self.mock_calendar_service.events().list.side_effect = None
        self.mock_calendar_service.events().list.return_value.execute.return_value = {
            "items": [],
            "nextSyncToken": "sync_token_v99"
        }

        def watch_mock(**kwargs):
            call_order.append("watch")
            m = MagicMock()
            m.execute.return_value = {"resourceId": "res_new_999", "expiration": str(now_ms + 1000000)}
            return m

        def stop_mock(**kwargs):
            call_order.append("stop")
            m = MagicMock()
            m.execute.return_value = {}
            return m

        self.mock_calendar_service.events().watch.side_effect = watch_mock
        self.mock_calendar_service.channels().stop.side_effect = stop_mock

        renewed = renew_expired_calendar_watch_channels(
            service=self.mock_calendar_service,
            webhook_url="https://meetmind.example.com/webhooks/google-calendar",
            db_path=TEST_DB,
            lead_time_hours=24
        )
        self.assertEqual(len(renewed), 1)

        # CRUCIAL CHECK: watch occurred BEFORE stop
        self.assertEqual(call_order, ["watch", "stop"], "New channel MUST be registered before stopping old channel")

        # Verify new channel has different channel ID and status ACTIVE
        new_ch_id = renewed[0]["new_registration"]["channel_id"]
        self.assertNotEqual(new_ch_id, old_ch_id)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT status FROM calendar_watch_channels WHERE channel_id=?", (old_ch_id,))
        self.assertEqual(c.fetchone()[0], "STOPPED")
        c.execute("SELECT status FROM calendar_watch_channels WHERE channel_id=?", (new_ch_id,))
        self.assertEqual(c.fetchone()[0], "ACTIVE")
        # Sync token was copied to new channel ID
        c.execute("SELECT sync_token FROM calendar_sync_state WHERE channel_id=?", (new_ch_id,))
        self.assertEqual(c.fetchone()[0], "sync_token_v99")
        conn.close()

    # 11. Calendar date updated reschedules reminders WITHOUT infinite loop (does not patch Calendar)
    def test_calendar_event_date_updated_reschedules_without_loop(self):
        """Calendar date change reschedules reminders in SQLite but DOES NOT call calendar patch (loop prevention)."""
        ch_id = "test_resched_ch"
        cal_id = "cal_resched_evt_001"
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO calendar_watch_channels (channel_id, resource_id, token, expiration, status)
            VALUES (?, 'res_resched', 'test_cal_token_xyz789', '9999999999999', 'ACTIVE')
        ''', (ch_id,))
        conn.commit()
        conn.close()

        self._seed_calendar_event(cal_id, "Priya", "Sync loop prevention task", self.five_days_str)
        self._seed_reminder("Priya", "Sync loop prevention task", self.five_days_str, "LARGE", cal_id, "TASK-LOOP-1")

        new_date = self.ten_days_str
        self.mock_calendar_service.events().list.side_effect = None
        self.mock_calendar_service.events().list.return_value.execute.return_value = {
            "items": [{"id": cal_id, "status": "confirmed", "start": {"date": new_date}}],
            "nextSyncToken": "sync_token_v3"
        }

        self.mock_calendar_service.events().patch.reset_mock()

        headers = {
            "X-Goog-Channel-ID": ch_id,
            "X-Goog-Channel-Token": "test_cal_token_xyz789",
            "X-Goog-Resource-ID": "res_resched",
            "X-Goog-Resource-State": "exists",
            "X-Goog-Message-Number": "3"
        }
        code, resp = process_calendar_push_notification(headers, db_path=TEST_DB, calendar_service=self.mock_calendar_service)
        self.assertEqual(code, 200)

        # Requirement C check: patch must NOT be called for Calendar-originated updates
        self.mock_calendar_service.events().patch.assert_not_called()

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT deadline, reminder_date FROM reminders WHERE google_event_id=? ORDER BY reminder_date ASC", (cal_id,))
        rows = c.fetchall()
        c.execute("SELECT deadline FROM calendar_events WHERE google_event_id=?", (cal_id,))
        cal_deadline = c.fetchone()[0]
        conn.close()

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], new_date)
        self.assertEqual(rows[1][0], new_date)
        self.assertEqual(cal_deadline, new_date)

    # 12. Calendar event cancelled/deleted cancels pending reminders
    def test_calendar_event_deleted_cancels_reminders(self):
        """Calendar incremental sync retrieving status='cancelled' event cancels pending reminders."""
        ch_id = "test_delete_ch"
        cal_id = "cal_deleted_evt_001"
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO calendar_watch_channels (channel_id, resource_id, token, expiration, status)
            VALUES (?, 'res_del', 'test_cal_token_xyz789', '9999999999999', 'ACTIVE')
        ''', (ch_id,))
        conn.commit()
        conn.close()

        self._seed_calendar_event(cal_id, "Priya", "Deletable task", self.five_days_str)
        self._seed_reminder("Priya", "Deletable task", self.five_days_str, "MEDIUM", cal_id, "TASK-DEL-1")

        self.mock_calendar_service.events().list.side_effect = None
        self.mock_calendar_service.events().list.return_value.execute.return_value = {
            "items": [{"id": cal_id, "status": "cancelled"}],
            "nextSyncToken": "sync_token_v2"
        }

        headers = {
            "X-Goog-Channel-ID": ch_id,
            "X-Goog-Channel-Token": "test_cal_token_xyz789",
            "X-Goog-Resource-ID": "res_del",
            "X-Goog-Resource-State": "exists",
            "X-Goog-Message-Number": "2"
        }
        code, resp = process_calendar_push_notification(headers, db_path=TEST_DB, calendar_service=self.mock_calendar_service)
        self.assertEqual(code, 200)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT status FROM reminders WHERE google_event_id = ?", (cal_id,))
        self.assertEqual(c.fetchone()[0], "CANCELLED")
        c.execute("SELECT status FROM calendar_events WHERE google_event_id = ?", (cal_id,))
        self.assertEqual(c.fetchone()[0], "DELETED")
        conn.close()

    # 13. Expired sync token and HTTP 410 recovery
    def test_calendar_sync_token_expired_410_recovery(self):
        """When incremental sync fails with 410 (sync token expired), performs full sync and recovers."""
        ch_id = "test_410_ch"
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO calendar_watch_channels (channel_id, resource_id, token, expiration, status)
            VALUES (?, 'res_410', 'test_cal_token_xyz789', '9999999999999', 'ACTIVE')
        ''', (ch_id,))
        c.execute('''
            INSERT INTO calendar_sync_state (channel_id, sync_token, last_synced_at)
            VALUES (?, 'expired_sync_token', '2026-10-01T00:00:00')
        ''', (ch_id,))
        conn.commit()
        conn.close()

        from googleapiclient.errors import HttpError
        fake_resp = MagicMock()
        fake_resp.status = 410
        fake_resp.reason = "Gone"

        call_count = 0
        def list_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("syncToken") == "expired_sync_token":
                raise HttpError(fake_resp, b'{"error": {"code": 410, "message": "Sync token is expired"}}')
            req_mock = MagicMock()
            req_mock.execute.return_value = {
                "items": [],
                "nextSyncToken": "fresh_new_sync_token_410_recovered"
            }
            return req_mock

        self.mock_calendar_service.events().list.side_effect = list_side_effect

        headers = {
            "X-Goog-Channel-ID": ch_id,
            "X-Goog-Channel-Token": "test_cal_token_xyz789",
            "X-Goog-Resource-ID": "res_410",
            "X-Goog-Resource-State": "exists",
            "X-Goog-Message-Number": "4"
        }
        code, resp = process_calendar_push_notification(headers, db_path=TEST_DB, calendar_service=self.mock_calendar_service)
        self.assertEqual(code, 200)
        self.assertTrue(resp["recovered_from_410"])

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT sync_token FROM calendar_sync_state WHERE channel_id=?", (ch_id,))
        self.assertEqual(c.fetchone()[0], "fresh_new_sync_token_410_recovered")
        conn.close()

    # 14. Duplicate Calendar notifications deduplicated
    def test_calendar_duplicate_notification_deduplicated(self):
        """Identical Calendar push notification (same message number) is acknowledged without redundant sync."""
        ch_id = "test_dup_cal_ch"
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO calendar_watch_channels (channel_id, resource_id, token, expiration, status)
            VALUES (?, 'res_dup', 'test_cal_token_xyz789', '9999999999999', 'ACTIVE')
        ''', (ch_id,))
        conn.commit()
        conn.close()

        self.mock_calendar_service.events().list.side_effect = None
        self.mock_calendar_service.events().list.return_value.execute.return_value = {
            "items": [],
            "nextSyncToken": "sync_tok_dup"
        }

        headers = {
            "X-Goog-Channel-ID": ch_id,
            "X-Goog-Channel-Token": "test_cal_token_xyz789",
            "X-Goog-Resource-ID": "res_dup",
            "X-Goog-Resource-State": "exists",
            "X-Goog-Message-Number": "101"
        }

        code1, resp1 = process_calendar_push_notification(headers, db_path=TEST_DB, calendar_service=self.mock_calendar_service)
        self.assertEqual(code1, 200)
        self.assertEqual(resp1["status"], "SUCCESS")

        code2, resp2 = process_calendar_push_notification(headers, db_path=TEST_DB, calendar_service=self.mock_calendar_service)
        self.assertEqual(code2, 200)
        self.assertEqual(resp2["status"], "DUPLICATE_IGNORED")

    # 15. Calendar webhook rejected on invalid token or missing channel
    def test_calendar_auth_validation_rejected(self):
        """Calendar webhook rejects missing channel, invalid token, and mismatched resource ID."""
        ok1, code1, _, _ = verify_calendar_webhook_headers({}, db_path=TEST_DB)
        self.assertFalse(ok1)
        self.assertEqual(code1, 400)

        ok2, code2, _, _ = verify_calendar_webhook_headers({"X-Goog-Channel-ID": "non_existent_channel"}, db_path=TEST_DB)
        self.assertFalse(ok2)
        self.assertEqual(code2, 404)

        ch_id = "test_active_ch_001"
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO calendar_watch_channels (channel_id, resource_id, token, expiration, status)
            VALUES (?, 'res_001', 'test_cal_token_xyz789', '9999999999999', 'ACTIVE')
        ''', (ch_id,))
        conn.commit()
        conn.close()

        ok3, code3, _, _ = verify_calendar_webhook_headers({
            "X-Goog-Channel-ID": ch_id,
            "X-Goog-Channel-Token": "wrong_token",
            "X-Goog-Resource-ID": "res_001"
        }, db_path=TEST_DB)
        self.assertFalse(ok3)
        self.assertEqual(code3, 401)

        ok4, code4, _, _ = verify_calendar_webhook_headers({
            "X-Goog-Channel-ID": ch_id,
            "X-Goog-Channel-Token": "test_cal_token_xyz789",
            "X-Goog-Resource-ID": "mismatched_resource_id"
        }, db_path=TEST_DB)
        self.assertFalse(ok4)
        self.assertEqual(code4, 403)

    # 16. Google Calendar renewal failure safety (old channel remains ACTIVE if registration fails)
    def test_calendar_channel_renewal_watch_failure_keeps_old_channel_active(self):
        """If new channel registration fails, old channel must remain ACTIVE and not be stopped."""
        expiring_ch_id = "expiring_ch_fail_test"
        now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
        exp_ms = now_ms + (2 * 3600 * 1000)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO calendar_watch_channels (channel_id, resource_id, token, expiration, status, webhook_url)
            VALUES (?, 'res_fail_test', 'tok_fail_test', ?, 'ACTIVE', 'https://meetmind.ai/webhooks/google-calendar')
        ''', (expiring_ch_id, str(exp_ms)))
        conn.commit()
        conn.close()

        # Simulate Google Calendar API failure during events().watch()
        self.mock_calendar_service.events().watch.side_effect = Exception("Google API 503 Backend Error")
        self.mock_calendar_service.channels().stop.reset_mock()

        renewed = renew_expired_calendar_watch_channels(
            service=self.mock_calendar_service,
            webhook_url="https://meetmind.ai/webhooks/google-calendar",
            db_path=TEST_DB
        )

        # Renewal returns empty list
        self.assertEqual(len(renewed), 0)

        # Old channel MUST remain ACTIVE
        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT status FROM calendar_watch_channels WHERE channel_id=?", (expiring_ch_id,))
        status = c.fetchone()[0]
        conn.close()
        self.assertEqual(status, "ACTIVE", "Existing channel must remain ACTIVE if renewal registration fails")

        # Stop must NOT have been called
        self.mock_calendar_service.channels().stop.assert_not_called()

    # 17. Google Calendar channel renewal uses stored webhook_url if omitted from CLI
    def test_calendar_channel_renewal_uses_stored_webhook_url(self):
        """Renewal succeeds when webhook_url is omitted by reading stored webhook_url from SQLite."""
        expiring_ch_id = "expiring_ch_stored_url"
        stored_url = "https://stored.meetmind.ai/webhooks/google-calendar"
        now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
        exp_ms = now_ms + (2 * 3600 * 1000)

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute('''
            INSERT INTO calendar_watch_channels (channel_id, resource_id, token, expiration, status, webhook_url)
            VALUES (?, 'res_stored_url', 'tok_stored_url', ?, 'ACTIVE', ?)
        ''', (expiring_ch_id, str(exp_ms), stored_url))
        conn.commit()
        conn.close()

        self.mock_calendar_service.events().watch.side_effect = None
        self.mock_calendar_service.events().watch.return_value.execute.return_value = {
            "resourceId": "res_new_from_stored",
            "expiration": str(now_ms + 1000000)
        }
        self.mock_calendar_service.events().list.side_effect = None
        self.mock_calendar_service.events().list.return_value.execute.return_value = {
            "items": [],
            "nextSyncToken": "sync_token_stored_url"
        }

        # Call with webhook_url=None
        renewed = renew_expired_calendar_watch_channels(
            service=self.mock_calendar_service,
            webhook_url=None,
            db_path=TEST_DB
        )
        self.assertEqual(len(renewed), 1)
        self.assertEqual(renewed[0]["old_channel_id"], expiring_ch_id)

    # 18. CLI argument parser validation
    def test_calendar_cli_parser_options(self):
        """CLI parser handles --renew, --lead-hours, --url, and --register flags properly."""
        parser = build_cli_parser()

        # Command A: python calendar_webhook.py --renew --lead-hours 24
        args_a = parser.parse_args(["--renew", "--lead-hours", "24"])
        self.assertTrue(args_a.renew)
        self.assertEqual(args_a.lead_hours, 24)
        self.assertEqual(args_a.url, "")

        # Command B: python calendar_webhook.py --renew --url "https://custom.url"
        args_b = parser.parse_args(["--renew", "--url", "https://custom.url"])
        self.assertTrue(args_b.renew)
        self.assertEqual(args_b.url, "https://custom.url")

        # Command C: python calendar_webhook.py --register "https://my.url"
        args_c = parser.parse_args(["--register", "https://my.url"])
        self.assertEqual(args_c.register, "https://my.url")
        self.assertFalse(args_c.renew)

    # 19. Jira test helper database isolation & safety protection
    def test_jira_test_helper_database_isolation(self):
        """Helper must default to test_jira_helper.db, reject reminders.db, and protect production."""
        # 1. Default DB is test_jira_helper.db
        self.assertTrue(DEFAULT_TEST_DB.endswith("test_jira_helper.db"))

        # 2. Production detection
        self.assertTrue(is_production_db(PROD_DB))
        self.assertTrue(is_production_db("reminders.db"))
        self.assertFalse(is_production_db(DEFAULT_TEST_DB))

        # 3. Seed on production DB MUST be blocked with PermissionError
        with self.assertRaises(PermissionError) as ctx_seed:
            seed_test_record(PROD_DB)
        self.assertIn("SAFETY VIOLATION", str(ctx_seed.exception))

        # 4. Clean on production DB MUST be blocked with PermissionError
        with self.assertRaises(PermissionError) as ctx_clean:
            clean_test_records(PROD_DB)
        self.assertIn("SAFETY VIOLATION", str(ctx_clean.exception))

        # 5. Non-default test DB requires explicit allow_custom flag
        temp_sandbox = os.path.join(PROJECT_ROOT, "temp_sandbox_helper_test.db")
        with self.assertRaises(PermissionError) as ctx_custom:
            seed_test_record(temp_sandbox, allow_custom=False)
        self.assertIn("SAFETY CHECK", str(ctx_custom.exception))

        # Clean up any temp file if created
        if os.path.exists(temp_sandbox):
            try:
                os.remove(temp_sandbox)
            except Exception:
                pass

if __name__ == "__main__":
    unittest.main()
