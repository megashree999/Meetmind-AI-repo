import sqlite3
import datetime
import os
import re
import uuid
import json
import logging
from typing import Optional, List, Dict, Any, Tuple

logger = logging.getLogger("meetmind.sprint_db")

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(PROJECT_ROOT, "sprint_comparison.db")

ALLOWED_STATUSES = {"PENDING", "IN_PROGRESS", "COMPLETED", "BLOCKED"}

def normalize_task_name(text: Optional[str]) -> str:
    """Normalize task text for robust, invariant comparison."""
    if not text:
        return ""
    # Lowercase, replace punctuation with spaces except alphanumeric, collapse whitespace
    cleaned = re.sub(r'[^a-zA-Z0-9\s]', ' ', str(text)).lower()
    return re.sub(r'\s+', ' ', cleaned).strip()

def get_sprint_connection(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    """
    Get a configured SQLite database connection.
    Enforces row_factory as sqlite3.Row for dictionary-like access.
    """
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # Enable WAL mode and foreign keys for durability and data integrity
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn

def init_sprint_db(db_path: str = DEFAULT_DB) -> None:
    """
    Initialize SQLite tables and indexes for Sprint Comparison.
    Operates strictly on sprint_comparison.db without touching reminders.db.
    """
    conn = get_sprint_connection(db_path)
    try:
        with conn:
            # 1. Meetings Table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS meetings (
                    meeting_id TEXT PRIMARY KEY,
                    project_key TEXT NOT NULL,
                    meeting_title TEXT NOT NULL,
                    sequence_number INTEGER NOT NULL,
                    transcript_hash TEXT NOT NULL,
                    summary TEXT,
                    created_at TEXT NOT NULL,
                    CONSTRAINT uq_project_transcript UNIQUE (project_key, transcript_hash),
                    CONSTRAINT uq_project_sequence UNIQUE (project_key, sequence_number)
                )
            ''')

            # 2. Project Tasks Table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS project_tasks (
                    task_id TEXT PRIMARY KEY,
                    project_key TEXT NOT NULL,
                    task_name TEXT NOT NULL,
                    normalized_task TEXT NOT NULL,
                    assignee TEXT,
                    assignee_email TEXT,
                    deadline TEXT,
                    task_size TEXT,
                    jira_issue_key TEXT,
                    status TEXT NOT NULL CHECK(status IN ('PENDING', 'IN_PROGRESS', 'COMPLETED', 'BLOCKED')),
                    completion_evidence TEXT,
                    first_seen_meeting_id TEXT NOT NULL,
                    last_seen_meeting_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            ''')

            # 3. Task History Table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS task_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    meeting_id TEXT NOT NULL,
                    previous_status TEXT,
                    new_status TEXT,
                    previous_assignee TEXT,
                    new_assignee TEXT,
                    previous_deadline TEXT,
                    new_deadline TEXT,
                    change_reason TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES project_tasks(task_id) ON DELETE CASCADE
                )
            ''')

            # 4. Jira Ticket Idempotency Cache Table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS created_jira_tickets (
                    ticket_key TEXT PRIMARY KEY,
                    project_key TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    normalized_summary TEXT NOT NULL,
                    meeting_id TEXT,
                    created_at TEXT NOT NULL
                )
            ''')

            # 5. Jira Creation Claims (Concurrency & Idempotency Locking)
            conn.execute('''
                CREATE TABLE IF NOT EXISTS jira_creation_claims (
                    idempotency_key TEXT PRIMARY KEY,
                    project_key TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    normalized_summary TEXT NOT NULL,
                    meeting_id TEXT,
                    task_id TEXT,
                    ticket_key TEXT,
                    status TEXT NOT NULL CHECK(status IN ('CLAIMED', 'CREATED', 'FAILED', 'UNCERTAIN_TIMEOUT')),
                    claimed_at TEXT NOT NULL,
                    completed_at TEXT,
                    last_error TEXT
                )
            ''')

            # 6. Comparison Runs (Persistent historical comparison runs)
            conn.execute('''
                CREATE TABLE IF NOT EXISTS comparison_runs (
                    comparison_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_key TEXT NOT NULL,
                    current_meeting_id TEXT NOT NULL,
                    previous_meeting_id TEXT,
                    status TEXT NOT NULL CHECK(status IN ('SUCCESS', 'FAILED')),
                    summary_counts TEXT NOT NULL,
                    comparison_data TEXT NOT NULL,
                    error_message TEXT,
                    created_at TEXT NOT NULL
                )
            ''')

            # 7. Comparison Run Tasks (Indexed snapshot of tasks per comparison run)
            conn.execute('''
                CREATE TABLE IF NOT EXISTS comparison_run_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    comparison_id INTEGER NOT NULL,
                    task_id TEXT,
                    task_name TEXT NOT NULL,
                    jira_issue_key TEXT,
                    classification TEXT NOT NULL,
                    previous_status TEXT,
                    current_status TEXT,
                    assignee TEXT,
                    deadline TEXT,
                    reason TEXT,
                    FOREIGN KEY (comparison_id) REFERENCES comparison_runs(comparison_id) ON DELETE CASCADE
                )
            ''')

            # 8. Created GitHub Issues (Meeting-scoped idempotency for GitHub issues)
            conn.execute('''
                CREATE TABLE IF NOT EXISTS created_github_issues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    meeting_id TEXT NOT NULL,
                    issue_number INTEGER,
                    title TEXT NOT NULL,
                    normalized_title TEXT NOT NULL,
                    issue_url TEXT,
                    created_at TEXT NOT NULL
                )
            ''')

            # Indexes for high performance
            conn.execute("CREATE INDEX IF NOT EXISTS idx_meetings_project_seq ON meetings(project_key, sequence_number)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_meetings_transcript_hash ON meetings(transcript_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON project_tasks(project_key, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_normalized ON project_tasks(project_key, normalized_task)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_jira_key ON project_tasks(jira_issue_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_history_task_id ON task_history(task_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_created_jira_project_norm ON created_jira_tickets(project_key, normalized_summary)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jira_claims_proj_norm ON jira_creation_claims(project_key, normalized_summary)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jira_claims_ticket ON jira_creation_claims(ticket_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_comparison_proj_created ON comparison_runs(project_key, created_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_comparison_meetings ON comparison_runs(project_key, current_meeting_id, previous_meeting_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_comparison_tasks_run ON comparison_run_tasks(comparison_id, classification)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_created_github_norm ON created_github_issues(meeting_id, normalized_title)")
    finally:
        conn.close()

