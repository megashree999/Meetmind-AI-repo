import os
from dotenv import load_dotenv
load_dotenv(override=True)

from graph import graph

TEST_TRANSCRIPT = """Megashree: Review architecture by Friday.
Kashif: Fix authentication API by Monday.
Megashree: Prepare architecture document by Tuesday.
Priya: Test authentication API by Wednesday.
Megashree: Present architecture changes by Thursday."""

def main():
    print(">>> RUNNING ACTUAL EXTRACTION NODE...")
    result = graph.invoke({"transcript": TEST_TRANSCRIPT})

    analysis = result.get("analysis")
    action_items = getattr(analysis, "action_items", []) or (analysis.get("action_items") if isinstance(analysis, dict) else [])
    
    print(f"\n1. Extracted ActionItems count: {len(action_items)}\n")
    
    grouped = {}
    for idx, item in enumerate(action_items, 1):
        assignee = getattr(item, "assignee", "Unassigned") if hasattr(item, "assignee") else item.get("assignee")
        task = getattr(item, "task", "") if hasattr(item, "task") else item.get("task")
        print(f"{idx}. {assignee} - {task}")
        grouped[assignee] = grouped.get(assignee, []) + [task]
        
    print("\n2. Grouped action items by person:")
    for k, v in grouped.items():
        print(f"   {k}: {len(v)} tasks")
        
    print("\n>>> DOWNSTREAM EMAIL NOTIFICATIONS...")
    notifications = result.get("email_notifications", [])
    print(f"\n3. Final email notification count: {len(notifications)}\n")
    
    print("4. Recipients:")
    for notif in notifications:
        print(f"   - {notif.get('assignee')} ({notif.get('email')})")
        
    print("\n5. Confirmation (Body Preview):")
    for notif in notifications:
        print(f"\n--- Email to {notif.get('assignee')} ---")
        try:
            print(notif.get("body", "No body available"))
        except UnicodeEncodeError:
            print(notif.get("body", "").encode("ascii", "ignore").decode("ascii"))
        print("-" * 40)

if __name__ == "__main__":
    main()
