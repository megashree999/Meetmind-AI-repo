import os
import re
import difflib
import logging
from typing import Optional, List, Dict, Any, Tuple, Set

logger = logging.getLogger("meetmind.sprint_engine")

from sprint_database import (
    DEFAULT_DB,
    init_sprint_db,
    get_sprint_connection,
    save_meeting,
    get_previous_meeting,
    save_or_update_task,
    get_project_tasks,
    get_task_history,
    normalize_task_name,
    ALLOWED_STATUSES,
    save_comparison_run
)
from jira_service import (
    get_jira_issues_for_project,
    map_jira_status_to_sprint_status
)

def analyze_task_statement(text: Optional[str]) -> Tuple[str, str]:
    """
    Linguistic analyzer to prevent negation and future-intention from triggering false completions,
    and flag tentative statements as NEEDS_REVIEW.

    Returns (classification, reason):
      - 'COMPLETED'
      - 'IN_PROGRESS'
      - 'NOT_COMPLETED' (e.g. negation 'not completed' or future intent 'will complete tomorrow')
      - 'NEEDS_REVIEW' (e.g. uncertain 'may be completed')
      - 'NONE'
    """
    if not text:
        return "NONE", "No text provided"
    t = text.lower().strip()

    verbs = r"(complete[ds]?|finish(?:ed|es|ing)?|resolve[ds]?|close[ds]?|fix(?:ed|es|ing)?|merge[ds]?|deploy(?:ed|ing)?|done)"

    # 1. Negation: Check if negation word precedes completion
    negation_pattern = rf"\b(not|never|hasn\'?t|haven\'?t|didn\'?t|isn\'?t|aren\'?t|wasn\'?t|weren\'?t|cannot|can\'?t|yet\s+to|incomplete|uncompleted|unresolved)\b.*?\b{verbs}\b"
    if re.search(negation_pattern, t):
        return "NOT_COMPLETED", f"Negation detected: '{text}'"

    # 2. Future intention / forward looking
    future_pattern = rf"\b(will|shall|plan(?:s|ned|ning)?\s+to|going\s+to|aim(?:s|ing)?\s+to|expect(?:s|ed|ing)?\s+to|tomorrow|next\s+week|next\s+sprint|by\s+[a-z]+|later)\b.*?\b{verbs}\b"
    future_pattern_alt = rf"\b{verbs}\b.*?\b(tomorrow|next\s+week|next\s+sprint|later)\b"
    if re.search(future_pattern, t) or re.search(future_pattern_alt, t):
        return "NOT_COMPLETED", f"Future intention detected: '{text}'"

    # 3. Tentative / uncertain
    uncertain_pattern = rf"\b(may|might|could\s+be|perhaps|possibly|probably|assumed|think\s+(?:it\s+is|it\'?s)|seems\s+(?:to\s+be|like)|unsure|not\s+sure)\b.*?\b{verbs}\b"
    if re.search(uncertain_pattern, t):
        return "NEEDS_REVIEW", f"Tentative or uncertain completion: '{text}'"

    # 4. In progress
    inprogress_pattern = r"\b(in\s+progress|still\s+in\s+progress|working\s+on|underway|investigating|active(?:ly)?\s+developing|wip)\b"
    if re.search(inprogress_pattern, t):
        return "IN_PROGRESS", f"In-progress reported: '{text}'"

    # 5. Explicit past-tense completion
    completion_pattern = r"\b(completed|finished|resolved|closed|merged|deployed|passed\s+qa|fixed\s+and\s+deployed|fixed|done)\b"
    if re.search(completion_pattern, t):
        return "COMPLETED", f"Explicit completion verified: '{text}'"

    return "NONE", "No clear signal"

