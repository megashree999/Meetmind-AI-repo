import os
import unittest
from unittest.mock import patch, MagicMock
import requests

from jira_service import (
    create_jira_issue_api,
    verify_jira_connection,
    get_jira_credentials,
    get_jira_headers,
    extract_jira_error
)
from github_service import (
    create_github_issue_api,
    verify_github_connection,
    get_github_credentials,
    get_github_headers,
    extract_github_error
)
from graph import graph, create_jira_ticket, create_github_issue, AgentState, JiraTask, GitHubIssue, RouterOutput

class TestJiraIntegration(unittest.TestCase):
    def setUp(self):
        # Set dummy env vars for testing (no real credentials)
        self.env_patcher = patch.dict(os.environ, {
            "JIRA_BASE_URL": "https://testdomain.atlassian.net",
            "JIRA_EMAIL": "testuser@example.com",
            "JIRA_API_TOKEN": "dummy_test_token_12345",
            "JIRA_PROJECT_KEY": "SCRUM",
            "JIRA_TIMEOUT": "2",
            "JIRA_MAX_RETRIES": "1",
            "JIRA_BACKOFF_FACTOR": "0.01"
        })
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    @patch("requests.post")
    def test_jira_ticket_success_201(self, mock_post):
        """Test successful Jira issue creation with HTTP 201."""
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"key": "SCRUM-101", "id": "10001"}
        mock_post.return_value = mock_resp

        result = create_jira_issue_api(
            title="Implement OAuth authentication",
            description="Details of authentication task"
        )

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["mode"], "real")
        self.assertEqual(result["http_status"], 201)
        self.assertEqual(result["issue_key"], "SCRUM-101")
        self.assertIn("https://testdomain.atlassian.net/browse/SCRUM-101", result["issue_url"])
        self.assertIn("REAL JIRA ISSUE", result["formatted_text"])
        self.assertIn("Created successfully", result["formatted_text"])
        self.assertEqual(mock_post.call_count, 1)

    @patch("requests.post")
    def test_jira_ticket_invalid_credentials_401(self, mock_post):
        """Test Jira authentication failure (HTTP 401 Unauthorized) - must NOT retry."""
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = '{"errorMessages":["Unauthorized"]}'
        mock_resp.json.return_value = {"errorMessages": ["Unauthorized"]}
        mock_post.return_value = mock_resp

        result = create_jira_issue_api(
            title="Failed Auth Task",
            description="Description"
        )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["mode"], "real")
        self.assertEqual(result["http_status"], 401)
        self.assertIn("401", result["error"])
        self.assertIn("FAILED JIRA ISSUE (API ERROR)", result["formatted_text"])
        self.assertIn("Status: FAILED", result["formatted_text"])
        # Permanent 401 error should not be retried
        self.assertEqual(mock_post.call_count, 1)

    @patch("requests.post")
    def test_jira_ticket_forbidden_403(self, mock_post):
        """Test Jira permission denied (HTTP 403 Forbidden) - must NOT retry."""
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = '{"errorMessages":["You do not have permission to create issues in this project"]}'
        mock_resp.json.return_value = {"errorMessages": ["You do not have permission to create issues in this project"]}
        mock_post.return_value = mock_resp

        result = create_jira_issue_api(
            title="Forbidden Task",
            description="Description"
        )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["http_status"], 403)
        self.assertIn("403", result["error"])
        self.assertIn("Permission denied", result["error"])
        self.assertEqual(mock_post.call_count, 1)

    @patch("requests.post")
    def test_jira_ticket_not_found_404(self, mock_post):
        """Test Jira project not found (HTTP 404 Not Found) - must NOT retry."""
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = '{"errorMessages":["Project SCRUM not found"]}'
        mock_resp.json.return_value = {"errorMessages": ["Project SCRUM not found"]}
        mock_post.return_value = mock_resp

        result = create_jira_issue_api(
            title="Missing Project Task",
            description="Description"
        )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["http_status"], 404)
        self.assertIn("404", result["error"])
        self.assertEqual(mock_post.call_count, 1)

    @patch("requests.post")
    def test_jira_ticket_read_timeout_with_retries(self, mock_post):
        """Test Jira request timeout - retries up to max_retries then fails gracefully."""
        mock_post.side_effect = requests.exceptions.Timeout("Read timed out")

        result = create_jira_issue_api(
            title="Timed Out Task",
            description="Description"
        )

        self.assertEqual(result["status"], "FAILED")
        self.assertIsNone(result["http_status"])
        self.assertIn("timed out", result["error"].lower())
        self.assertIn("FAILED JIRA ISSUE (API ERROR)", result["formatted_text"])
        # Initial attempt + 1 retry = 2 calls
        self.assertEqual(mock_post.call_count, 2)

    @patch("requests.post")
    def test_jira_ticket_connection_error_with_retries(self, mock_post):
        """Test Jira network connection failure - retries then fails gracefully."""
        mock_post.side_effect = requests.exceptions.ConnectionError("Connection refused")

        result = create_jira_issue_api(
            title="Connection Error Task",
            description="Description"
        )

        self.assertEqual(result["status"], "FAILED")
        self.assertIsNone(result["http_status"])
        self.assertIn("connection error", result["error"].lower())
        self.assertEqual(mock_post.call_count, 2)

    @patch("requests.post")
    def test_jira_transient_retry_success(self, mock_post):
        """Test transient 500 error followed by 201 success on retry."""
        resp_500 = MagicMock()
        resp_500.status_code = 500
        resp_500.text = "Internal Server Error"
        resp_500.json.return_value = {"message": "Internal Server Error"}

        resp_201 = MagicMock()
        resp_201.status_code = 201
        resp_201.json.return_value = {"key": "SCRUM-102"}

        mock_post.side_effect = [resp_500, resp_201]

        result = create_jira_issue_api(
            title="Transient Retry Task",
            description="Description"
        )

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["issue_key"], "SCRUM-102")
        self.assertEqual(mock_post.call_count, 2)

    def test_jira_simulated_mode_when_credentials_missing(self):
        """Test simulated fallback mode when credentials are missing."""
        with patch.dict(os.environ, {"JIRA_BASE_URL": "", "JIRA_EMAIL": "", "JIRA_API_TOKEN": ""}):
            result = create_jira_issue_api(
                title="Simulated Task",
                description="Description"
            )
            self.assertEqual(result["status"], "SIMULATED")
            self.assertEqual(result["mode"], "mock")
            self.assertIn("SIMULATED JIRA ISSUE", result["formatted_text"])
            self.assertIn("Status: SIMULATED", result["formatted_text"])

    @patch("requests.get")
    def test_verify_jira_connection_success(self, mock_get):
        """Test verify_jira_connection when auth and project access succeed."""
        myself_resp = MagicMock()
        myself_resp.status_code = 200
        myself_resp.json.return_value = {"displayName": "Test Admin"}

        project_resp = MagicMock()
        project_resp.status_code = 200
        project_resp.json.return_value = {"name": "Scrum Project", "id": "10000"}

        mock_get.side_effect = [myself_resp, project_resp]

        result = verify_jira_connection()
        self.assertEqual(result["auth"], "SUCCESS")
        self.assertEqual(result["project_access"], "SUCCESS")
        self.assertEqual(result["display_name"], "Test Admin")
        self.assertEqual(result["project_name"], "Scrum Project")
        self.assertIsNone(result["error"])


