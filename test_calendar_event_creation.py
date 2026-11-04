# test_calendar_event_creation.py
"""
Standalone script that creates ONE real Google Calendar event (and stores its ID in the SQLite `calendar_events` table).

Features
--------
* Uses the real OAuth flow in `google_calendar_service.py`.
* Guarantees a single test event – duplicates are detected and skipped.
* No attendees, no email notifications.
* Prints a clear SUCCESS line with the event ID and HTML link.
* Leaves all existing production settings untouched (CALENDAR_NOTIFICATIONS_ENABLED
  remains whatever the user set; we force a real call with force_simulate=False).
"""

import os
import sys
import datetime
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 1. Load the project's .env (makes token paths and env vars available)
# ---------------------------------------------------------------------------
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ---------------------------------------------------------------------------
# 2. Import the calendar helpers (exact signatures as defined in google_calendar_service.py)
# ---------------------------------------------------------------------------
try:
    from google_calendar_service import (
        create_calendar_event,
        is_duplicate_calendar_event,
    )
except ImportError as e:
    print(f"FAILED: Could not import calendar helpers – {e}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# 3. Define a deterministic test‑event description
# ---------------------------------------------------------------------------
EVENT_TITLE = "MeetMind AI - Calendar Integration Test"
ASSIGNEE = "Test User"                     # a name that will *not* resolve to a real email
MEETING_ID = "CalendarIntegrationTest"      # unique marker used for duplicate detection

# Fixed future deadline (ISO YYYY‑MM‑DD). Using a date far enough in the future
# ensures the same value is used on every run, so duplicate detection works.
DEADLINE_STR = "2027-01-01"

def main():
    # ---------------------------------------------------------------------------
    # 4. Duplicate‑check – skip creation if we already have this test event
    # ---------------------------------------------------------------------------
    existing_event_id = is_duplicate_calendar_event(
        assignee=ASSIGNEE,
        task=EVENT_TITLE,
        deadline=DEADLINE_STR,
        meeting=MEETING_ID,
    )

    if existing_event_id:
        print("SUCCESS")
        print(
            f"Duplicate test event already exists – Event ID: {existing_event_id}"
        )
        sys.exit(0)

    # ---------------------------------------------------------------------------
    # 5. Create the real calendar event (force_simulate=False forces a real API call)
    # ---------------------------------------------------------------------------
    result = create_calendar_event(
        task=EVENT_TITLE,
        assignee=ASSIGNEE,
        deadline=DEADLINE_STR,
        task_size="SMALL",
        meeting=MEETING_ID,
        force_simulate=False,   # explicit override – we really want the event created
    )

    status = result.get("status")
    event_id = result.get("event_id")
    html_link = result.get("html_link")

    if status == "SUCCESS" and event_id:
        print("SUCCESS")
        print(f"Event ID   : {event_id}")
        print(f"Event Link : {html_link or '(no link returned)'}")
    else:
        # Propagate the error message returned by the service for debugging.
        error_msg = result.get("error") or result.get("message") or "unknown error"
        print(f"FAILED: {error_msg}")
        sys.exit(1)

if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# End of script – no further side‑effects.
# ---------------------------------------------------------------------------