def save_meeting(
    meeting_id: str,
    project_key: str,
    meeting_title: str,
    transcript_hash: str,
    summary: Optional[str] = None,
    sequence_number: Optional[int] = None,
    created_at: Optional[str] = None,
    db_path: str = DEFAULT_DB
) -> Dict[str, Any]:
    """
    Save a meeting record with sequence ordering and duplicate prevention.
    Returns status: 'SUCCESS' or 'DUPLICATE'.
    """
    init_sprint_db(db_path)
    clean_project = project_key.strip().upper()
    now_str = created_at or datetime.datetime.now(datetime.timezone.utc).isoformat()

    conn = get_sprint_connection(db_path)
    try:
        with conn:
            # Check for duplicate by meeting_id
            cursor = conn.execute("SELECT * FROM meetings WHERE meeting_id = ?", (meeting_id,))
            existing = cursor.fetchone()
            if existing:
                return {
                    "status": "DUPLICATE",
                    "reason": "MEETING_ID_EXISTS",
                    "meeting_id": meeting_id,
                    "meeting": dict(existing)
                }

            # Check for duplicate by transcript_hash within the same project
            cursor = conn.execute(
                "SELECT * FROM meetings WHERE project_key = ? AND transcript_hash = ?",
                (clean_project, transcript_hash)
            )
            existing_hash = cursor.fetchone()
            if existing_hash:
                return {
                    "status": "DUPLICATE",
                    "reason": "TRANSCRIPT_HASH_EXISTS",
                    "meeting_id": existing_hash["meeting_id"],
                    "meeting": dict(existing_hash)
                }

            # Calculate next sequence number for this project if not provided
            if sequence_number is None:
                cursor = conn.execute(
                    "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS next_seq FROM meetings WHERE project_key = ?",
                    (clean_project,)
                )
                seq = cursor.fetchone()["next_seq"]
            else:
                seq = int(sequence_number)

            conn.execute('''
                INSERT INTO meetings (
                    meeting_id, project_key, meeting_title, sequence_number, transcript_hash, summary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                meeting_id,
                clean_project,
                meeting_title,
                seq,
                transcript_hash,
                summary or "",
                now_str
            ))

            return {
                "status": "SUCCESS",
                "meeting_id": meeting_id,
                "project_key": clean_project,
                "meeting_title": meeting_title,
                "sequence_number": seq,
                "transcript_hash": transcript_hash,
                "summary": summary or "",
                "created_at": now_str
            }
    finally:
        conn.close()

def get_previous_meeting(
    project_key: str,
    current_sequence_number: Optional[int] = None,
    current_meeting_id: Optional[str] = None,
    db_path: str = DEFAULT_DB
) -> Optional[Dict[str, Any]]:
    """
    Retrieve the immediately preceding meeting for a given project.
    """
    init_sprint_db(db_path)
    clean_project = project_key.strip().upper()

    conn = get_sprint_connection(db_path)
    try:
        target_seq = current_sequence_number

        # If current_meeting_id is passed and sequence not provided, look up its sequence
        if target_seq is None and current_meeting_id:
            cursor = conn.execute(
                "SELECT sequence_number FROM meetings WHERE meeting_id = ? AND project_key = ?",
                (current_meeting_id, clean_project)
            )
            row = cursor.fetchone()
            if row:
                target_seq = row["sequence_number"]

        if target_seq is not None:
            # Find the highest sequence number strictly less than target_seq
            cursor = conn.execute('''
                SELECT * FROM meetings 
                WHERE project_key = ? AND sequence_number < ?
                ORDER BY sequence_number DESC 
                LIMIT 1
            ''', (clean_project, target_seq))
        else:
            # If no target specified, return the latest meeting
            cursor = conn.execute('''
                SELECT * FROM meetings 
                WHERE project_key = ?
                ORDER BY sequence_number DESC 
                LIMIT 1
            ''', (clean_project,))

        result = cursor.fetchone()
        return dict(result) if result else None
    finally:
        conn.close()

def save_or_update_task(
    project_key: str,
    task_name: str,
    meeting_id: str,
    assignee: Optional[str] = None,
    assignee_email: Optional[str] = None,
    deadline: Optional[str] = None,
    task_size: Optional[str] = None,
    jira_issue_key: Optional[str] = None,
    status: str = "PENDING",
    completion_evidence: Optional[str] = None,
    task_id: Optional[str] = None,
    change_reason: Optional[str] = None,
    db_path: str = DEFAULT_DB
) -> Dict[str, Any]:
    """
    Insert a new task or update an existing task while strictly recording task history.
    Allowed statuses: PENDING, IN_PROGRESS, COMPLETED, BLOCKED.
    """
    init_sprint_db(db_path)
    clean_project = project_key.strip().upper()
    clean_status = status.strip().upper()
    if clean_status not in ALLOWED_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of: {sorted(ALLOWED_STATUSES)}")

    clean_task_name = task_name.strip()
    norm_task = normalize_task_name(clean_task_name)
    clean_jira_key = jira_issue_key.strip().upper() if jira_issue_key else None
    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

    conn = get_sprint_connection(db_path)
    try:
        with conn:
            existing = None

            # 1. Match by explicit task_id if provided
            if task_id:
                cursor = conn.execute("SELECT * FROM project_tasks WHERE task_id = ?", (task_id,))
                existing = cursor.fetchone()

            # 2. Match by Jira issue key within project if available
            if not existing and clean_jira_key:
                cursor = conn.execute(
                    "SELECT * FROM project_tasks WHERE project_key = ? AND jira_issue_key = ?",
                    (clean_project, clean_jira_key)
                )
                existing = cursor.fetchone()

            # 3. Match by normalized_task within project
            if not existing and norm_task:
                cursor = conn.execute(
                    "SELECT * FROM project_tasks WHERE project_key = ? AND normalized_task = ?",
                    (clean_project, norm_task)
                )
                existing = cursor.fetchone()

            if existing:
                # Update existing task
                t_id = existing["task_id"]
                prev_status = existing["status"]
                prev_assignee = existing["assignee"]
                prev_deadline = existing["deadline"]

                # Check if changes occurred that warrant a history entry
                status_changed = prev_status != clean_status
                assignee_changed = (prev_assignee or "").strip().lower() != (assignee or "").strip().lower()
                deadline_changed = (prev_deadline or "").strip() != (deadline or "").strip()

                reasons = []
                if status_changed:
                    reasons.append(f"Status changed from {prev_status} to {clean_status}")
                if assignee_changed:
                    reasons.append(f"Assignee changed from {prev_assignee} to {assignee}")
                if deadline_changed:
                    reasons.append(f"Deadline changed from {prev_deadline} to {deadline}")

                final_jira_key = clean_jira_key or existing["jira_issue_key"]
                if clean_status != "COMPLETED":
                    final_completion_evidence = None
                else:
                    final_completion_evidence = completion_evidence or existing["completion_evidence"]

                conn.execute('''
                    UPDATE project_tasks
                    SET task_name = ?,
                        normalized_task = ?,
                        assignee = ?,
                        assignee_email = ?,
                        deadline = ?,
                        task_size = ?,
                        jira_issue_key = ?,
                        status = ?,
                        completion_evidence = ?,
                        last_seen_meeting_id = ?,
                        updated_at = ?
                    WHERE task_id = ?
                ''', (
                    clean_task_name,
                    norm_task,
                    assignee,
                    assignee_email,
                    deadline,
                    task_size,
                    final_jira_key,
                    clean_status,
                    final_completion_evidence,
                    meeting_id,
                    now_str,
                    t_id
                ))

                # Log history record if any significant attributes changed
                if status_changed or assignee_changed or deadline_changed:
                    log_reason = change_reason or "; ".join(reasons)
                    conn.execute('''
                        INSERT INTO task_history (
                            task_id, meeting_id, previous_status, new_status,
                            previous_assignee, new_assignee, previous_deadline, new_deadline,
                            change_reason, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        t_id,
                        meeting_id,
                        prev_status,
                        clean_status,
                        prev_assignee,
                        assignee,
                        prev_deadline,
                        deadline,
                        log_reason,
                        now_str
                    ))

                # Fetch updated record
                cursor = conn.execute("SELECT * FROM project_tasks WHERE task_id = ?", (t_id,))
                updated_row = dict(cursor.fetchone())
                action_type = "UPDATED" if reasons else "UNCHANGED"
                return {
                    "action": action_type,
                    "task_id": t_id,
                    "task": updated_row,
                    "changes": reasons
                }
            else:
                # Insert new task
                t_id = task_id or (clean_jira_key if clean_jira_key else f"task_{uuid.uuid4().hex[:12]}")
                conn.execute('''
                    INSERT INTO project_tasks (
                        task_id, project_key, task_name, normalized_task, assignee, assignee_email,
                        deadline, task_size, jira_issue_key, status, completion_evidence,
                        first_seen_meeting_id, last_seen_meeting_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    t_id,
                    clean_project,
                    clean_task_name,
                    norm_task,
                    assignee,
                    assignee_email,
                    deadline,
                    task_size,
                    clean_jira_key,
                    clean_status,
                    completion_evidence,
                    meeting_id,
                    meeting_id,
                    now_str,
                    now_str
                ))

                # Initial history record
                conn.execute('''
                    INSERT INTO task_history (
                        task_id, meeting_id, previous_status, new_status,
                        previous_assignee, new_assignee, previous_deadline, new_deadline,
                        change_reason, created_at
                    ) VALUES (?, ?, NULL, ?, NULL, ?, NULL, ?, ?, ?)
                ''', (
                    t_id,
                    meeting_id,
                    clean_status,
                    assignee,
                    deadline,
                    change_reason or "Initial task assignment",
                    now_str
                ))

                cursor = conn.execute("SELECT * FROM project_tasks WHERE task_id = ?", (t_id,))
                new_row = dict(cursor.fetchone())
                return {
                    "action": "CREATED",
                    "task_id": t_id,
                    "task": new_row,
                    "changes": []
                }
    finally:
        conn.close()

def get_project_tasks(
    project_key: str,
    status: Optional[str] = None,
    meeting_id: Optional[str] = None,
    db_path: str = DEFAULT_DB
) -> List[Dict[str, Any]]:
    """
    Retrieve project tasks with optional filtering by status or last_seen_meeting_id.
    """
    init_sprint_db(db_path)
    clean_project = project_key.strip().upper()

    conn = get_sprint_connection(db_path)
    try:
        query = "SELECT * FROM project_tasks WHERE project_key = ?"
        params: List[Any] = [clean_project]

        if status:
            query += " AND status = ?"
            params.append(status.strip().upper())

        if meeting_id:
            query += " AND last_seen_meeting_id = ?"
            params.append(meeting_id)

        query += " ORDER BY created_at ASC"

        cursor = conn.execute(query, params)
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()

def get_task_history(
    task_id: str,
    db_path: str = DEFAULT_DB
) -> List[Dict[str, Any]]:
    """
    Retrieve chronological audit trail of all changes for a specific task.
    """
    init_sprint_db(db_path)
    conn = get_sprint_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT * FROM task_history WHERE task_id = ? ORDER BY history_id ASC",
            (task_id,)
        )
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()

def generate_jira_idempotency_key(
    project_key: str,
    summary: str,
    meeting_id: Optional[str] = None,
    task_id: Optional[str] = None,
    assignee: Optional[str] = None
) -> str:
    """Generate a deterministic, unique idempotency key for Jira creation."""
    clean_project = project_key.strip().upper()
    norm_summary = normalize_task_name(summary)
    
    # Priority 1: Exact stable task ID
    if task_id and task_id.strip():
        return f"{clean_project}:task:{task_id.strip()}"
        
    # Priority 2: Meeting and action identity
    clean_assignee = (assignee or "").strip().lower()
    if meeting_id and meeting_id.strip():
        if clean_assignee:
            return f"{clean_project}:m:{meeting_id.strip()}:{clean_assignee}:{norm_summary}"
        return f"{clean_project}:m:{meeting_id.strip()}:{norm_summary}"
        
    # Priority 3: Fallback query identity
    if clean_assignee:
        return f"{clean_project}:q:{clean_assignee}:{norm_summary}"
    return f"{clean_project}:q:{norm_summary}"

def claim_jira_creation(
    idempotency_key: str,
    project_key: str,
    summary: str,
    meeting_id: Optional[str] = None,
    task_id: Optional[str] = None,
    claim_timeout_seconds: int = 120,
    db_path: str = DEFAULT_DB
) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Atomically claim the right to create a Jira issue.
    Returns (status, ticket_key, message):
      - ('CLAIMED', None, None): Caller has acquired claim and may call Jira.
      - ('ALREADY_CREATED', ticket_key, None): Ticket already created; reuse ticket_key.
      - ('IN_PROGRESS', None, message): Another process is currently executing the creation.
      - ('UNCERTAIN_TIMEOUT', None, message): Previous attempt timed out after sending request; verify Jira before creating.
    """
    init_sprint_db(db_path)
    clean_project = project_key.strip().upper()
    norm_summary = normalize_task_name(summary)
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    now_str = now_dt.isoformat()

    conn = get_sprint_connection(db_path)
    try:
        with conn:
            cursor = conn.execute(
                "SELECT * FROM jira_creation_claims WHERE idempotency_key = ?",
                (idempotency_key,)
            )
            claim = cursor.fetchone()

            if claim:
                status = claim["status"]
                ticket_key = claim["ticket_key"]

                if status == "CREATED" and ticket_key:
                    return "ALREADY_CREATED", ticket_key, None

                if status == "UNCERTAIN_TIMEOUT":
                    return "UNCERTAIN_TIMEOUT", None, (
                        f"Prior attempt timed out during network request ({claim['last_error']}). "
                        "Outcome with Jira is uncertain; verify Jira issues before creating another ticket."
                    )

                if status == "CLAIMED":
                    claimed_at_str = claim["claimed_at"]
                    try:
                        claimed_dt = datetime.datetime.fromisoformat(claimed_at_str)
                        elapsed = (now_dt - claimed_dt).total_seconds()
                    except Exception:
                        elapsed = claim_timeout_seconds + 1

                    if elapsed < claim_timeout_seconds:
                        return "IN_PROGRESS", None, f"Jira creation in progress by another worker (claimed {int(elapsed)}s ago)."
                    else:
                        conn.execute('''
                            UPDATE jira_creation_claims
                            SET claimed_at = ?, last_error = 'Stale claim reclaimed'
                            WHERE idempotency_key = ?
                        ''', (now_str, idempotency_key))
                        return "CLAIMED", None, None

                if status == "FAILED":
                    conn.execute('''
                        UPDATE jira_creation_claims
                        SET status = 'CLAIMED', claimed_at = ?, last_error = NULL
                        WHERE idempotency_key = ?
                    ''', (now_str, idempotency_key))
                    return "CLAIMED", None, None

            # Insert atomic claim
            conn.execute('''
                INSERT INTO jira_creation_claims (
                    idempotency_key, project_key, summary, normalized_summary,
                    meeting_id, task_id, status, claimed_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'CLAIMED', ?)
            ''', (
                idempotency_key, clean_project, summary.strip(), norm_summary,
                meeting_id, task_id, now_str
            ))
            return "CLAIMED", None, None
    finally:
        conn.close()

def resolve_jira_claim(
    idempotency_key: str,
    status: str,
    ticket_key: Optional[str] = None,
    error_message: Optional[str] = None,
    db_path: str = DEFAULT_DB
) -> None:
    """
    Resolve a Jira creation claim to 'CREATED', 'FAILED', or 'UNCERTAIN_TIMEOUT'.
    Does not hold a database transaction open during Jira network operations.
    """
    init_sprint_db(db_path)
    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
    clean_status = status.strip().upper()

    conn = get_sprint_connection(db_path)
    try:
        with conn:
            conn.execute('''
                UPDATE jira_creation_claims
                SET status = ?,
                    ticket_key = COALESCE(?, ticket_key),
                    completed_at = ?,
                    last_error = ?
                WHERE idempotency_key = ?
            ''', (clean_status, ticket_key, now_str, error_message, idempotency_key))

            if clean_status == "CREATED" and ticket_key:
                cursor = conn.execute(
                    "SELECT project_key, summary, meeting_id FROM jira_creation_claims WHERE idempotency_key = ?",
                    (idempotency_key,)
                )
                row = cursor.fetchone()
                if row:
                    norm_summary = normalize_task_name(row["summary"])
                    conn.execute('''
                        INSERT OR REPLACE INTO created_jira_tickets (
                            ticket_key, project_key, summary, normalized_summary, meeting_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                    ''', (ticket_key.strip().upper(), row["project_key"], row["summary"], norm_summary, row["meeting_id"], now_str))
    finally:
        conn.close()

def record_created_jira_ticket(
    ticket_key: str,
    project_key: str,
    summary: str,
    meeting_id: Optional[str] = None,
    db_path: str = DEFAULT_DB
) -> None:
    """Record a created Jira ticket in the idempotency cache table."""
    init_sprint_db(db_path)
    clean_project = project_key.strip().upper()
    norm_summary = normalize_task_name(summary)
    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

    conn = get_sprint_connection(db_path)
    try:
        with conn:
            conn.execute('''
                INSERT OR REPLACE INTO created_jira_tickets (
                    ticket_key, project_key, summary, normalized_summary, meeting_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            ''', (ticket_key.strip().upper(), clean_project, summary.strip(), norm_summary, meeting_id, now_str))
    finally:
        conn.close()

def get_cached_jira_ticket(
    project_key: str,
    summary: str,
    meeting_id: Optional[str] = None,
    task_id: Optional[str] = None,
    assignee: Optional[str] = None,
    db_path: str = DEFAULT_DB
) -> Optional[str]:
    """
    Retrieve an existing Jira issue key enforcing strict 4-tier matching priority:
      1. Exact stable task ID or idempotency key
      2. Exact meeting & action identity
      3. Cautious summary fallback requiring validation (rejecting completed tasks or conflicting assignees)
      4. Idempotency cache table
    """
    init_sprint_db(db_path)
    clean_project = project_key.strip().upper()
    norm_summary = normalize_task_name(summary)
    clean_summary_lower = summary.strip().lower()
    clean_assignee = (assignee or "").strip().lower()

    conn = get_sprint_connection(db_path)
    try:
        # Priority 1: Exact stable task ID
        if task_id:
            cursor = conn.execute(
                "SELECT ticket_key FROM jira_creation_claims WHERE project_key = ? AND task_id = ? AND status = 'CREATED' LIMIT 1",
                (clean_project, task_id)
            )
            row = cursor.fetchone()
            if row and row["ticket_key"]:
                return row["ticket_key"]

            cursor = conn.execute(
                "SELECT jira_issue_key FROM project_tasks WHERE project_key = ? AND task_id = ? AND jira_issue_key IS NOT NULL AND jira_issue_key != '' LIMIT 1",
                (clean_project, task_id)
            )
            row = cursor.fetchone()
            if row and row["jira_issue_key"]:
                return row["jira_issue_key"]

        # Priority 2: Exact meeting & action identity
        if meeting_id:
            cursor = conn.execute(
                "SELECT ticket_key FROM jira_creation_claims WHERE project_key = ? AND meeting_id = ? AND normalized_summary = ? AND status = 'CREATED' LIMIT 1",
                (clean_project, meeting_id, norm_summary)
            )
            row = cursor.fetchone()
            if row and row["ticket_key"]:
                return row["ticket_key"]

        # Priority 3: Cautious summary match on project_tasks
        cursor = conn.execute(
            "SELECT * FROM project_tasks WHERE project_key = ? AND (normalized_task = ? OR LOWER(task_name) = ?) AND jira_issue_key IS NOT NULL AND jira_issue_key != ''",
            (clean_project, norm_summary, clean_summary_lower)
        )
        matching_tasks = cursor.fetchall()
        if len(matching_tasks) == 1:
            mt = matching_tasks[0]
            existing_assignee = (mt["assignee"] or "").strip().lower()
            existing_status = mt["status"]

            # Guard against false reuse:
            # - If existing task was already COMPLETED, do not silently reuse for a new task
            # - If assignees conflict (e.g. Alice vs Bob), do not silently reuse
            assignee_matches = not clean_assignee or not existing_assignee or clean_assignee == existing_assignee
            if assignee_matches and existing_status != "COMPLETED":
                return mt["jira_issue_key"]
            else:
                print(f"[JIRA CACHE] Ambiguous reuse flagged for '{summary}' (status={existing_status}, assignee='{existing_assignee}' vs '{clean_assignee}')")
                return None
        elif len(matching_tasks) > 1:
            print(f"[JIRA CACHE] Multiple matching tasks found for '{summary}' in project {clean_project}. Ambiguous reuse flagged.")
            return None

        # Priority 4: Fallback to created_jira_tickets cache
        cursor = conn.execute(
            "SELECT ticket_key, meeting_id FROM created_jira_tickets WHERE project_key = ? AND (normalized_summary = ? OR LOWER(summary) = ?) LIMIT 1",
            (clean_project, norm_summary, clean_summary_lower)
        )
        row = cursor.fetchone()
        if row and row["ticket_key"]:
            # If meeting_id is present on both, only reuse if they belong to the same meeting!
            if meeting_id and row["meeting_id"] and meeting_id.strip() != row["meeting_id"].strip():
                # Different meetings with identical summaries and no task link -> do NOT blindly reuse!
                return None
            if not meeting_id and row["meeting_id"]:
                # Meeting task should not be reused for a standalone non-meeting query
                return None
            return row["ticket_key"]

        return None
    finally:
        conn.close()

def save_comparison_run(
    project_key: str,
    current_meeting_id: str,
    previous_meeting_id: Optional[str],
    status: str,
    summary_counts: Dict[str, Any],
    comparison_data: Dict[str, Any],
    error_message: Optional[str] = None,
    db_path: str = DEFAULT_DB
) -> int:
    """
    Persist a comparison run snapshot.
    Stores the full structured result so historical comparisons remain 100% accurate
    even as tasks evolve across subsequent meetings.
    """
    init_sprint_db(db_path)
    clean_project = project_key.strip().upper()
    clean_status = status.strip().upper()
    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

    conn = get_sprint_connection(db_path)
    try:
        with conn:
            cursor = conn.execute('''
                INSERT INTO comparison_runs (
                    project_key, current_meeting_id, previous_meeting_id,
                    status, summary_counts, comparison_data, error_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                clean_project,
                current_meeting_id,
                previous_meeting_id,
                clean_status,
                json.dumps(summary_counts or {}),
                json.dumps(comparison_data or {}),
                error_message,
                now_str
            ))
            comparison_id = cursor.lastrowid

            # Save indexed task records if successful
            if clean_status == "SUCCESS" and comparison_data:
                category_map = [
                    ("NEWLY_ASSIGNED", comparison_data.get("newly_assigned_tasks", [])),
                    ("COMPLETED", comparison_data.get("completed_tasks", [])),
                    ("PENDING", comparison_data.get("pending_tasks", [])),
                    ("IN_PROGRESS", comparison_data.get("in_progress_tasks", [])),
                    ("CHANGED", comparison_data.get("changed_tasks", [])),
                    ("NEEDS_REVIEW", comparison_data.get("needs_review_tasks", []))
                ]
                for classification, task_list in category_map:
                    for t in (task_list or []):
                        t_id = t.get("task_id")
                        t_name = t.get("task_name") or t.get("task") or "Untitled Task"
                        j_key = t.get("jira_issue_key")
                        prev_st = t.get("previous_status")
                        curr_st = t.get("current_status") or t.get("status")
                        assignee = t.get("assignee")
                        deadline = t.get("deadline")
                        reason = t.get("reason") or t.get("change_reason") or t.get("completion_evidence")

                        conn.execute('''
                            INSERT INTO comparison_run_tasks (
                                comparison_id, task_id, task_name, jira_issue_key,
                                classification, previous_status, current_status,
                                assignee, deadline, reason
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            comparison_id, t_id, t_name, j_key,
                            classification, prev_st, curr_st,
                            assignee, deadline, reason
                        ))
            return comparison_id
    finally:
        conn.close()

def get_latest_comparison_run(
    project_key: str,
    db_path: str = DEFAULT_DB
) -> Optional[Dict[str, Any]]:
    """Retrieve the latest comparison run for a project, including its status and full snapshot."""
    init_sprint_db(db_path)
    clean_project = project_key.strip().upper()
    conn = get_sprint_connection(db_path)
    try:
        cursor = conn.execute('''
            SELECT * FROM comparison_runs
            WHERE project_key = ?
            ORDER BY comparison_id DESC
            LIMIT 1
        ''', (clean_project,))
        row = cursor.fetchone()
        if not row:
            return None
        res = dict(row)
        res["summary_counts"] = json.loads(res["summary_counts"]) if res["summary_counts"] else {}
        res["comparison_data"] = json.loads(res["comparison_data"]) if res["comparison_data"] else {}
        return res
    finally:
        conn.close()

def get_comparison_run_by_meetings(
    project_key: str,
    current_meeting_id: str,
    previous_meeting_id: Optional[str] = None,
    db_path: str = DEFAULT_DB
) -> Optional[Dict[str, Any]]:
    """Retrieve a specific historical comparison run between two meetings."""
    init_sprint_db(db_path)
    clean_project = project_key.strip().upper()
    conn = get_sprint_connection(db_path)
    try:
        if previous_meeting_id:
            cursor = conn.execute('''
                SELECT * FROM comparison_runs
                WHERE project_key = ? AND current_meeting_id = ? AND previous_meeting_id = ?
                ORDER BY comparison_id DESC
                LIMIT 1
            ''', (clean_project, current_meeting_id, previous_meeting_id))
        else:
            cursor = conn.execute('''
                SELECT * FROM comparison_runs
                WHERE project_key = ? AND current_meeting_id = ?
                ORDER BY comparison_id DESC
                LIMIT 1
            ''', (clean_project, current_meeting_id))
        row = cursor.fetchone()
        if not row:
            return None
        res = dict(row)
        res["summary_counts"] = json.loads(res["summary_counts"]) if res["summary_counts"] else {}
        res["comparison_data"] = json.loads(res["comparison_data"]) if res["comparison_data"] else {}
        return res
    finally:
        conn.close()

def get_all_projects(db_path: str = DEFAULT_DB) -> List[str]:
    """Retrieve a distinct list of project keys across meetings and tasks."""
    init_sprint_db(db_path)
    conn = get_sprint_connection(db_path)
    try:
        cursor = conn.execute('''
            SELECT DISTINCT project_key FROM meetings
            UNION
            SELECT DISTINCT project_key FROM project_tasks
            ORDER BY project_key ASC
        ''')
        return [row[0] for row in cursor.fetchall() if row[0]]
    finally:
        conn.close()

def get_meetings_for_project(
    project_key: str,
    db_path: str = DEFAULT_DB
) -> List[Dict[str, Any]]:
    """Retrieve all meetings for a given project ordered by sequence number."""
    init_sprint_db(db_path)
    clean_project = project_key.strip().upper()
    conn = get_sprint_connection(db_path)
    try:
        cursor = conn.execute('''
            SELECT meeting_id, project_key, meeting_title, sequence_number, summary, created_at
            FROM meetings
            WHERE project_key = ?
            ORDER BY sequence_number ASC, created_at ASC
        ''', (clean_project,))
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()

def get_task_by_id(
    task_id: str,
    db_path: str = DEFAULT_DB
) -> Optional[Dict[str, Any]]:
    """Retrieve full task details for a single task."""
    init_sprint_db(db_path)
    conn = get_sprint_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT * FROM project_tasks WHERE task_id = ?",
            (task_id.strip(),)
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def get_cached_github_issue(
    meeting_id: Optional[str],
    title: str,
    db_path: str = DEFAULT_DB
) -> Optional[Dict[str, Any]]:
    """Retrieve existing created GitHub issue for meeting and title to prevent duplicates."""
    init_sprint_db(db_path)
    clean_meeting = (meeting_id or "").strip()
    norm_title = normalize_task_name(title)
    if not norm_title:
        return None

    conn = get_sprint_connection(db_path)
    try:
        cursor = conn.execute('''
            SELECT issue_number, title, issue_url
            FROM created_github_issues
            WHERE (meeting_id = ? OR meeting_id = '') AND normalized_title = ?
            ORDER BY id DESC LIMIT 1
        ''', (clean_meeting, norm_title))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def record_created_github_issue(
    meeting_id: Optional[str],
    issue_number: Optional[int],
    title: str,
    issue_url: Optional[str],
    db_path: str = DEFAULT_DB
) -> None:
    """Record successfully created or simulated GitHub issue for meeting idempotency."""
    init_sprint_db(db_path)
    clean_meeting = (meeting_id or "").strip()
    norm_title = normalize_task_name(title)
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    conn = get_sprint_connection(db_path)
    try:
        with conn:
            conn.execute('''
                INSERT INTO created_github_issues
                (meeting_id, issue_number, title, normalized_title, issue_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (clean_meeting, issue_number, title.strip(), norm_title, issue_url, now_iso))
    finally:
        conn.close()

def get_dashboard_summary_counts(
    project_key: str,
    db_path: str = DEFAULT_DB
) -> Dict[str, Any]:
    """Compute current dashboard summary counts directly from project_tasks and latest comparison."""
    init_sprint_db(db_path)
    clean_project = project_key.strip().upper()
    conn = get_sprint_connection(db_path)
    try:
        cursor = conn.execute('''
            SELECT status, count(*) as cnt
            FROM project_tasks
            WHERE project_key = ?
            GROUP BY status
        ''', (clean_project,))
        status_counts = {r["status"]: r["cnt"] for r in cursor.fetchall()}

        latest_run = get_latest_comparison_run(clean_project, db_path=db_path)
        comp_summary = latest_run.get("summary_counts", {}) if latest_run and latest_run.get("status") == "SUCCESS" else {}

        active = status_counts.get("PENDING", 0) + status_counts.get("IN_PROGRESS", 0)

        # Return both short keys (used by OverviewCards) and long keys (used by API serializers)
        return {
            "project_key": clean_project,
            "total_active_tasks": active,
            "active": active,
            "completed_tasks": status_counts.get("COMPLETED", 0),
            "completed": status_counts.get("COMPLETED", 0),
            "pending_tasks": status_counts.get("PENDING", 0),
            "pending": status_counts.get("PENDING", 0),
            "in_progress_tasks": status_counts.get("IN_PROGRESS", 0),
            "in_progress": status_counts.get("IN_PROGRESS", 0),
            "blocked_tasks": status_counts.get("BLOCKED", 0),
            "blocked": status_counts.get("BLOCKED", 0),
            "newly_assigned_tasks": comp_summary.get("newly_assigned", 0),
            "newly_assigned": comp_summary.get("newly_assigned", 0),
            "changed_tasks": comp_summary.get("changed", 0),
            "changed": comp_summary.get("changed", 0),
            "needs_review_tasks": comp_summary.get("needs_review", 0),
            "needs_review": comp_summary.get("needs_review", 0),
            "latest_comparison_status": latest_run.get("status") if latest_run else None,
            "latest_comparison_error": latest_run.get("error_message") if latest_run else None,
        }
    finally:
        conn.close()