class TestGitHubIntegration(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {
            "GITHUB_TOKEN": "ghp_dummy_token_for_testing_1234567890",
            "GITHUB_OWNER": "testowner",
            "GITHUB_REPO": "testrepo",
            "GITHUB_TIMEOUT": "2",
            "GITHUB_MAX_RETRIES": "1",
            "GITHUB_BACKOFF_FACTOR": "0.01"
        })
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    @patch("requests.post")
    def test_github_issue_success_201(self, mock_post):
        """Test successful GitHub issue creation with HTTP 201."""
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {
            "number": 42,
            "html_url": "https://github.com/testowner/testrepo/issues/42"
        }
        mock_post.return_value = mock_resp

        result = create_github_issue_api(
            title="Bug: login timeout on staging",
            body="Reproduction steps..."
        )

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["mode"], "real")
        self.assertEqual(result["http_status"], 201)
        self.assertEqual(result["issue_number"], 42)
        self.assertEqual(result["issue_url"], "https://github.com/testowner/testrepo/issues/42")
        self.assertIn("REAL GITHUB ISSUE", result["formatted_text"])
        self.assertIn("Issue Number: #42", result["formatted_text"])
        self.assertIn("Created successfully", result["formatted_text"])
        self.assertEqual(mock_post.call_count, 1)

    @patch("requests.post")
    def test_github_issue_bad_credentials_401(self, mock_post):
        """Test GitHub 401 Unauthorized (Bad credentials) - must NOT retry."""
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = '{"message":"Bad credentials","documentation_url":"https://docs.github.com/rest"}'
        mock_resp.json.return_value = {"message": "Bad credentials"}
        mock_post.return_value = mock_resp

        result = create_github_issue_api(
            title="Unauthorized issue",
            body="Body"
        )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["mode"], "real")
        self.assertEqual(result["http_status"], 401)
        self.assertIn("401", result["error"])
        self.assertIn("Bad credentials", result["error"])
        self.assertIn("FAILED GITHUB ISSUE (API ERROR)", result["formatted_text"])
        self.assertIn("Status: FAILED", result["formatted_text"])
        # Permanent 401 error should not be retried
        self.assertEqual(mock_post.call_count, 1)

    @patch("requests.post")
    def test_github_issue_forbidden_403(self, mock_post):
        """Test GitHub 403 Forbidden (insufficient permissions) - must NOT retry."""
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = '{"message":"Resource not accessible by personal access token"}'
        mock_resp.json.return_value = {"message": "Resource not accessible by personal access token"}
        mock_post.return_value = mock_resp

        result = create_github_issue_api(
            title="Forbidden issue",
            body="Body"
        )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["http_status"], 403)
        self.assertIn("403", result["error"])
        self.assertIn("Forbidden", result["error"])
        self.assertEqual(mock_post.call_count, 1)

    @patch("requests.post")
    def test_github_issue_not_found_404(self, mock_post):
        """Test GitHub 404 Not Found (repo not found) - must NOT retry."""
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = '{"message":"Not Found"}'
        mock_resp.json.return_value = {"message": "Not Found"}
        mock_post.return_value = mock_resp

        result = create_github_issue_api(
            title="Not Found issue",
            body="Body"
        )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["http_status"], 404)
        self.assertIn("404", result["error"])
        self.assertEqual(mock_post.call_count, 1)

    @patch("requests.post")
    def test_github_issue_validation_failed_422(self, mock_post):
        """Test GitHub 422 Unprocessable Entity - must NOT retry."""
        mock_resp = MagicMock()
        mock_resp.status_code = 422
        mock_resp.text = '{"message":"Validation Failed","errors":[{"field":"title","code":"missing_field"}]}'
        mock_resp.json.return_value = {
            "message": "Validation Failed",
            "errors": [{"field": "title", "code": "missing_field"}]
        }
        mock_post.return_value = mock_resp

        result = create_github_issue_api(
            title="",
            body="Body without title"
        )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["http_status"], 422)
        self.assertIn("422", result["error"])
        self.assertEqual(mock_post.call_count, 1)

    @patch("requests.post")
    def test_github_issue_read_timeout_with_retries(self, mock_post):
        """Test GitHub timeout - retries up to max_retries then fails gracefully."""
        mock_post.side_effect = requests.exceptions.Timeout("HTTPSConnectionPool Read timed out")

        result = create_github_issue_api(
            title="Timeout issue",
            body="Body"
        )

        self.assertEqual(result["status"], "FAILED")
        self.assertIsNone(result["http_status"])
        self.assertIn("timed out", result["error"].lower())
        self.assertIn("FAILED GITHUB ISSUE (API ERROR)", result["formatted_text"])
        self.assertEqual(mock_post.call_count, 2)

    @patch("requests.post")
    def test_github_issue_connection_error_with_retries(self, mock_post):
        """Test GitHub network failure - retries then fails gracefully."""
        mock_post.side_effect = requests.exceptions.ConnectionError("Failed to establish a new connection")

        result = create_github_issue_api(
            title="Connection error issue",
            body="Body"
        )

        self.assertEqual(result["status"], "FAILED")
        self.assertIsNone(result["http_status"])
        self.assertIn("connection error", result["error"].lower())
        self.assertEqual(mock_post.call_count, 2)

    @patch("requests.post")
    def test_github_transient_retry_success(self, mock_post):
        """Test transient 429 rate limit error followed by 201 success on retry."""
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.text = "Too Many Requests"
        resp_429.json.return_value = {"message": "Too Many Requests"}

        resp_201 = MagicMock()
        resp_201.status_code = 201
        resp_201.json.return_value = {"number": 99, "html_url": "https://github.com/testowner/testrepo/issues/99"}

        mock_post.side_effect = [resp_429, resp_201]

        result = create_github_issue_api(
            title="Rate limited then succeeded",
            body="Body"
        )

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["issue_number"], 99)
        self.assertEqual(mock_post.call_count, 2)

    def test_github_simulated_mode_when_credentials_missing(self):
        """Test simulated fallback mode when GitHub credentials are not configured."""
        with patch.dict(os.environ, {"GITHUB_TOKEN": "", "GITHUB_OWNER": "", "GITHUB_REPO": ""}):
            result = create_github_issue_api(
                title="Simulated Issue",
                body="Body"
            )
            self.assertEqual(result["status"], "SIMULATED")
            self.assertEqual(result["mode"], "mock")
            self.assertIn("SIMULATED GITHUB ISSUE", result["formatted_text"])
            self.assertIn("Status: SIMULATED", result["formatted_text"])

    @patch("requests.get")
    def test_verify_github_connection_success(self, mock_get):
        """Test verify_github_connection when auth and repo checks succeed."""
        user_resp = MagicMock()
        user_resp.status_code = 200
        user_resp.json.return_value = {"login": "testowner"}

        repo_resp = MagicMock()
        repo_resp.status_code = 200
        repo_resp.json.return_value = {
            "full_name": "testowner/testrepo",
            "private": False,
            "has_issues": True
        }

        mock_get.side_effect = [user_resp, repo_resp]

        result = verify_github_connection()
        self.assertEqual(result["auth_status"], "SUCCESS")
        self.assertEqual(result["repo_status"], "SUCCESS")
        self.assertEqual(result["authenticated_user"], "testowner")
        self.assertIsNone(result["error"])


