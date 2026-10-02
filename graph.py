import os
import operator
import requests
from requests.auth import HTTPBasicAuth
from typing import TypedDict, List, Optional, Literal, Annotated, Dict, Any
from pydantic import BaseModel, Field
from langchain_ollama import ChatOllama, OllamaEmbeddings
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langgraph.graph import StateGraph, START, END
from dotenv import load_dotenv
from github_service import create_github_issue_api

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)
if os.environ.get("LANGSMITH_API_KEY") == "your_langsmith_api_key_here":
    os.environ["LANGCHAIN_TRACING_V2"] = "false"

# ==========================================
# 1. MODELS
# ==========================================

class ActionItem(BaseModel):
    task: str = Field(description="The task that needs to be done")
    assignee: Optional[str] = Field(description="The person responsible for the task")
    deadline: Optional[str] = Field(description="The deadline for the task")

class JiraTask(BaseModel):
    title: str = Field(description="Title of the Jira task")
    description: str = Field(description="Description of the Jira task, including relevant context/investigation details")
    assignee: Optional[str] = Field(default=None, description="Assignee name if mentioned")

class GitHubIssue(BaseModel):
    title: str = Field(description="Title of the GitHub issue")
    description: str = Field(description="Description of the GitHub issue, including testing details or reproduction info")

class MeetingExtraction(BaseModel):
    summary: str = Field(description="A concise summary of the meeting")
    decisions: List[str] = Field(description="A list of key decisions made during the meeting")
    action_items: List[ActionItem] = Field(description="A list of action items extracted from the meeting")
    jira_tasks: List[JiraTask] = Field(
        default_factory=list,
        description="Explicit Jira tasks agreed to be created in Jira during the meeting"
    )
    github_issues: List[GitHubIssue] = Field(
        default_factory=list,
        description="Explicit GitHub issues agreed to be created in GitHub during the meeting"
    )

# Backward-compatibility alias
MeetingAnalysis = MeetingExtraction

class RouterOutput(BaseModel):
    route: Optional[Literal["RAG_QUERY", "CREATE_TICKET_JIRA", "CREATE_TICKET_GITHUB"]] = Field(
        default="RAG_QUERY",
        description="The single route to take based on the user query: RAG_QUERY, CREATE_TICKET_JIRA, or CREATE_TICKET_GITHUB."
    )
    ticket_title: Optional[str] = Field(default=None, description="If creating a ticket, the suggested title based on extracted action items")
    ticket_description: Optional[str] = Field(default=None, description="If creating a ticket, the suggested description based on extracted action items")

    def __init__(self, **data):
        if "routes" in data and "route" not in data:
            routes = data.pop("routes")
            data["route"] = routes[0] if routes else None
        super().__init__(**data)

    @property
    def routes(self) -> List[str]:
        return [self.route] if self.route else []

class AgentState(TypedDict):
    transcript: Optional[str]
    analysis: Optional[MeetingExtraction]
    user_query: Optional[str]
    retrieved_context: Optional[str]
    answer: Optional[str]
    ticket_result: Annotated[List[str], operator.add]
    email_notifications: Annotated[List[Dict[str, Any]], operator.add]
    router_output: Optional[RouterOutput]
    determined_actions: Optional[List[str]]

# ==========================================
# 2. UTILS
# ==========================================

from team_directory import get_assignee_email, load_team_members
from gmail_service import send_action_item_notification

def get_llm(model="llama3.2:3b", temperature=0):
    return ChatOllama(model=model, temperature=temperature)

def get_vector_store():
    embeddings = OllamaEmbeddings(model="nomic-embed-text")
    return Chroma(persist_directory="./chroma_db", embedding_function=embeddings)

# ==========================================
# 3. NODES
# ==========================================