def calculate_task_similarity(task_a: str, task_b: str) -> float:
    """Calculate normalized token-based sequence similarity between two task names."""
    norm_a = normalize_task_name(task_a)
    norm_b = normalize_task_name(task_b)
    if not norm_a or not norm_b:
        return 0.0
    if norm_a == norm_b:
        return 1.0

    # Sequence matcher ratio
    seq_ratio = difflib.SequenceMatcher(None, norm_a, norm_b).ratio()

    # Token overlap (Jaccard similarity and containment)
    tokens_a = set(norm_a.split())
    tokens_b = set(norm_b.split())
    if not tokens_a or not tokens_b:
        return seq_ratio
    jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
    common_tokens = tokens_a & tokens_b
    containment = len(common_tokens) / min(len(tokens_a), len(tokens_b))

    if len(common_tokens) >= 2 and containment >= 0.75:
        return max((seq_ratio * 0.5) + (jaccard * 0.5), containment * 0.85)

    return (seq_ratio * 0.6) + (jaccard * 0.4)

def find_candidate_matches(
    incoming_item: Dict[str, Any],
    existing_tasks: List[Dict[str, Any]],
    similarity_threshold: float = 0.75
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Conservative multi-tier task matching:
      1. Exact Jira issue key
      2. Exact Task ID
      3. High-confidence normalized task name match

    Returns (single_matched_task or None, ambiguous_candidates_list).
    """
    inc_jira = (incoming_item.get("jira_issue_key") or incoming_item.get("jira_key") or "").strip().upper()
    inc_task_id = (incoming_item.get("task_id") or "").strip()
    inc_name = incoming_item.get("task") or incoming_item.get("task_name") or ""
    norm_inc = normalize_task_name(inc_name)

    # 1. Exact Jira Issue Key Match
    if inc_jira:
        jira_matches = [t for t in existing_tasks if (t.get("jira_issue_key") or "").strip().upper() == inc_jira]
        if len(jira_matches) == 1:
            return jira_matches[0], []
        elif len(jira_matches) > 1:
            return None, jira_matches

    # 2. Exact Task ID Match
    if inc_task_id:
        id_matches = [t for t in existing_tasks if t.get("task_id") == inc_task_id]
        if len(id_matches) == 1:
            return id_matches[0], []
        elif len(id_matches) > 1:
            return None, id_matches

    # 3. Normalized Task Name Match
    exact_name_matches = [
        t for t in existing_tasks if t.get("normalized_task") == norm_inc and norm_inc != ""
    ]
    if len(exact_name_matches) == 1:
        return exact_name_matches[0], []
    elif len(exact_name_matches) > 1:
        return None, exact_name_matches

    # 4. Fuzzy High-Confidence Similarity Match
    candidates = []
    for t in existing_tasks:
        # If both tasks have explicit different Jira issue keys, they are definitely distinct tasks
        t_jira = (t.get("jira_issue_key") or "").strip().upper()
        if inc_jira and t_jira and inc_jira != t_jira:
            continue

        score = calculate_task_similarity(inc_name, t.get("task_name", ""))
        if score >= similarity_threshold:
            candidates.append((score, t))

    if len(candidates) == 1:
        # Check if assignees match or one was unassigned
        matched = candidates[0][1]
        inc_assignee = (incoming_item.get("assignee") or "").strip().lower()
        exist_assignee = (matched.get("assignee") or "").strip().lower()
        if not inc_assignee or not exist_assignee or inc_assignee == exist_assignee or candidates[0][0] >= 0.90:
            return matched, []
        else:
            # Different assignee with moderate similarity -> ambiguous
            return None, [matched]
    elif len(candidates) > 1:
        return None, [c[1] for c in candidates]

    return None, []

def compare_sprint_meetings(
    project_key: str,
    current_meeting_id: str,
    current_action_items: Optional[List[Any]] = None,
    current_meeting_title: Optional[str] = None,
    previous_meeting_id: Optional[str] = None,
    transcript: Optional[str] = None,
    jira_issues: Optional[List[Dict[str, Any]]] = None,
    sync_jira: bool = True,
    db_path: str = DEFAULT_DB
) -> Dict[str, Any]:
    """
    Compare two consecutive sprint meetings for a project and classify all tasks into:
      - NEWLY ASSIGNED
      - COMPLETED
      - PENDING
      - IN PROGRESS
      - CHANGED
      - NEEDS REVIEW

    Strict rules enforced:
      - Absence Guard: Omission from latest meeting NEVER marks a task completed.
      - Explicit Completion Evidence required (transcript, progress, or Jira Done).
      - Reopened Jira issues correctly transition from COMPLETED back to PENDING/IN_PROGRESS.
      - Idempotent and safe: duplicate processing does not duplicate tasks.
    """
    init_sprint_db(db_path)
    clean_project = project_key.strip().upper()

    # 1. Resolve Previous Meeting
    prev_meeting = None
    if previous_meeting_id:
        conn = get_sprint_connection(db_path)
        try:
            cursor = conn.execute(
                "SELECT * FROM meetings WHERE meeting_id = ? AND project_key = ?",
                (previous_meeting_id, clean_project)
            )
            row = cursor.fetchone()
            if row:
                prev_meeting = dict(row)
        finally:
            conn.close()

    if not prev_meeting:
        prev_meeting = get_previous_meeting(
            project_key=clean_project,
            current_meeting_id=current_meeting_id,
            db_path=db_path
        )

    resolved_prev_id = prev_meeting["meeting_id"] if prev_meeting else None

    # 2. Retrieve Existing Project Tasks from Database
    existing_tasks = get_project_tasks(project_key=clean_project, db_path=db_path)
    existing_by_id: Dict[str, Dict[str, Any]] = {t["task_id"]: dict(t) for t in existing_tasks}
    matched_existing_ids: Set[str] = set()

    # 3. Synchronize Jira State (if requested)
    jira_map: Dict[str, Dict[str, Any]] = {}
    if sync_jira:
        if jira_issues is None:
            try:
                fetch_res = get_jira_issues_for_project(clean_project)
                if fetch_res.get("status") == "SUCCESS":
                    jira_issues = fetch_res.get("issues", [])
                else:
                    jira_issues = []
            except Exception as e:
                logger.warning(f"Could not fetch Jira issues for {clean_project}: {e}")
                jira_issues = []

        for j in (jira_issues or []):
            if j.get("key"):
                jira_map[j["key"].strip().upper()] = j

    # Check for Jira status shifts on existing tasks (e.g. Jira marked Done, or Jira Reopened)
    jira_transitioned_completed: Set[str] = set()
    for t_id, task_data in list(existing_by_id.items()):
        j_key = (task_data.get("jira_issue_key") or "").strip().upper()
        if j_key and j_key in jira_map:
            j_item = jira_map[j_key]
            j_sprint_status = j_item.get("sprint_status") or map_jira_status_to_sprint_status(
                j_item.get("status_name"), j_item.get("status_category")
            )
            local_status = task_data.get("status")

            # Case A: Reopened in Jira
            if local_status == "COMPLETED" and j_sprint_status in ("PENDING", "IN_PROGRESS", "BLOCKED"):
                logger.info(f"[SPRINT ENGINE] Task '{j_key}' was COMPLETED locally but is now {j_sprint_status} in Jira. Updating.")
                update_res = save_or_update_task(
                    project_key=clean_project,
                    task_name=task_data["task_name"],
                    meeting_id=current_meeting_id,
                    assignee=j_item.get("assignee_name") or task_data["assignee"],
                    deadline=j_item.get("due_date") or task_data["deadline"],
                    jira_issue_key=j_key,
                    status=j_sprint_status,
                    completion_evidence=None, # clear completion evidence on reopen
                    task_id=t_id,
                    change_reason=f"Reopened in Jira ({j_key} transitioned to {j_item.get('status_name')})",
                    db_path=db_path
                )
                existing_by_id[t_id] = update_res["task"]

            # Case B: Marked Done in Jira
            elif local_status != "COMPLETED" and j_sprint_status == "COMPLETED":
                evidence = f"Verified completed in Jira ({j_key}, status: {j_item.get('status_name', 'Done')})"
                logger.info(f"[SPRINT ENGINE] Task '{j_key}' verified COMPLETED in Jira.")
                update_res = save_or_update_task(
                    project_key=clean_project,
                    task_name=task_data["task_name"],
                    meeting_id=current_meeting_id,
                    assignee=j_item.get("assignee_name") or task_data["assignee"],
                    deadline=j_item.get("due_date") or task_data["deadline"],
                    jira_issue_key=j_key,
                    status="COMPLETED",
                    completion_evidence=evidence,
                    task_id=t_id,
                    change_reason=f"Marked completed in Jira ({j_key})",
                    db_path=db_path
                )
                existing_by_id[t_id] = update_res["task"]
                jira_transitioned_completed.add(t_id)

    # 4. Standardize Incoming Action Items from Current Meeting
    raw_items = current_action_items or []
    normalized_incoming: List[Dict[str, Any]] = []
    for item in raw_items:
        if isinstance(item, dict):
            task_dict = dict(item)
        else:
            task_dict = {
                "task": getattr(item, "task", ""),
                "assignee": getattr(item, "assignee", None),
                "deadline": getattr(item, "deadline", None),
                "task_size": getattr(item, "task_size", None),
                "jira_issue_key": getattr(item, "jira_issue_key", None) or getattr(item, "jira_key", None),
                "task_id": getattr(item, "task_id", None),
                "status": getattr(item, "status", None),
                "progress": getattr(item, "progress", None),
                "context": getattr(item, "context", None),
                "completion_evidence": getattr(item, "completion_evidence", None)
            }
        normalized_incoming.append(task_dict)

    # Buckets for output
    newly_assigned: List[Dict[str, Any]] = []
    completed: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    in_progress: List[Dict[str, Any]] = []
    changed: List[Dict[str, Any]] = []
    needs_review: List[Dict[str, Any]] = []

    # 5. Match and Process Each Incoming Task
    for item in normalized_incoming:
        task_text = item.get("task") or item.get("task_name") or ""
        if not task_text.strip():
            continue

        matched_task, ambiguous_candidates = find_candidate_matches(
            item, list(existing_by_id.values())
        )

        if ambiguous_candidates:
            # Ambiguous match -> Flag as NEEDS REVIEW
            candidate_keys = [c.get("jira_issue_key") or c.get("task_id") for c in ambiguous_candidates]
            review_entry = {
                "task_name": task_text,
                "assignee": item.get("assignee"),
                "deadline": item.get("deadline"),
                "reason": f"Ambiguous matching candidates found: {', '.join(filter(None, candidate_keys))}",
                "candidates": ambiguous_candidates
            }
            needs_review.append(review_entry)
            continue

        # Extract explicit progress / completion signals from incoming item
        progress_text = item.get("progress") or ""
        item_status = (item.get("status") or "").strip().upper()

        signal_prog, reason_prog = analyze_task_statement(progress_text)
        signal_task, reason_task = analyze_task_statement(task_text)

        # Progress note has priority over task name if both present
        signal = signal_prog if signal_prog != "NONE" else signal_task
        signal_reason = reason_prog if signal_prog != "NONE" else reason_task

        if signal == "NEEDS_REVIEW":
            needs_review.append({
                "task_name": task_text,
                "assignee": item.get("assignee"),
                "deadline": item.get("deadline"),
                "reason": signal_reason,
                "candidates": [matched_task] if matched_task else []
            })
            if matched_task:
                matched_existing_ids.add(matched_task["task_id"])
            continue

        has_completion_signal = (
            item_status == "COMPLETED"
            or signal == "COMPLETED"
            or (item.get("completion_evidence") is not None)
        ) and (signal != "NOT_COMPLETED")

        has_inprogress_signal = (
            item_status == "IN_PROGRESS"
            or signal == "IN_PROGRESS"
        )

        if matched_task:
            t_id = matched_task["task_id"]
            matched_existing_ids.add(t_id)

            prev_assignee = matched_task.get("assignee")
            new_assignee = item.get("assignee") or prev_assignee
            prev_deadline = matched_task.get("deadline")
            new_deadline = item.get("deadline") or prev_deadline

            assignee_changed = (prev_assignee or "").strip().lower() != (new_assignee or "").strip().lower()
            deadline_changed = (prev_deadline or "").strip() != (new_deadline or "").strip()

            # Determine new status
            prev_status = matched_task.get("status", "PENDING")
            target_status = prev_status
            evidence = matched_task.get("completion_evidence")

            # Check Jira status for matched task
            j_key = (matched_task.get("jira_issue_key") or item.get("jira_issue_key") or "").strip().upper()
            if j_key and j_key in jira_map:
                j_status = jira_map[j_key].get("sprint_status")
                if j_status == "COMPLETED":
                    has_completion_signal = True
                    evidence = f"Verified completed in Jira ({j_key})"

            if signal == "NOT_COMPLETED":
                target_status = "PENDING"
                evidence = None
            elif has_completion_signal:
                target_status = "COMPLETED"
                evidence = evidence or item.get("completion_evidence") or f"Explicit completion reported in meeting {current_meeting_id}: '{progress_text or task_text}'"
            elif has_inprogress_signal:
                target_status = "IN_PROGRESS"

            # Check if assignee or deadline changed
            change_reasons = []
            if assignee_changed:
                change_reasons.append(f"Assignee changed from {prev_assignee} to {new_assignee}")
            if deadline_changed:
                change_reasons.append(f"Deadline changed from {prev_deadline} to {new_deadline}")

            # Save / update in sprint database
            save_res = save_or_update_task(
                project_key=clean_project,
                task_name=task_text or matched_task["task_name"],
                meeting_id=current_meeting_id,
                assignee=new_assignee,
                deadline=new_deadline,
                jira_issue_key=j_key or None,
                status=target_status,
                completion_evidence=evidence,
                task_id=t_id,
                change_reason="; ".join(change_reasons) if change_reasons else None,
                db_path=db_path
            )

            updated_task = save_res["task"]
            existing_by_id[t_id] = updated_task

            first_seen = matched_task.get("first_seen_meeting_id")

            # Classification
            if target_status == "COMPLETED":
                completed.append({
                    **updated_task,
                    "classification": "COMPLETED",
                    "reason": evidence or "Marked completed in current meeting"
                })
            elif assignee_changed or deadline_changed:
                changed.append({
                    **updated_task,
                    "classification": "CHANGED",
                    "reason": "; ".join(change_reasons),
                    "previous_assignee": prev_assignee,
                    "new_assignee": new_assignee,
                    "previous_deadline": prev_deadline,
                    "new_deadline": new_deadline
                })
            elif first_seen == current_meeting_id:
                newly_assigned.append({
                    **updated_task,
                    "classification": "NEWLY ASSIGNED",
                    "reason": f"First introduced in meeting {current_meeting_id}"
                })
            elif target_status == "IN_PROGRESS":
                in_progress.append({
                    **updated_task,
                    "classification": "IN_PROGRESS",
                    "reason": f"Reported in progress: '{progress_text or 'Under active development'}'"
                })
            else:
                pending.append({
                    **updated_task,
                    "classification": "PENDING",
                    "reason": "Discussed in current meeting; still pending"
                })

        else:
            # BRAND NEW TASK
            init_status = "PENDING"
            init_evidence = None

            j_key = (item.get("jira_issue_key") or item.get("jira_key") or "").strip().upper()
            if j_key and j_key in jira_map:
                j_status = jira_map[j_key].get("sprint_status")
                if j_status == "COMPLETED":
                    init_status = "COMPLETED"
                    init_evidence = f"Verified completed in Jira ({j_key})"

            if signal == "NOT_COMPLETED":
                init_status = "PENDING"
                init_evidence = None
            elif has_completion_signal:
                init_status = "COMPLETED"
                init_evidence = item.get("completion_evidence") or f"Created as completed: '{progress_text or task_text}'"
            elif has_inprogress_signal:
                init_status = "IN_PROGRESS"

            save_res = save_or_update_task(
                project_key=clean_project,
                task_name=task_text,
                meeting_id=current_meeting_id,
                assignee=item.get("assignee"),
                deadline=item.get("deadline"),
                task_size=item.get("task_size"),
                jira_issue_key=j_key or None,
                status=init_status,
                completion_evidence=init_evidence,
                task_id=item.get("task_id"),
                change_reason="Newly assigned in meeting",
                db_path=db_path
            )

            new_task = save_res["task"]
            t_id = new_task["task_id"]
            existing_by_id[t_id] = new_task
            matched_existing_ids.add(t_id)

            newly_assigned.append({
                **new_task,
                "classification": "NEWLY ASSIGNED",
                "reason": f"First introduced in meeting {current_meeting_id}"
            })

            # If created immediately as completed or in progress, also cross-reflect
            if init_status == "COMPLETED":
                completed.append({
                    **new_task,
                    "classification": "COMPLETED",
                    "reason": init_evidence
                })
            elif init_status == "IN_PROGRESS":
                in_progress.append({
                    **new_task,
                    "classification": "IN_PROGRESS",
                    "reason": "Introduced and reported in progress"
                })

    # 6. Absence Guard: Process Existing Tasks that were NOT Mentioned in Current Meeting
    # "A task being absent from the latest meeting does NOT mean it is completed."
    for t_id, task_data in existing_by_id.items():
        if t_id in matched_existing_ids:
            continue

        status = task_data.get("status")
        if t_id in jira_transitioned_completed:
            completed.append({
                **task_data,
                "classification": "COMPLETED",
                "reason": task_data.get("completion_evidence") or f"Verified completed in Jira ({task_data.get('jira_issue_key')})"
            })
        elif status == "COMPLETED":
            # Already completed previously; do not flag as newly completed in this comparison
            continue
        elif status == "IN_PROGRESS":
            in_progress.append({
                **task_data,
                "classification": "IN_PROGRESS",
                "reason": "Carried over in progress from previous meeting (not mentioned in latest meeting)"
            })
        else:
            # PENDING or BLOCKED
            pending.append({
                **task_data,
                "classification": "PENDING",
                "reason": "Carried over from prior meeting; absent from latest discussion but still incomplete"
            })

    # 7. Deduplicate list entries by task_id in each bucket
    def _dedup_list(lst: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = set()
        res = []
        for x in lst:
            t_key = x.get("task_id") or x.get("task_name")
            if t_key not in seen:
                seen.add(t_key)
                res.append(x)
        return res

    newly_assigned = _dedup_list(newly_assigned)
    completed = _dedup_list(completed)
    pending = _dedup_list(pending)
    in_progress = _dedup_list(in_progress)
    changed = _dedup_list(changed)
    needs_review = _dedup_list(needs_review)

    summary_counts = {
        "newly_assigned": len(newly_assigned),
        "completed": len(completed),
        "pending": len(pending),
        "in_progress": len(in_progress),
        "changed": len(changed),
        "needs_review": len(needs_review),
        "total_active_tasks": len(pending) + len(in_progress) + len(newly_assigned)
    }

    comparison_result = {
        "status": "SUCCESS",
        "project_key": clean_project,
        "previous_meeting_id": resolved_prev_id,
        "current_meeting_id": current_meeting_id,
        "current_meeting_title": current_meeting_title or f"Meeting {current_meeting_id}",
        "summary_counts": summary_counts,
        "newly_assigned_tasks": newly_assigned,
        "completed_tasks": completed,
        "pending_tasks": pending,
        "in_progress_tasks": in_progress,
        "changed_tasks": changed,
        "needs_review_tasks": needs_review
    }

    try:
        comparison_id = save_comparison_run(
            project_key=clean_project,
            current_meeting_id=current_meeting_id,
            previous_meeting_id=resolved_prev_id,
            status="SUCCESS",
            summary_counts=summary_counts,
            comparison_data=comparison_result,
            db_path=db_path
        )
        comparison_result["comparison_id"] = comparison_id
    except Exception as e:
        logger.warning(f"Could not persist comparison run: {e}")

    return comparison_result