class TestLangGraphNodeSafety(unittest.TestCase):
    """Verify that the LangGraph nodes continue safely when integrations fail or succeed."""

    @patch("graph.create_jira_issue_api")
    def test_create_jira_ticket_node_handles_failure_safely(self, mock_create):
        """When Jira ticket creation fails (e.g. 401 or timeout), node records failure without crashing."""
        mock_create.return_value = {
            "mode": "real",
            "status": "FAILED",
            "http_status": 401,
            "error": "HTTP 401 Unauthorized: Jira authentication failed.",
            "title": "Fix login bug",
            "formatted_text": "FAILED JIRA ISSUE (API ERROR)\nTitle: Fix login bug\nHTTP Status: 401\nStatus: FAILED"
        }

        state: AgentState = {
            "meeting_id": "test_meeting",
            "transcript": "Test transcript",
            "analysis": MagicMock(jira_tasks=[JiraTask(title="Fix login bug", description="Fix it")]),
            "user_query": None,
            "retrieved_context": None,
            "answer": None,
            "ticket_result": [],
            "email_notifications": [],
            "router_output": None,
            "determined_actions": ["create_jira_ticket"]
        }

        output = create_jira_ticket(state)
        self.assertIn("ticket_result", output)
        self.assertEqual(len(output["ticket_result"]), 1)
        self.assertIn("FAILED JIRA ISSUE", output["ticket_result"][0])
        self.assertIn("Status: FAILED", output["ticket_result"][0])

    @patch("graph.create_github_issue_api")
    def test_create_github_issue_node_handles_failure_safely(self, mock_create):
        """When GitHub issue creation fails (e.g. 401 Bad credentials), node records failure without crashing."""
        mock_create.return_value = {
            "mode": "real",
            "status": "FAILED",
            "http_status": 401,
            "error": "HTTP 401 Unauthorized: Bad credentials.",
            "title": "Review auth code",
            "formatted_text": "FAILED GITHUB ISSUE (API ERROR)\nTitle: Review auth code\nHTTP Status: 401\nStatus: FAILED"
        }

        state: AgentState = {
            "meeting_id": "test_meeting",
            "transcript": "Test transcript",
            "analysis": MagicMock(github_issues=[GitHubIssue(title="Review auth code", description="Review it")]),
            "user_query": None,
            "retrieved_context": None,
            "answer": None,
            "ticket_result": [],
            "email_notifications": [],
            "router_output": None,
            "determined_actions": ["create_github_issue"]
        }

        output = create_github_issue(state)
        self.assertIn("ticket_result", output)
        self.assertEqual(len(output["ticket_result"]), 1)
        self.assertIn("FAILED GITHUB ISSUE", output["ticket_result"][0])
        self.assertIn("Status: FAILED", output["ticket_result"][0])

    @patch("graph.create_jira_issue_api")
    def test_create_jira_ticket_node_handles_exception_safely(self, mock_create):
        """When an unhandled exception is raised, create_jira_ticket catches it and formats failure."""
        mock_create.side_effect = RuntimeError("Catastrophic connection error")

        state: AgentState = {
            "meeting_id": "test_meeting",
            "transcript": "Test transcript",
            "analysis": MagicMock(jira_tasks=[JiraTask(title="Crashed task", description="desc")]),
            "user_query": None,
            "retrieved_context": None,
            "answer": None,
            "ticket_result": [],
            "email_notifications": [],
            "router_output": None,
            "determined_actions": ["create_jira_ticket"]
        }

        output = create_jira_ticket(state)
        self.assertIn("ticket_result", output)
        self.assertEqual(len(output["ticket_result"]), 1)
        self.assertIn("FAILED JIRA ISSUE", output["ticket_result"][0])
        self.assertIn("Catastrophic connection error", output["ticket_result"][0])

    @patch("graph.create_github_issue_api")
    def test_create_github_issue_node_handles_exception_safely(self, mock_create):
        """When an unhandled exception is raised, create_github_issue catches it and formats failure."""
        mock_create.side_effect = RuntimeError("Catastrophic GitHub connection error")

        state: AgentState = {
            "meeting_id": "test_meeting",
            "transcript": "Test transcript",
            "analysis": MagicMock(github_issues=[GitHubIssue(title="Crashed issue", description="desc")]),
            "user_query": None,
            "retrieved_context": None,
            "answer": None,
            "ticket_result": [],
            "email_notifications": [],
            "router_output": None,
            "determined_actions": ["create_github_issue"]
        }

        output = create_github_issue(state)
        self.assertIn("ticket_result", output)
        self.assertEqual(len(output["ticket_result"]), 1)
        self.assertIn("FAILED GITHUB ISSUE", output["ticket_result"][0])
        self.assertIn("Catastrophic GitHub connection error", output["ticket_result"][0])