def extract_meeting(state: AgentState):
    """Extract summary, action items, decisions, Jira tasks, and GitHub issues."""
    transcript = state.get("transcript", "")
    llm = get_llm()
    structured_llm = llm.with_structured_output(MeetingExtraction)
    prompt = f"""You are an expert executive meeting assistant. Analyze the following meeting transcript.
Extract the structured meeting details into the specified schema:
1. summary: A concise summary of the meeting.
2. decisions: Key decisions made during the meeting.
3. action_items: Every assigned action item with assignee, task, and deadline.
4. jira_tasks: Tasks where the team explicitly agreed to create a Jira ticket. Extract exactly ONE Jira task for each agreed topic (for example: title 'Login Timeout Issue' with description detailing the investigation into the authentication service).
5. github_issues: Issues where the team explicitly agreed to create a GitHub issue. Extract exactly ONE GitHub issue for each agreed topic (for example: title 'Concurrent Login Testing Problem' with description detailing the concurrent-login testing problem).

Meeting Transcript:
{transcript}"""
    result = structured_llm.invoke(prompt)
    return {"analysis": result}

def index_transcript(state: AgentState):
    """Chunk and index the transcript into Chroma."""
    transcript = state.get("transcript", "")
    if not transcript:
        return {}
    
    splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
    chunks = splitter.split_text(transcript)
    docs = [Document(page_content=chunk) for chunk in chunks]
    
    vector_store = get_vector_store()
    vector_store.add_documents(docs)
    return {}

def determine_actions(state: AgentState):
    """Inspect extracted analysis and automatically determine which action nodes must execute."""
    analysis = state.get("analysis")
    actions = []
    if analysis:
        if isinstance(analysis, dict):
            action_items = analysis.get("action_items") or []
            jira_tasks = analysis.get("jira_tasks") or []
            github_issues = analysis.get("github_issues") or []
        else:
            action_items = getattr(analysis, "action_items", []) or []
            jira_tasks = getattr(analysis, "jira_tasks", []) or []
            github_issues = getattr(analysis, "github_issues", []) or []

        if action_items:
            actions.append("send_email_notifications")
        if jira_tasks:
            actions.append("create_jira_ticket")
        if github_issues:
            actions.append("create_github_issue")

    print(f"\n[DETERMINE ACTIONS] Automatically determined actions to execute: {actions}")
    return {"determined_actions": actions}

def send_email_notifications(state: AgentState):
    """Notify assignees of extracted action items via email or preview."""
    analysis = state.get("analysis")
    if not analysis:
        return {}
    
    # Safely retrieve action items and summary (handle both object and dict)
    if isinstance(analysis, dict):
        action_items = analysis.get("action_items", [])
        summary = analysis.get("summary", "")
    else:
        action_items = getattr(analysis, "action_items", [])
        summary = getattr(analysis, "summary", "")
        
    if not action_items:
        return {}

    notifications = []
    seen_keys = set()
    
    # Check already processed notifications in state to prevent duplicates
    existing_notifs = state.get("email_notifications") or []
    for notif in existing_notifs:
        seen_keys.add((notif.get("assignee"), notif.get("task")))

    for item in action_items:
        if isinstance(item, dict):
            task = item.get("task", "")
            assignee = item.get("assignee")
            deadline = item.get("deadline")
        else:
            task = getattr(item, "task", "")
            assignee = getattr(item, "assignee", None)
            deadline = getattr(item, "deadline", None)

        assignee_name = assignee.strip() if assignee else "Unassigned"
        
        # Deduplication check
        item_key = (assignee_name, task)
        if item_key in seen_keys:
            duplicate_record = {
                "status": "skipped_duplicate",
                "assignee": assignee_name,
                "email": "N/A",
                "task": task,
                "message": f"SKIPPED (duplicate notification for {assignee_name}: {task})"
            }
            print(f"[EMAIL NOTIFICATION] Assignee: {assignee_name} -> Task: '{task}' -> Status: skipped_duplicate")
            notifications.append(duplicate_record)
            continue
        seen_keys.add(item_key)

        # Lookup registered email from team_directory
        recipient_email = get_assignee_email(assignee_name)
        
        # Send or generate preview via gmail_service
        result = send_action_item_notification(
            to_email=recipient_email,
            assignee_name=assignee_name,
            task=task,
            deadline=deadline,
            summary=summary
        )
        result["task"] = task
        result["deadline"] = deadline
        notifications.append(result)

        # Clear logging: assignee -> email -> notification status
        print(f"[EMAIL NOTIFICATION] Assignee: {assignee_name} -> Email: {recipient_email} -> Status: {result.get('status')} ({result.get('message') or result.get('error', '')})")

    return {"email_notifications": notifications}

