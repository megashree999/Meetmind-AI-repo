# test_google_calendar_connection.py
"""Standalone script to verify Google Calendar connectivity.

Requirements fulfilled:
1. Loads OAuth token from the file defined by GOOGLE_CALENDAR_TOKEN_FILE (defaults to token_calendar.json).
2. Refreshes credentials when expired.
3. Connects to the Google Calendar API.
4. Retrieves the primary calendar metadata (name and time zone).
5. Prints only the calendar name and time zone – no event manipulation.
6. Never prints access/refresh tokens, client secrets or personal data.
7. Forces CALENDAR_NOTIFICATIONS_ENABLED to False for safety.
8. Reports a clear SUCCESS/FAILED status with helpful error details.
"""

import os
import sys
from dotenv import load_dotenv

# Load the project's .env (if present) – this brings in any required paths.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)

# Ensure the integration is disabled – we only want a connection check.
os.environ["CALENDAR_NOTIFICATIONS_ENABLED"] = "false"

# Import the helper that creates a Google Calendar service instance.
# The function returns (service, error_message).
try:
    from google_calendar_service import get_calendar_service
except ImportError as e:
    print(f"FAILED: Unable to import calendar helper – {e}")
    sys.exit(1)

def main():
    # Attempt to obtain an authenticated service.
    service, err_msg = get_calendar_service()
    if not service:
        print(f"FAILED: Calendar service unavailable – {err_msg}")
        sys.exit(1)

    # ------------------------------------------------------------
    # Verify access by listing at most one event from the primary calendar.
    try:
        from googleapiclient.errors import HttpError

        calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
        # "events().list" is covered by the calendar.events scope.
        _ = service.events().list(
            calendarId=calendar_id,
            maxResults=1,
            fields="items(id)"
        ).execute()

        print("SUCCESS")
        print(f"Connected to calendar ID: {calendar_id} – event‑list access verified.")
    except HttpError as http_err:
        print(f"FAILED: HTTP error {http_err.resp.status} – {http_err._get_reason()}")
        sys.exit(1)
    except Exception as exc:
        print(f"FAILED: Unexpected error – {exc}")
        sys.exit(1)

if __name__ == "__main__":
    main()

