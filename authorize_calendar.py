"""
authorize_calendar.py
─────────────────────
One-time OAuth 2.0 authorization script for Google Calendar API.

Run this script once to generate token_calendar.json.
A browser window will open for Google account authorization.
The resulting token is saved separately from the Gmail token so that
Calendar permissions do not affect existing Gmail functionality.

Usage:
    python authorize_calendar.py

Environment variables used:
    GOOGLE_CREDENTIALS_FILE      - Path to OAuth 2.0 client secrets file
                                   (defaults to GMAIL_CREDENTIALS_FILE, then "credentials.json")
    GOOGLE_CALENDAR_TOKEN_FILE   - Path where the Calendar token will be saved
                                   (defaults to "token_calendar.json")

SECURITY NOTICE:
    - Never commit token_calendar.json to version control.
    - token_calendar.json grants access to your Google Calendar.
    - Ensure .gitignore includes token_calendar.json.
"""

import os
import sys

from dotenv import load_dotenv

# Load project .env so GOOGLE_CREDENTIALS_FILE etc. are available
load_dotenv(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    override=True,
)

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def main() -> None:
    # Resolve paths from env or use defaults
    creds_file = (
        os.environ.get("GOOGLE_CREDENTIALS_FILE")
        or os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
    )
    token_file = os.environ.get("GOOGLE_CALENDAR_TOKEN_FILE", "token_calendar.json")

    if not os.path.exists(creds_file):
        print(
            f"[ERROR] Client secrets file not found: '{creds_file}'\n"
            "Download it from Google Cloud Console -> APIs & Services -> Credentials.\n"
            "Set GOOGLE_CREDENTIALS_FILE in .env if it has a different path."
        )
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print(
            "[ERROR] google-auth-oauthlib is not installed.\n"
            "Run: pip install google-auth-oauthlib"
        )
        sys.exit(1)

    print(f"[CALENDAR AUTH] Using credentials file: {creds_file}")
    print(f"[CALENDAR AUTH] Token will be saved to:  {token_file}")
    print("[CALENDAR AUTH] Opening browser for Google authorization...")
    print("               Scopes requested:", CALENDAR_SCOPES)

    try:
        flow = InstalledAppFlow.from_client_secrets_file(creds_file, CALENDAR_SCOPES)
        creds = flow.run_local_server(port=0)
    except Exception as e:
        print(f"[ERROR] OAuth authorization flow failed: {e}")
        sys.exit(1)

    try:
        with open(token_file, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
        print(f"\n[CALENDAR AUTH] Authorization successful.")
        print(f"[CALENDAR AUTH] Token saved to: {token_file}")
        print("[CALENDAR AUTH] You can now use Google Calendar integration in MeetMind AI.")
    except IOError as e:
        print(f"[ERROR] Failed to save token to '{token_file}': {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

