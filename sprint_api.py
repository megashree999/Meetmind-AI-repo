import os
import re
import logging
from typing import Optional, Dict, Any, List
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger("meetmind.sprint_api")

from sprint_database import (
    DEFAULT_DB,
    get_all_projects,
    get_meetings_for_project,
    get_latest_comparison_run,
    get_comparison_run_by_meetings,
    get_task_by_id,
    get_task_history,
    get_dashboard_summary_counts,
    get_project_tasks
)
from sprint_comparison_engine import compare_sprint_meetings
from jira_service import get_safe_jira_issue_url

def sanitize_identifier(value: Optional[str], max_len: int = 100) -> Optional[str]:
    """Validate and sanitize user-provided identifiers (alphanumeric, -, _, :, .)."""
    if not value or not isinstance(value, str):
        return None
    val = value.strip()
    if len(val) > max_len:
        return None
    if not re.match(r"^[A-Za-z0-9_\-\:\.]+$", val):
        return None
    return val

def enrich_task_with_jira_url(task: Dict[str, Any]) -> Dict[str, Any]:
    """Attach validated, domain-checked Jira URL to task dictionary."""
    t = dict(task)
    jira_key = t.get("jira_issue_key") or t.get("jira_key")
    t["jira_url"] = get_safe_jira_issue_url(jira_key)
    return t

def enrich_comparison_tasks(comp_data: Dict[str, Any]) -> Dict[str, Any]:
    """Enrich all task categories in a comparison data dictionary with safe Jira links."""
    enriched = dict(comp_data)
    for cat in [
        "newly_assigned_tasks",
        "completed_tasks",
        "pending_tasks",
        "in_progress_tasks",
        "changed_tasks",
        "needs_review_tasks"
    ]:
        if cat in enriched and isinstance(enriched[cat], list):
            enriched[cat] = [enrich_task_with_jira_url(item) for item in enriched[cat]]
    return enriched

def get_db_path(request: Request) -> str:
    """Retrieve injected or configured canonical sprint database path."""
    try:
        app = getattr(request, "app", None)
        app_db = getattr(app.state, "sprint_db_path", None) if app and hasattr(app, "state") else None
        if app_db:
            return os.path.abspath(app_db)
    except Exception:
        pass
    env_db = os.environ.get("SPRINT_COMPARISON_DB")
    if env_db:
        return os.path.abspath(env_db)
    return os.path.abspath(DEFAULT_DB)

def list_projects(request: Request) -> JSONResponse:
    """GET /api/projects - Distinct project keys."""
    try:
        db_path = get_db_path(request)
        projects = get_all_projects(db_path=db_path)
        if not projects:
            projects = ["SCRUM"]
        return JSONResponse({"status": "SUCCESS", "projects": projects})
    except Exception as e:
        logger.error(f"Error listing projects: {e}")
        return JSONResponse({"status": "ERROR", "message": "Failed to retrieve projects"}, status_code=500)

def list_meetings(request: Request) -> JSONResponse:
    """GET /api/meetings?project_key=SCRUM - Meeting history for project."""
    project_key = sanitize_identifier(request.query_params.get("project_key") or "SCRUM", max_len=30)
    if not project_key:
        return JSONResponse({"status": "ERROR", "message": "Invalid project_key"}, status_code=400)

    try:
        db_path = get_db_path(request)
        meetings = get_meetings_for_project(project_key=project_key, db_path=db_path)
        return JSONResponse({
            "status": "SUCCESS",
            "project_key": project_key.upper(),
            "count": len(meetings),
            "meetings": meetings
        })
    except Exception as e:
        logger.error(f"Error listing meetings for {project_key}: {e}")
        return JSONResponse({"status": "ERROR", "message": "Failed to retrieve meetings"}, status_code=500)

