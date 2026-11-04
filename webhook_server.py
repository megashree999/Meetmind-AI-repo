import os
import sys
import json
import logging
from typing import Optional, Any
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, FileResponse
from starlette.routing import Route, Mount
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles
from dotenv import load_dotenv

# Ensure stdout handles UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=False)
DEFAULT_DB = os.path.join(PROJECT_ROOT, "reminders.db")
DEFAULT_SPRINT_DB = os.path.join(PROJECT_ROOT, "sprint_comparison.db")
FRONTEND_DIST = os.path.join(PROJECT_ROOT, "frontend", "dist")
MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024  # 2MB max payload

logger = logging.getLogger("meetmind.webhook_server")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)]
    )

from jira_webhook import verify_jira_webhook_auth, process_jira_webhook_event
from calendar_webhook import process_calendar_push_notification
from google_calendar_service import get_calendar_service
from sprint_api import sprint_api_routes

def create_app(
    db_path: str = DEFAULT_DB,
    calendar_service: Any = None,
    sprint_db_path: str = DEFAULT_SPRINT_DB
) -> Starlette:
    """
    Factory creating the unified Starlette webhook and Sprint Comparison API application.
    Supports dependency injection of custom db_path, sprint_db_path and calendar_service.
    """

    async def health_check(request: Request) -> Response:
        """GET /health - Verify server and SQLite database readiness."""
        import sqlite3
        db_status = "UNKNOWN"
        try:
            conn = sqlite3.connect(db_path, timeout=5.0)
            c = conn.cursor()
            c.execute("SELECT 1")
            conn.close()
            db_status = "HEALTHY"
        except Exception as e:
            db_status = f"UNHEALTHY: {e}"

        return JSONResponse({
            "service": "MeetMind Webhook Service",
            "status": "UP",
            "database": db_status
        }, status_code=200 if db_status == "HEALTHY" else 503)

    async def handle_jira_webhook(request: Request) -> Response:
        """POST /webhooks/jira - Receive and process Jira Cloud webhook events."""
        # Check Content-Length if present
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_REQUEST_BODY_BYTES:
            logger.warning("[JIRA WEBHOOK] Request body exceeded size limit (413).")
            return JSONResponse({"error": "Payload Too Large: max 2MB"}, status_code=413)

        raw_body = await request.body()
        if len(raw_body) > MAX_REQUEST_BODY_BYTES:
            logger.warning("[JIRA WEBHOOK] Request body exceeded size limit (413).")
            return JSONResponse({"error": "Payload Too Large: max 2MB"}, status_code=413)

        # 1. Authenticate Jira Webhook using HMAC-SHA256 signature
        signature_header = request.headers.get("x-hub-signature") or request.headers.get("x-hub-signature-256")
        auth_header = request.headers.get("authorization")
        custom_header = request.headers.get("x-atlassian-webhook-secret")
        secret_param = request.query_params.get("secret")

        if not verify_jira_webhook_auth(
            raw_body=raw_body,
            signature_header=signature_header,
            auth_header=auth_header,
            custom_header=custom_header,
            secret_param=secret_param
        ):
            logger.warning("[JIRA WEBHOOK] Authentication failed: invalid signature or missing secret.")
            return JSONResponse({"error": "Unauthorized: invalid webhook signature"}, status_code=401)

        # 2. Parse JSON payload
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception as json_err:
            logger.warning(f"[JIRA WEBHOOK] Malformed JSON payload: {json_err}")
            return JSONResponse({"error": "Malformed JSON payload"}, status_code=400)

        # Resolve calendar service if needed for deadline changes
        cal_svc = calendar_service
        if cal_svc is None:
            try:
                cal_svc, _ = get_calendar_service()
            except Exception:
                cal_svc = None

        # 3. Process event
        code, resp_data = process_jira_webhook_event(
            payload=payload,
            raw_body=raw_body,
            db_path=db_path,
            calendar_service=cal_svc,
            headers=dict(request.headers)
        )
        return JSONResponse(resp_data, status_code=code)

    async def handle_calendar_webhook(request: Request) -> Response:
        """POST /webhooks/google-calendar - Receive and process Google Calendar push notifications."""
        # Convert request headers to standard dict
        headers = dict(request.headers)

        # Resolve calendar service instance
        cal_svc = calendar_service
        if cal_svc is None:
            try:
                cal_svc, err = get_calendar_service()
                if not cal_svc:
                    logger.error(f"[CALENDAR WEBHOOK] Failed to initialize Calendar service: {err}")
            except Exception as e:
                logger.error(f"[CALENDAR WEBHOOK] Calendar service exception: {e}")
                cal_svc = None

        code, resp_data = process_calendar_push_notification(
            headers=headers,
            db_path=db_path,
            calendar_service=cal_svc
        )
        return JSONResponse(resp_data, status_code=code)

    def api_404_handler(request: Request) -> JSONResponse:
        """Explicitly trap any unmatched /api routes to prevent SPA fallback interception."""
        return JSONResponse({
            "status": "ERROR",
            "message": f"API endpoint not found: {request.url.path}"
        }, status_code=404)

    def spa_fallback(request: Request) -> Response:
        """Serve index.html for client-side React routing, ensuring API/webhook routes are excluded."""
        path = request.url.path
        if path.startswith("/api/") or path.startswith("/webhooks/") or path == "/health":
            return JSONResponse({"status": "ERROR", "message": "Not found"}, status_code=404)

        index_file = os.path.join(FRONTEND_DIST, "index.html")
        if os.path.exists(index_file):
            return FileResponse(index_file)

        return JSONResponse({
            "service": "MeetMind Webhook & Sprint Dashboard API",
            "status": "API_READY",
            "message": "Frontend build not yet generated. API endpoints active under /api."
        })

    # Order is critical:
    # 1. Health & Webhooks
    # 2. REST API endpoints
    # 3. Explicit API 404 guard
    # 4. Frontend static assets & SPA fallback
    routes = [
        Route("/health", health_check, methods=["GET"]),
        Route("/webhooks/jira", handle_jira_webhook, methods=["POST"]),
        Route("/webhooks/google-calendar", handle_calendar_webhook, methods=["POST"]),
    ]

    # Register Sprint API routes
    routes.extend(sprint_api_routes)

    # Register API 404 guard so broken /api requests never hit SPA fallback
    routes.append(Route("/api/{rest_of_path:path}", api_404_handler, methods=["GET", "POST", "PUT", "DELETE"]))

    # Frontend static files and SPA route
    assets_dir = os.path.join(FRONTEND_DIST, "assets")
    if os.path.isdir(assets_dir):
        routes.append(Mount("/assets", StaticFiles(directory=assets_dir), name="assets"))

    # SPA catch-all at root
    routes.append(Route("/{path:path}", spa_fallback, methods=["GET"]))

    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    ]

    app = Starlette(debug=False, routes=routes, middleware=middleware)
    app.state.db_path = os.path.abspath(db_path)
    app.state.sprint_db_path = os.path.abspath(sprint_db_path)
    logger.info(f"[DATABASE] Reminders DB resolved to canonical path: {app.state.db_path}")
    logger.info(f"[DATABASE] Sprint DB resolved to canonical path: {app.state.sprint_db_path}")
    return app

# Global app instance for uvicorn
app = create_app()

if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("WEBHOOK_HOST", "127.0.0.1")
    port = int(os.environ.get("WEBHOOK_PORT", "8000"))
    logger.info(f"Starting MeetMind Webhook Service on {host}:{port}")
    uvicorn.run(app, host=host, port=port)