def router(state: AgentState):
    """Route user query to RAG, Jira ticket, or GitHub issue."""
    query = state.get("user_query")
    analysis = state.get("analysis")
    
    if not query:
        return {"router_output": RouterOutput(route=None)}
        
    llm = get_llm()
    structured_llm = llm.with_structured_output(RouterOutput)
    
    if isinstance(analysis, dict):
        import json
        analysis_context = json.dumps(analysis)
    elif hasattr(analysis, "model_dump_json"):
        analysis_context = analysis.model_dump_json()
    else:
        analysis_context = str(analysis) if analysis else "No analysis available."
    
    prompt = f"""You are a router. Analyze the user request: "{query}"

Classify into EXACTLY ONE route:
- RAG_QUERY: Use this when the user is asking an informational question, looking for answers, or querying details about the meeting (e.g. "What did we decide...", "Who is responsible...", "What tasks...").
- CREATE_TICKET_JIRA: Use this ONLY when the user is requesting to create, open, or file a Jira ticket.
- CREATE_TICKET_GITHUB: Use this ONLY when the user is requesting to create, open, or file a GitHub issue.

CRITICAL RULES:
- Choose EXACTLY ONE route. Never select more than one route.
- Questions asking "who", "what", "when", "why", "how", "did we" must ALWAYS route to RAG_QUERY.
- Only route to CREATE_TICKET_JIRA if creating a Jira ticket is explicitly requested.
- Only route to CREATE_TICKET_GITHUB if creating a GitHub issue is explicitly requested.

Here is the extracted meeting context for action items:
{analysis_context}

If creating a ticket, also formulate a suitable ticket_title and ticket_description based on the context.
"""
    
    result = structured_llm.invoke(prompt)
    
    # Deterministic guardrails for small LLMs
    q_lower = query.lower().strip()
    is_question = any(q_lower.startswith(w) for w in ["what", "who", "when", "why", "where", "how", "did", "can", "is", "are"])
    if not is_question:
        if "jira" in q_lower and "github" not in q_lower:
            result.route = "CREATE_TICKET_JIRA"
        elif "github" in q_lower and "jira" not in q_lower:
            result.route = "CREATE_TICKET_GITHUB"
            
    return {"router_output": result}

def rag_query(state: AgentState):
    """Retrieve context and answer the query."""
    query = state.get("user_query", "")
    
    vector_store = get_vector_store()
    retriever = vector_store.as_retriever(search_kwargs={"k": 3})
    docs = retriever.invoke(query)
    context = "\n\n".join([doc.page_content for doc in docs])
    
    llm = get_llm()
    prompt = f"Answer the user's question based ONLY on the context below.\n\nContext:\n{context}\n\nQuestion: {query}"
    response = llm.invoke(prompt)
    
    return {"retrieved_context": context, "answer": response.content}