class TestGraphWorkflowMocked(unittest.TestCase):
    """Test full LangGraph routing pipeline with mocked LLM and mocked APIs."""

    def setUp(self):
        import tempfile
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_sprint_db = os.path.join(self.temp_dir.name, "test_sprint.db")
        self.env_patcher = patch.dict(os.environ, {
            "SPRINT_COMPARISON_DB": self.test_sprint_db,
            "JIRA_BASE_URL": "https://testdomain.atlassian.net",
            "JIRA_EMAIL": "testuser@example.com",
            "JIRA_API_TOKEN": "dummy_jira_token",
            "JIRA_PROJECT_KEY": "SCRUM",
            "GITHUB_TOKEN": "ghp_dummy_token",
            "GITHUB_OWNER": "testowner",
            "GITHUB_REPO": "testrepo",
            "JIRA_TIMEOUT": "2",
            "GITHUB_TIMEOUT": "2",
            "JIRA_MAX_RETRIES": "1",
            "GITHUB_MAX_RETRIES": "1",
            "JIRA_BACKOFF_FACTOR": "0.01",
            "GITHUB_BACKOFF_FACTOR": "0.01",
        })
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()
        if hasattr(self, "temp_dir"):
            self.temp_dir.cleanup()

    @patch("graph.get_llm")
    @patch("jira_service.requests.post")
    def test_router_create_jira_ticket_success(self, mock_jira_post, mock_get_llm):
        """Test user query routing to Jira ticket creation with successful mock response."""
        mock_llm = MagicMock()
        mock_structured = MagicMock()
        mock_structured.invoke.return_value = RouterOutput(
            route="CREATE_TICKET_JIRA",
            ticket_title="MeetMind Integration Test",
            ticket_description="Integration test ticket description"
        )
        mock_llm.with_structured_output.return_value = mock_structured
        mock_get_llm.return_value = mock_llm

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"key": "SCRUM-555"}
        mock_jira_post.return_value = mock_resp

        inputs = {
            "user_query": "Create a Jira ticket for MeetMind Integration. Description: Integration test ticket.",
            "analysis": None,
            "transcript": None,
            "ticket_result": [],
            "email_notifications": []
        }

        output = graph.invoke(inputs)
        results = output.get("ticket_result", [])
        self.assertTrue(len(results) > 0)
        self.assertIn("REAL JIRA ISSUE", results[0])
        self.assertIn("Issue Key: SCRUM-555", results[0])
        self.assertIn("Status: Created successfully", results[0])

    @patch("graph.get_llm")
    @patch("jira_service.requests.post")
    def test_router_create_jira_ticket_failure_continues_safely(self, mock_jira_post, mock_get_llm):
        """Test user query routing to Jira when API fails with 401: workflow finishes safely."""
        mock_llm = MagicMock()
        mock_structured = MagicMock()
        mock_structured.invoke.return_value = RouterOutput(
            route="CREATE_TICKET_JIRA",
            ticket_title="MeetMind Integration Test",
            ticket_description="Integration test ticket description"
        )
        mock_llm.with_structured_output.return_value = mock_structured
        mock_get_llm.return_value = mock_llm

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = '{"errorMessages":["Unauthorized"]}'
        mock_resp.json.return_value = {"errorMessages": ["Unauthorized"]}
        mock_jira_post.return_value = mock_resp

        inputs = {
            "user_query": "Create a Jira ticket for MeetMind Integration. Description: Integration test ticket.",
            "analysis": None,
            "transcript": None,
            "ticket_result": [],
            "email_notifications": []
        }

        output = graph.invoke(inputs)
        results = output.get("ticket_result", [])
        self.assertTrue(len(results) > 0)
        self.assertIn("FAILED JIRA ISSUE (API ERROR)", results[0])
        self.assertIn("HTTP Status: 401", results[0])
        self.assertIn("Status: FAILED", results[0])

    @patch("graph.get_llm")
    @patch("github_service.requests.post")
    def test_router_create_github_issue_success(self, mock_gh_post, mock_get_llm):
        """Test user query routing to GitHub issue creation with successful mock response."""
        mock_llm = MagicMock()
        mock_structured = MagicMock()
        mock_structured.invoke.return_value = RouterOutput(
            route="CREATE_TICKET_GITHUB",
            ticket_title="MeetMind Integration Test",
            ticket_description="Integration test issue description"
        )
        mock_llm.with_structured_output.return_value = mock_structured
        mock_get_llm.return_value = mock_llm

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"number": 888, "html_url": "https://github.com/testowner/testrepo/issues/888"}
        mock_gh_post.return_value = mock_resp

        inputs = {
            "user_query": "Create a GitHub issue for MeetMind Integration. Description: Integration test issue.",
            "analysis": None,
            "transcript": None,
            "ticket_result": [],
            "email_notifications": []
        }

        output = graph.invoke(inputs)
        results = output.get("ticket_result", [])
        self.assertTrue(len(results) > 0)
        self.assertIn("REAL GITHUB ISSUE", results[0])
        self.assertIn("Issue Number: #888", results[0])
        self.assertIn("Status: Created successfully", results[0])

    @patch("graph.get_llm")
    @patch("github_service.requests.post")
    def test_router_create_github_issue_failure_continues_safely(self, mock_gh_post, mock_get_llm):
        """Test user query routing to GitHub when API fails with 401: workflow finishes safely."""
        mock_llm = MagicMock()
        mock_structured = MagicMock()
        mock_structured.invoke.return_value = RouterOutput(
            route="CREATE_TICKET_GITHUB",
            ticket_title="MeetMind Integration Test",
            ticket_description="Integration test issue description"
        )
        mock_llm.with_structured_output.return_value = mock_structured
        mock_get_llm.return_value = mock_llm

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = '{"message":"Bad credentials"}'
        mock_resp.json.return_value = {"message": "Bad credentials"}
        mock_gh_post.return_value = mock_resp

        inputs = {
            "user_query": "Create a GitHub issue for MeetMind Integration. Description: Integration test issue.",
            "analysis": None,
            "transcript": None,
            "ticket_result": [],
            "email_notifications": []
        }

        output = graph.invoke(inputs)
        results = output.get("ticket_result", [])
        self.assertTrue(len(results) > 0)
        self.assertIn("FAILED GITHUB ISSUE (API ERROR)", results[0])
        self.assertIn("HTTP Status: 401", results[0])
        self.assertIn("Status: FAILED", results[0])


if __name__ == "__main__":
    unittest.main()
