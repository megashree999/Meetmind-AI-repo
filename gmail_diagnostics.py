import os
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

def diagnose_gmail():
    print("=" * 50)
    print("GMAIL DIAGNOSTICS")
    print("=" * 50)
    
    creds_file = "credentials.json"
    token_file = "token.json"
    
    creds_exist = os.path.exists(creds_file)
    token_exist = os.path.exists(token_file)
    
    print(f"OAuth credentials file exists: {'YES' if creds_exist else 'NO'}")
    print(f"OAuth token file exists: {'YES' if token_exist else 'NO'}")
    
    creds = None
    if token_exist:
        try:
            creds = Credentials.from_authorized_user_file(token_file, ["https://www.googleapis.com/auth/gmail.send"])
        except Exception as e:
            print(f"Error loading token: {e}")
            
    refresh_status = "N/A"
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            refresh_status = "PASS"
            # save
            with open(token_file, "w", encoding="utf-8") as token:
                token.write(creds.to_json())
        except Exception as e:
            refresh_status = f"FAIL (Error: {e})"
    elif creds and not creds.expired:
        refresh_status = "PASS (Not expired)"
    else:
        refresh_status = "FAIL (No valid token or refresh token)"
        
    print(f"OAuth refresh: {refresh_status}")
    
    api_connection = "FAIL"
    init_pass = "FAIL"
    if creds and creds.valid:
        try:
            service = build("gmail", "v1", credentials=creds)
            init_pass = "PASS"
            # Try to get profile to verify connection
            profile = service.users().getProfile(userId="me").execute()
            if profile:
                api_connection = "PASS"
        except Exception as e:
            api_connection = f"FAIL (Error: {e})"
            
    print(f"Gmail service initialization: {init_pass}")
    print(f"Gmail API connection: {api_connection}")

if __name__ == "__main__":
    diagnose_gmail()