def get_latest_comparison(request: Request) -> JSONResponse:
    """
    GET /api/comparisons/latest?project_key=SCRUM
    Returns the latest persistent comparison run.
    Accurately reports FAILED comparisons and does not mask errors with stale successes.
    """
    project_key = sanitize_identifier(request.query_params.get("project_key") or "SCRUM", max_len=30)
    if not project_key:
        return JSONResponse({"status": "ERROR", "message": "Invalid project_key"}, status_code=400)

    clean_project = project_key.upper()
    db_path = get_db_path(request)

    try:
        run = get_latest_comparison_run(clean_project, db_path=db_path)
        if not run:
            # Check meeting count to determine if empty project
            meetings = get_meetings_for_project(clean_project, db_path=db_path)
            return JSONResponse({
                "status": "SUCCESS",
                "project_key": clean_project,
                "has_comparisons": False,
                "meeting_count": len(meetings),
                "message": "No comparison runs available yet for this project.",
                "comparison": None
            })

        # If latest comparison run failed, report failure honestly
        if run.get("status") == "FAILED":
            return JSONResponse({
                "status": "FAILED",
                "project_key": clean_project,
                "has_comparisons": True,
                "comparison_id": run.get("comparison_id"),
                "current_meeting_id": run.get("current_meeting_id"),
                "previous_meeting_id": run.get("previous_meeting_id"),
                "error": run.get("error_message") or "Sprint comparison failed.",
                "created_at": run.get("created_at"),
                "comparison": None
            })

        # Successful comparison: enrich tasks with safe Jira URLs
        comp_data = run.get("comparison_data") or {}
        comp_data = enrich_comparison_tasks(comp_data)

        return JSONResponse({
            "status": "SUCCESS",
            "project_key": clean_project,
            "has_comparisons": True,
            "comparison_id": run.get("comparison_id"),
            "current_meeting_id": run.get("current_meeting_id"),
            "previous_meeting_id": run.get("previous_meeting_id"),
            "created_at": run.get("created_at"),
            "summary_counts": run.get("summary_counts"),
            "comparison": comp_data
        })
    except Exception as e:
        logger.error(f"Error fetching latest comparison: {e}")
        return JSONResponse({"status": "ERROR", "message": "Failed to retrieve latest comparison"}, status_code=500)

def get_comparison_between_meetings(request: Request) -> JSONResponse:
    """
    GET /api/comparisons?project_key=SCRUM&current_meeting_id=m2&previous_meeting_id=m1
    Returns persistent comparison run between specific meetings.
    """
    project_key = sanitize_identifier(request.query_params.get("project_key") or "SCRUM", max_len=30)
    current_meeting_id = sanitize_identifier(request.query_params.get("current_meeting_id"), max_len=64)
    previous_meeting_id = sanitize_identifier(request.query_params.get("previous_meeting_id"), max_len=64)

    if not project_key or not current_meeting_id:
        return JSONResponse({
            "status": "ERROR",
            "message": "Both project_key and current_meeting_id are required parameters"
        }, status_code=400)

    clean_project = project_key.upper()
    db_path = get_db_path(request)

    try:
        run = get_comparison_run_by_meetings(
            project_key=clean_project,
            current_meeting_id=current_meeting_id,
            previous_meeting_id=previous_meeting_id,
            db_path=db_path
        )

        if run:
            if run.get("status") == "FAILED":
                return JSONResponse({
                    "status": "FAILED",
                    "project_key": clean_project,
                    "comparison_id": run.get("comparison_id"),
                    "error": run.get("error_message") or "Sprint comparison failed.",
                    "created_at": run.get("created_at")
                })
            comp_data = enrich_comparison_tasks(run.get("comparison_data") or {})
            return JSONResponse({
                "status": "SUCCESS",
                "project_key": clean_project,
                "comparison_id": run.get("comparison_id"),
                "current_meeting_id": current_meeting_id,
                "previous_meeting_id": previous_meeting_id,
                "created_at": run.get("created_at"),
                "summary_counts": run.get("summary_counts"),
                "comparison": comp_data
            })

        # If not persisted, run dynamic comparison and persist it
        comp_result = compare_sprint_meetings(
            project_key=clean_project,
            current_meeting_id=current_meeting_id,
            previous_meeting_id=previous_meeting_id,
            db_path=db_path
        )
        enriched = enrich_comparison_tasks(comp_result)
        return JSONResponse({
            "status": "SUCCESS",
            "project_key": clean_project,
            "comparison": enriched,
            "summary_counts": comp_result.get("summary_counts")
        })
    except Exception as e:
        logger.error(f"Error comparing meetings {current_meeting_id} vs {previous_meeting_id}: {e}")
        return JSONResponse({"status": "ERROR", "message": f"Failed to compute comparison: {e}"}, status_code=500)