def create_jira_ticket(state: AgentState):
    """Create Jira ticket(s) automatically from extracted meeting tasks, or manual router_output."""
    import json as _json

    # --- DIAGNOSTIC: Ensure .env is loaded relative to this file ---
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    load_dotenv(_env_path, override=True)

    analysis = state.get("analysis")
    tasks_to_create = []

    # 1. Automatic mode: check if structured jira_tasks exist in analysis
    if analysis:
        if isinstance(analysis, dict):
            tasks_to_create = analysis.get("jira_tasks") or []
        else:
            tasks_to_create = getattr(analysis, "jira_tasks", []) or []

    # 2. Manual query fallback: check router_output if no structured tasks were extracted
    if not tasks_to_create:
        router_output = state.get("router_output")
        if isinstance(router_output, dict):
            title = router_output.get("ticket_title")
            desc = router_output.get("ticket_description")
        elif router_output:
            title = getattr(router_output, "ticket_title", None)
            desc = getattr(router_output, "ticket_description", None)
        else:
            title, desc = None, None
        if title:
            tasks_to_create = [{"title": title, "description": desc or "No description provided"}]

    if not tasks_to_create:
        return {}

    # Check credentials
    jira_base_url = (os.environ.get("JIRA_BASE_URL") or os.environ.get("JIRA_URL") or "").rstrip("/")
    jira_email = (os.environ.get("JIRA_EMAIL") or "").strip()
    jira_token = (os.environ.get("JIRA_API_TOKEN") or "").strip()
    project_key = (os.environ.get("JIRA_PROJECT_KEY") or "SCRUM").strip()

    # --- DIAGNOSTIC LOGGING (safe — no token printed) ---
    print(f"\n{'='*60}")
    print(f"[JIRA DIAG] .env path resolved: {_env_path}")
    print(f"[JIRA DIAG] .env file exists: {os.path.exists(_env_path)}")
    print(f"[JIRA DIAG] CWD: {os.getcwd()}")
    print(f"[JIRA DIAG] JIRA_BASE_URL = {jira_base_url!r}")
    print(f"[JIRA DIAG] JIRA_EMAIL    = {jira_email!r}")
    print(f"[JIRA DIAG] JIRA_PROJECT_KEY = {project_key!r}")
    print(f"[JIRA DIAG] JIRA_API_TOKEN configured = {bool(jira_token)}")
    print(f"[JIRA DIAG] JIRA_API_TOKEN length = {len(jira_token)}")
    print(f"[JIRA DIAG] tasks_to_create count = {len(tasks_to_create)}")
    print(f"{'='*60}\n")

    # In-execution duplicate prevention
    existing_results = state.get("ticket_result") or []
    seen_titles = set()
    for res_str in existing_results:
        for line in res_str.split("\n"):
            if line.startswith("Title:"):
                seen_titles.add(line.replace("Title:", "").strip().lower())

    results = []
    processed_in_batch = set()

    for item in tasks_to_create:
        if isinstance(item, dict):
            title = item.get("title") or "Untitled Jira Task"
            desc = item.get("description") or "No description provided"
        else:
            title = getattr(item, "title", "Untitled Jira Task")
            desc = getattr(item, "description", "No description provided")

        clean_title_key = title.strip().lower()
        if clean_title_key in seen_titles or clean_title_key in processed_in_batch:
            print(f"[JIRA TICKET] Skipping duplicate task within execution: '{title}'")
            continue
        processed_in_batch.add(clean_title_key)

        print(f"[JIRA TICKET] Processing Jira task: '{title}'...")

        # Fallback to mock mode if credentials are missing
        if not (jira_base_url and jira_email and jira_token):
            mock_str = (
                f"MOCK JIRA ISSUE\n"
                f"Title: {title}\n"
                f"Description: {desc}\n"
                f"Status: Simulated successfully"
            )
            print(f"[JIRA TICKET] Status: Simulated (credentials missing)")
            results.append(mock_str)
            continue

        endpoint = f"{jira_base_url}/rest/api/3/issue"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        auth = HTTPBasicAuth(jira_email, jira_token)
        payload = {
            "fields": {
                "project": {"key": project_key},
                "summary": title,
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": desc
                                }
                            ]
                        }
                    ]
                },
                "issuetype": {"name": "Task"}
            }
        }

        # --- DIAGNOSTIC: Log exact request details ---
        print(f"[JIRA DIAG] Endpoint: {endpoint}")
        print(f"[JIRA DIAG] Auth email: {auth.username}")
        print(f"[JIRA DIAG] Payload: {_json.dumps(payload, indent=2)}")

        try:
            response = requests.post(endpoint, headers=headers, json=payload, auth=auth, timeout=20)

            # --- DIAGNOSTIC: Log response ---
            print(f"[JIRA DIAG] Response HTTP Status: {response.status_code}")
            print(f"[JIRA DIAG] Response Body: {response.text[:500]}")

            if response.status_code in (200, 201):
                data = response.json()
                issue_key = data.get("key")
                issue_url = f"{jira_base_url}/browse/{issue_key}"
                result_str = (
                    f"REAL JIRA ISSUE\n"
                    f"Issue Key: {issue_key}\n"
                    f"Issue URL: {issue_url}\n"
                    f"HTTP Status: {response.status_code}\n"
                    f"Title: {title}\n"
                    f"Status: Created successfully"
                )
                print(f"[JIRA TICKET] SUCCESS: {issue_key} ({issue_url})")
                results.append(result_str)
            else:
                status_explanation = "Fallback simulation"
                if response.status_code == 400:
                    status_explanation = "Bad Request (Check project key, permissions, or API token)"
                elif response.status_code == 401:
                    status_explanation = "Authentication failed (Check JIRA_EMAIL and JIRA_API_TOKEN)"
                elif response.status_code == 403:
                    status_explanation = "Permission denied (User does not have access to project)"
                elif response.status_code == 404:
                    status_explanation = "Not found (Check JIRA_BASE_URL or resource)"

                safe_err = response.text[:200]
                try:
                    err_json = response.json()
                    if "errorMessages" in err_json and err_json["errorMessages"]:
                        safe_err = err_json["errorMessages"][0]
                except Exception:
                    pass

                err_str = (
                    f"MOCK JIRA ISSUE (FALLBACK)\n"
                    f"Title: {title}\n"
                    f"Description: {desc}\n"
                    f"HTTP Status: {response.status_code}\n"
                    f"Error: {safe_err}\n"
                    f"Status: {status_explanation}"
                )
                print(f"[JIRA TICKET] API Error HTTP {response.status_code}: {safe_err}")
                results.append(err_str)
        except Exception as e:
            err_str = (
                f"MOCK JIRA ISSUE (FALLBACK)\n"
                f"Title: {title}\n"
                f"Description: {desc}\n"
                f"Error: {str(e)}\n"
                f"Status: Fallback simulation"
            )
            print(f"[JIRA TICKET] Request Exception: {str(e)}")
            results.append(err_str)

    return {"ticket_result": results}

