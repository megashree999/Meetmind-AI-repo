import sqlite3
import datetime
import os
import base64
from email.mime.text import MIMEText
from dotenv import load_dotenv

# We reuse gmail_service to fetch the authenticated service
from gmail_service import get_gmail_service

import re

def create_reminder_email_content(assignee: str, task: str, deadline: str, meeting: str) -> tuple[str, str]:
    # Strip deadline phrase from task
    task_clean = re.sub(r'\bby\s+(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|tomorrow|next\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)|[a-zA-Z]+\s+\d{1,2}(?:st|nd|rd|th)?)\b', '', task, flags=re.IGNORECASE).strip()
    
    subject = "⏰ Reminder: Action Item Due Tomorrow"
    body = f"""Hi {assignee},

This is a friendly reminder from MeetMind AI regarding an action item assigned to you during the {meeting}.

────────────────────────

ACTION ITEM

Task:
{task_clean}

Deadline:
{deadline}

Meeting:
{meeting}

────────────────────────

Please make sure the task is completed by the deadline.

If you have already completed this task, you can ignore this reminder.

Regards,
MeetMind AI
Automated Meeting Assistant"""
    return subject, body

def process_reminders():
    load_dotenv()
    db_path = "reminders.db"
    
    if not os.path.exists(db_path):
        print("No reminders.db found. Exiting.")
        return
        
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    c.execute('''
        SELECT reminder_id, assignee, email, task, deadline, meeting, status 
        FROM reminders 
        WHERE reminder_date = ? AND status IN ('PENDING', 'FAILED')
    ''', (today_str,))
    
    reminders = c.fetchall()
    if not reminders:
        print(f"No pending reminders for today ({today_str}).")
        conn.close()
        return
        
    print(f"Found {len(reminders)} reminder(s) to process for today.")
    
    service, error_msg = get_gmail_service()
    if not service:
        print(f"Failed to get Gmail service: {error_msg}")
        conn.close()
        return
        
    from googleapiclient.errors import HttpError
        
    for r in reminders:
        r_id, assignee, email, task, deadline, meeting, status = r
        
        subject, body = create_reminder_email_content(assignee, task, deadline, meeting)
        
        notifications_enabled = os.environ.get("EMAIL_NOTIFICATIONS_ENABLED", "false").lower() == "true"
        
        if not notifications_enabled:
            print(f"PREVIEW (Emails disabled) - Would send reminder {r_id} to {email}")
            print(f"Subject: {subject}\nBody:\n{body}\n")
            continue
            
        try:
            message = MIMEText(body)
            message["to"] = email
            message["subject"] = subject
            raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
            
            print(f"Sending reminder {r_id} to {email} for task: '{task[:30]}...'")
            sent_response = service.users().messages().send(
                userId="me",
                body={"raw": raw_message}
            ).execute()
            
            # Mark as SENT only after success
            now_str = datetime.datetime.now().isoformat()
            c.execute('''
                UPDATE reminders 
                SET status = 'SENT', sent_at = ? 
                WHERE reminder_id = ?
            ''', (now_str, r_id))
            print(f"Successfully sent reminder {r_id}")
            
        except HttpError as error:
            print(f"Gmail API error for reminder {r_id}: {error}")
            c.execute("UPDATE reminders SET status = 'FAILED' WHERE reminder_id = ?", (r_id,))
        except Exception as e:
            print(f"Failed to send reminder {r_id}: {e}")
            c.execute("UPDATE reminders SET status = 'FAILED' WHERE reminder_id = ?", (r_id,))
            
        conn.commit()
        
    conn.close()
    print("Done processing reminders.")

if __name__ == "__main__":
    process_reminders()