def get_task_details(request: Request) -> JSONResponse:
    """GET /api/tasks/{task_id} - Details of a single task."""
    task_id = sanitize_identifier(request.path_params.get("task_id"), max_len=100)
    if not task_id:
        return JSONResponse({"status": "ERROR", "message": "Invalid task_id"}, status_code=400)

    try:
        db_path = get_db_path(request)
        task = get_task_by_id(task_id, db_path=db_path)
        if not task:
            return JSONResponse({"status": "ERROR", "message": f"Task '{task_id}' not found"}, status_code=404)

        enriched = enrich_task_with_jira_url(task)
        return JSONResponse({"status": "SUCCESS", "task": enriched})
    except Exception as e:
        logger.error(f"Error getting task {task_id}: {e}")
        return JSONResponse({"status": "ERROR", "message": "Failed to retrieve task"}, status_code=500)

def get_task_history_endpoint(request: Request) -> JSONResponse:
    """GET /api/tasks/{task_id}/history - Full chronological audit trail of a task."""
    task_id = sanitize_identifier(request.path_params.get("task_id"), max_len=100)
    if not task_id:
        return JSONResponse({"status": "ERROR", "message": "Invalid task_id"}, status_code=400)

    try:
        db_path = get_db_path(request)
        history = get_task_history(task_id, db_path=db_path)
        return JSONResponse({
            "status": "SUCCESS",
            "task_id": task_id,
            "count": len(history),
            "history": history
        })
    except Exception as e:
        logger.error(f"Error getting history for task {task_id}: {e}")
        return JSONResponse({"status": "ERROR", "message": "Failed to retrieve task history"}, status_code=500)

def get_dashboard_summary(request: Request) -> JSONResponse:
    """GET /api/summary?project_key=SCRUM - Overall dashboard counts."""
    project_key = sanitize_identifier(request.query_params.get("project_key") or "SCRUM", max_len=30)
    if not project_key:
        return JSONResponse({"status": "ERROR", "message": "Invalid project_key"}, status_code=400)

    clean_project = project_key.upper()
    try:
        db_path = get_db_path(request)
        summary = get_dashboard_summary_counts(clean_project, db_path=db_path)
        return JSONResponse({"status": "SUCCESS", "summary": summary})
    except Exception as e:
        logger.error(f"Error getting dashboard summary: {e}")
        return JSONResponse({"status": "ERROR", "message": "Failed to retrieve summary counts"}, status_code=500)

# Starlette Routes Definition
sprint_api_routes = [
    Route("/api/projects", list_projects, methods=["GET"]),
    Route("/api/meetings", list_meetings, methods=["GET"]),
    Route("/api/comparisons/latest", get_latest_comparison, methods=["GET"]),
    Route("/api/comparisons", get_comparison_between_meetings, methods=["GET"]),
    Route("/api/tasks/{task_id}", get_task_details, methods=["GET"]),
    Route("/api/tasks/{task_id}/history", get_task_history_endpoint, methods=["GET"]),
    Route("/api/summary", get_dashboard_summary, methods=["GET"]),
]