def create_github_issue(state: AgentState):
    """Create GitHub issue(s) automatically from extracted meeting tasks, or manual router_output."""
    analysis = state.get("analysis")
    issues_to_create = []

    # 1. Automatic mode: check if structured github_issues exist in analysis
    if analysis:
        if isinstance(analysis, dict):
            issues_to_create = analysis.get("github_issues") or []
        else:
            issues_to_create = getattr(analysis, "github_issues", []) or []

    # 2. Manual query fallback: check router_output if no structured issues were extracted
    if not issues_to_create:
        router_output = state.get("router_output")
        if isinstance(router_output, dict):
            title = router_output.get("ticket_title")
            desc = router_output.get("ticket_description")
        elif router_output:
            title = getattr(router_output, "ticket_title", None)
            desc = getattr(router_output, "ticket_description", None)
        else:
            title, desc = None, None
        if title:
            issues_to_create = [{"title": title, "description": desc or "No description provided"}]

    if not issues_to_create:
        return {}

    # In-execution duplicate prevention
    existing_results = state.get("ticket_result") or []
    seen_titles = set()
    for res_str in existing_results:
        for line in res_str.split("\n"):
            if line.startswith("Title:"):
                seen_titles.add(line.replace("Title:", "").strip().lower())

    results = []
    processed_in_batch = set()

    for item in issues_to_create:
        if isinstance(item, dict):
            title = item.get("title") or "Untitled GitHub Issue"
            desc = item.get("description") or "No description provided"
        else:
            title = getattr(item, "title", "Untitled GitHub Issue")
            desc = getattr(item, "description", "No description provided")

        clean_title_key = title.strip().lower()
        if clean_title_key in seen_titles or clean_title_key in processed_in_batch:
            print(f"[GITHUB ISSUE] Skipping duplicate issue within execution: '{title}'")
            continue
        processed_in_batch.add(clean_title_key)

        print(f"[GITHUB ISSUE] Processing GitHub issue: '{title}'...")
        try:
            api_result = create_github_issue_api(title=title, body=desc)
            formatted_text = api_result.get("formatted_text", f"GitHub issue: {title}")
            print(f"[GITHUB ISSUE] Status: {api_result.get('status', 'unknown')}")
            results.append(formatted_text)
        except Exception as e:
            fallback_text = (
                f"MOCK GITHUB ISSUE (FALLBACK)\n"
                f"Title: {title}\n"
                f"Description: {desc}\n"
                f"Error: {str(e)}\n"
                f"Status: Fallback simulation"
            )
            print(f"[GITHUB ISSUE] Request Exception: {str(e)}")
            results.append(fallback_text)

    return {"ticket_result": results}


