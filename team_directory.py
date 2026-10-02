import os
import json
from typing import Dict, Any, Optional

DIRECTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "team_members.json")

def load_team_members(file_path: Optional[str] = None) -> Dict[str, Any]:
    """Load team members directory from JSON file."""
    path = file_path or DIRECTORY_FILE
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def get_assignee_email(name: Optional[str], file_path: Optional[str] = None) -> str:
    """
    Look up the registered email address for a given assignee name.
    
    Returns:
        The email address string if found, otherwise 'email not found'.
    """
    if not name or not isinstance(name, str):
        return "email not found"

    cleaned_name = name.strip()
    if not cleaned_name:
        return "email not found"

    members = load_team_members(file_path)
    if not members:
        return "email not found"

    # 1. Direct case-insensitive match
    for member_name, info in members.items():
        if member_name.lower() == cleaned_name.lower():
            return info.get("email", "email not found")

    # 2. Word-boundary / name token match (e.g., 'Speaker 2 (Kashif)' or 'Ali Mohammed')
    cleaned_lower = cleaned_name.lower()
    for member_name, info in members.items():
        m_lower = member_name.lower()
        # Check if the registered name appears as a discrete word/token in the input
        words = [w.strip("(),.-: ") for w in cleaned_lower.split()]
        if m_lower in words:
            return info.get("email", "email not found")

    return "email not found"