# ==========================================
# 4. GRAPH ROUTING LOGIC
# ==========================================

def route_from_start(state: AgentState):
    """Determine initial step from START."""
    transcript = state.get("transcript")
    user_query = state.get("user_query")
    if user_query and user_query.strip():
        return "router"

    if transcript and transcript.strip():
        return "extract_meeting"
    return END

def route_actions(state: AgentState) -> List[str]:
    """Route to all determined actions in parallel/conditional branches."""
    determined = state.get("determined_actions") or []
    if determined:
        return determined
    return [END]

def route_from_router(state: AgentState) -> str:
    router_output = state.get("router_output")
    if not router_output:
        return END
    
    if isinstance(router_output, dict):
        route = router_output.get("route")
        if not route and router_output.get("routes"):
            routes = router_output.get("routes", [])
            route = routes[0] if routes else None
    else:
        route = getattr(router_output, "route", None)
        if not route and getattr(router_output, "routes", None):
            routes = router_output.routes
            route = routes[0] if routes else None

    if route == "CREATE_TICKET_JIRA":
        return "create_jira_ticket"
    elif route == "CREATE_TICKET_GITHUB":
        return "create_github_issue"
    elif route == "RAG_QUERY":
        return "rag_query"
        
    return END

# ==========================================
# 5. BUILD GRAPH
# ==========================================

workflow = StateGraph(AgentState)

# Register all nodes
workflow.add_node("extract_meeting", extract_meeting)
workflow.add_node("index_transcript", index_transcript)
workflow.add_node("determine_actions", determine_actions)
workflow.add_node("send_email_notifications", send_email_notifications)
workflow.add_node("create_jira_ticket", create_jira_ticket)
workflow.add_node("create_github_issue", create_github_issue)
workflow.add_node("router", router)
workflow.add_node("rag_query", rag_query)

# START transitions
workflow.add_conditional_edges(
    START,
    route_from_start,
    ["extract_meeting", "router", END]
)

# Automated Transcript Pipeline: extract -> index -> determine_actions -> conditional action fan-out
workflow.add_edge("extract_meeting", "index_transcript")
workflow.add_edge("index_transcript", "determine_actions")

workflow.add_conditional_edges(
    "determine_actions",
    route_actions,
    ["send_email_notifications", "create_jira_ticket", "create_github_issue", END]
)

# Join action branches at END
workflow.add_edge("send_email_notifications", END)
workflow.add_edge("create_jira_ticket", END)
workflow.add_edge("create_github_issue", END)

# Router transitions (for standalone user queries / backward-compatibility)
workflow.add_conditional_edges(
    "router",
    route_from_router,
    ["rag_query", "create_jira_ticket", "create_github_issue", END]
)
workflow.add_edge("rag_query", END)

graph = workflow.compile()
