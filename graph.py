import os
import datetime
import re
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
from jira_service import create_jira_issue_api
from google_calendar_service import create_calendar_events_for_action_items
from sprint_database import save_meeting, DEFAULT_DB as SPRINT_DB
from sprint_comparison_engine import compare_sprint_meetings

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=False)
if os.environ.get("LANGSMITH_API_KEY") == "your_langsmith_api_key_here":
    os.environ["LANGCHAIN_TRACING_V2"] = "false"

# ==========================================
# 1. MODELS
# ==========================================

class ActionItem(BaseModel):
    task: str = Field(description="The task that needs to be done")
    assignee: Optional[str] = Field(description="The person responsible for the task")
    deadline: Optional[str] = Field(description="The deadline for the task, formatted exactly as YYYY-MM-DD")
    task_size: Optional[Literal["SMALL", "MEDIUM", "LARGE"]] = Field(default=None, description="Task size classification: SMALL, MEDIUM, or LARGE.")
    context: Optional[str] = Field(default=None, description="Why this task was discussed and the relevant discussion surrounding it. Filter out unrelated meeting discussions. Only include information explicitly present.")
    progress: Optional[str] = Field(default=None, description="Information about what has already been completed or discussed regarding this specific task. Omit and return null if not explicitly mentioned. NEVER output generic values like 'To-Do', 'Pending', 'Not started', or 'In progress'.")
    expected_outcome: Optional[str] = Field(default=None, description="What the assignee is expected to achieve, based ONLY on the transcript. Do not invent details.")
    decisions: Optional[str] = Field(default=None, description="Specific decisions or agreements made during the meeting that affect this task. Omit if none.")
    next_steps: Optional[str] = Field(default=None, description="Concrete next steps extracted from the transcript. Do not invent steps.")
    blockers: Optional[str] = Field(default=None, description="Blockers or dependencies explicitly mentioned for this task. Omit if none.")

class JiraTask(BaseModel):
    title: str = Field(description="Title of the Jira task")
    description: str = Field(description="Description of the Jira task, including relevant context/investigation details")
    assignee: Optional[str] = Field(default=None, description="Assignee name if mentioned")

class GitHubIssue(BaseModel):
    title: str = Field(description="Title of the GitHub issue")
    description: str = Field(description="Description of the GitHub issue, including testing details or reproduction info")

class ActionItemDetails(BaseModel):
    deadline: Optional[str] = Field(description="The deadline for THIS SPECIFIC task, formatted exactly as YYYY-MM-DD. Set to null if no deadline is explicitly stated for THIS task.")
    task_size: Optional[Literal["SMALL", "MEDIUM", "LARGE"]] = Field(default=None, description="Classify the task size: SMALL (focused/limited scope), MEDIUM (multiple related steps), LARGE (substantial work/integration).")
    context: Optional[str] = Field(default=None, description="Why this task was discussed and the relevant discussion surrounding it. Filter out unrelated meeting discussions. Only include information explicitly present.")
    progress: Optional[str] = Field(default=None, description="Information about what has already been completed or discussed regarding this specific task. Omit and return null if not explicitly mentioned. NEVER output generic values like 'To-Do', 'Pending', 'Not started', or 'In progress'.")
    expected_outcome: Optional[str] = Field(default=None, description="What the assignee is expected to achieve, based ONLY on the transcript. Do not invent details.")
    decisions: Optional[str] = Field(default=None, description="Specific decisions or agreements made during the meeting that affect this task. Omit if none.")
    next_steps: Optional[str] = Field(default=None, description="Concrete next steps extracted from the transcript. Do not invent steps.")
    blockers: Optional[str] = Field(default=None, description="Blockers or dependencies explicitly mentioned for this task. Omit if none.")

class RawActionItem(BaseModel):
    assignee: str = Field(description="The person responsible for the task")
    task: str = Field(description="A brief description of the task")

class MeetingExtractionPass1(BaseModel):
    summary: str = Field(description="A concise summary of the meeting")
    decisions: List[str] = Field(description="A list of key decisions made during the meeting")
    action_items: List[RawActionItem] = Field(description="A COMPLETE and EXHAUSTIVE list of EVERY SINGLE action item extracted from the meeting. You MUST include every person mentioned with a task.")
    jira_tasks: List[JiraTask] = Field(
        default_factory=list,
        description="Explicit Jira tasks agreed to be created in Jira during the meeting"
    )
    github_issues: List[GitHubIssue] = Field(
        default_factory=list,
        description="Explicit GitHub issues agreed to be created in GitHub during the meeting"
    )

class MeetingExtraction(BaseModel):
    summary: str = Field(description="A concise summary of the meeting")
    decisions: List[str] = Field(description="A list of key decisions made during the meeting")
    action_items: List[ActionItem] = Field(description="A COMPLETE and EXHAUSTIVE list of EVERY SINGLE action item extracted from the meeting. You MUST include every person mentioned with a task.")
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
    meeting_id: Optional[str]
    meeting_title: Optional[str]
    transcript: Optional[str]
    analysis: Optional[MeetingExtraction]
    user_query: Optional[str]
    retrieved_context: Optional[str]
    answer: Optional[str]
    ticket_result: Annotated[List[str], operator.add]
    email_notifications: Annotated[List[Dict[str, Any]], operator.add]
    router_output: Optional[RouterOutput]
    determined_actions: Optional[List[str]]
    calendar_events: Annotated[List[Dict[str, Any]], operator.add]
    project_key: Optional[str]
    sprint_comparison: Optional[Dict[str, Any]]

# ==========================================
# 2. UTILS
# ==========================================

from team_directory import get_assignee_email, load_team_members
from gmail_service import send_action_item_notification

def get_llm(model="llama3.2:3b", temperature=0):
    return ChatOllama(model=model, temperature=temperature, num_predict=2000)

def get_vector_store():
    embeddings = OllamaEmbeddings(model="nomic-embed-text")
    return Chroma(persist_directory="./chroma_db", embedding_function=embeddings)

# ==========================================
# 3. NODES
# ==========================================

def normalize_transcript(text: Optional[str]) -> str:
    """Normalize transcript text for invariant duplicate detection across whitespace and line endings."""
    if not text:
        return ""
    lines = [re.sub(r'[ \t]+', ' ', line.strip()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)

def extract_meeting(state: AgentState):
    """Extract summary, action items, decisions, Jira tasks, and GitHub issues."""
    if state.get("analysis") is not None:
        return {"analysis": state.get("analysis")}
    transcript = state.get("transcript", "")
    llm = get_llm()
    structured_llm_pass1 = llm.with_structured_output(MeetingExtractionPass1)
    prompt_pass1 = f"""You are an expert executive meeting assistant. Analyze the following meeting transcript.
Extract the structured meeting details into the specified schema:
1. summary: A concise but detailed paragraph summarizing the actual meeting discussion. 
Generate the meeting summary using ONLY the CURRENT TRANSCRIPT provided below.
Do not use retrieved_context, previous meetings, historical ChromaDB results, memory, or information not present in the current transcript.
Do not invent participants, tasks, decisions, deadlines, or events.
DO NOT just output the meeting title. You must summarize the CURRENT TRANSCRIPT content.
2. decisions: Key decisions made during the meeting.
3. action_items: Every assigned action item with assignee and task.
4. jira_tasks: Tasks where the team explicitly agreed to create a Jira ticket. Extract exactly ONE Jira task for each agreed topic.
5. github_issues: Issues where participants explicitly agreed or requested to create a GitHub issue (e.g., 'create a GitHub issue for testing the authentication API'). You MUST extract a GitHub issue with title and description.

CRITICAL, MANDATORY INSTRUCTIONS:
- READ THE TRANSCRIPT LINE BY LINE.
- You must identify EVERY SINGLE action item for EVERY SINGLE person.
- If there are 6 people mentioned with tasks, YOU MUST OUTPUT AT LEAST 6 ACTION ITEMS in the action_items array.
- DO NOT STOP AFTER THE FIRST ITEM. YOU MUST EXTRACT THEM ALL.
- If the same person has multiple tasks, output multiple items for them.
- If someone explicitly asks to create a GitHub issue or Jira ticket, record it in github_issues and jira_tasks respectively.

EXAMPLE OUTPUT FORMAT for action_items:
[
  {{"assignee": "Alice", "task": "Update the documentation by Friday"}},
  {{"assignee": "Bob", "task": "Review the pull request"}},
  {{"assignee": "Charlie", "task": "Fix the database bug"}}
]

Meeting Transcript:
{transcript}"""
    result_pass1 = structured_llm_pass1.invoke(prompt_pass1)

    structured_llm_pass2 = llm.with_structured_output(ActionItemDetails)
    detailed_action_items = []
    
    from google_calendar_service import resolve_deterministic_deadline

    # Determine reference date for relative weekday arithmetic (default to today or state meeting_date)
    ref_date = state.get("meeting_date") or datetime.date.today()
    if isinstance(ref_date, str):
        try:
            ref_date = datetime.datetime.strptime(ref_date[:10], "%Y-%m-%d").date()
        except Exception:
            ref_date = datetime.date.today()

    if result_pass1 and result_pass1.action_items:
        for raw_item in result_pass1.action_items:
            current_time_str = datetime.datetime.now().strftime("%A, %B %d, %Y %I:%M %p")
            prompt_pass2 = f"""Extract details strictly for the specific task '{raw_item.task}' assigned to '{raw_item.assignee}'.
CRITICAL INSTRUCTIONS:
- ONLY extract information related to THIS SPECIFIC TASK: '{raw_item.task}'.
- IGNORE ALL OTHER TASKS. DO NOT copy the deadline of another task.
- If the phrase '{raw_item.task}' does not explicitly contain a deadline, you MUST set the deadline field to null. DO NOT look at other sentences for the deadline.
- Do not output generic fillers like "Not specified", "N/A", or "None". Leave the field null.
- The current meeting date/time is {current_time_str}. Use this as the reference when resolving relative deadlines (e.g., 'tomorrow', 'Friday', 'next Monday'). If a deadline cannot be confidently interpreted, leave it null.
- You MUST output the `deadline` field exactly in YYYY-MM-DD format based on {current_time_str}.
- You MUST classify the `task_size` field as one of: SMALL, MEDIUM, LARGE.

Meeting Transcript:
{transcript}"""
            try:
                details = structured_llm_pass2.invoke(prompt_pass2)
                
                # Deterministic Python Weekday Resolution:
                # Stop depending on the LLM for weekday arithmetic.
                # Use ref_date and explicit temporal keywords to calculate exact calendar dates.
                resolved_deadline = resolve_deterministic_deadline(
                    task_text=raw_item.task,
                    ref_date=ref_date,
                    extracted_deadline=details.deadline
                )

                detailed_action_items.append(ActionItem(
                    assignee=raw_item.assignee,
                    task=raw_item.task,
                    deadline=resolved_deadline,
                    task_size=details.task_size,
                    context=details.context,
                    progress=details.progress,
                    expected_outcome=details.expected_outcome,
                    decisions=details.decisions,
                    next_steps=details.next_steps,
                    blockers=details.blockers
                ))
            except Exception as e:
                print(f"Error expanding action item {raw_item.task}: {e}")
                fallback_dl = resolve_deterministic_deadline(task_text=raw_item.task, ref_date=ref_date)
                detailed_action_items.append(ActionItem(
                    assignee=raw_item.assignee,
                    task=raw_item.task,
                    deadline=fallback_dl
                ))

    # Deterministic fallback for explicit GitHub issue creation requests:
    extracted_github_issues = list(result_pass1.github_issues) if (result_pass1 and result_pass1.github_issues) else []
    if not extracted_github_issues:
        for item in detailed_action_items:
            t_text = item.task.strip()
            # Must contain explicit creation verb + github issue
            m = re.search(
                r'\b(?:create|open|file|submit|add)\b(?:\s+(?:a|an|the))?\s+\bgithub\s+issue\b(?:\s+(?:for|to|about|on))?\s*(.*)',
                t_text,
                re.IGNORECASE
            )
            if m:
                extracted_topic = m.group(1).strip()
                extracted_topic = re.sub(r'[\.\,\;]+$', '', extracted_topic).strip()
                issue_title = extracted_topic if extracted_topic else t_text
                if issue_title and issue_title[0].islower():
                    issue_title = issue_title[0].upper() + issue_title[1:]

                desc = f"Action item assigned to {item.assignee or 'team'}: {t_text}"
                if item.context:
                    desc += f"\nContext: {item.context}"

                extracted_github_issues.append(GitHubIssue(
                    title=issue_title,
                    description=desc
                ))
                print(f"[EXTRACT MEETING] Fallback extracted GitHub issue: '{issue_title}' from action item '{t_text}'")

    if not extracted_github_issues and transcript:
        existing_gh_titles = {i.title.strip().lower() for i in extracted_github_issues}
        for line in transcript.splitlines():
            if "github" in line.lower() and "issue" in line.lower():
                m = re.search(
                    r'\b(?:create|open|file|submit|add)\b(?:\s+(?:a|an|the))?\s+\bgithub\s+issue\b(?:\s+(?:for|to|about|on))?\s*([^\.\n]+)',
                    line,
                    re.IGNORECASE
                )
                if m:
                    extracted_topic = m.group(1).strip()
                    extracted_topic = re.sub(r'[\.\,\;]+$', '', extracted_topic).strip()
                    issue_title = extracted_topic if extracted_topic else "GitHub Issue"
                    if issue_title and issue_title[0].islower():
                        issue_title = issue_title[0].upper() + issue_title[1:]
                    if issue_title.lower() not in existing_gh_titles:
                        extracted_github_issues.append(GitHubIssue(
                            title=issue_title,
                            description=f"Requested in meeting: {line.strip()}"
                        ))
                        existing_gh_titles.add(issue_title.lower())
                        print(f"[EXTRACT MEETING] Transcript fallback extracted GitHub issue: '{issue_title}' from line '{line.strip()}'")

    final_result = MeetingExtraction(
        summary=result_pass1.summary if result_pass1 else "No summary",
        decisions=result_pass1.decisions if result_pass1 else [],
        action_items=detailed_action_items,
        jira_tasks=result_pass1.jira_tasks if result_pass1 else [],
        github_issues=extracted_github_issues
    )
    
    return {"analysis": final_result}

def index_transcript(state: AgentState):
    """Chunk and index the transcript into Chroma."""
    transcript = state.get("transcript", "")
    if not transcript:
        return {}
    
    import hashlib
    norm_transcript = normalize_transcript(transcript)
    # Derive meeting_id directly from normalized transcript to prevent stale state and whitespace divergence
    meeting_id = hashlib.sha256(norm_transcript.encode('utf-8')).hexdigest()[:12]
    
    incoming_title = state.get("meeting_title")
    meeting_title = incoming_title.strip() if incoming_title and incoming_title.strip() else f"Meeting {meeting_id}"
    
    vector_store = get_vector_store()
    
    try:
        results = vector_store.get(where={"meeting_id": meeting_id})
        if results and results.get("ids") and len(results["ids"]) > 0:
            print(f"[INDEX] Meeting {meeting_id} already indexed, skipping.")
            return {"meeting_id": meeting_id, "meeting_title": meeting_title}
    except Exception as e:
        print(f"[INDEX] Error checking existing meeting docs: {e}")
        pass
    
    try:
        splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
        chunks = splitter.split_text(transcript)
        docs = [Document(page_content=chunk, metadata={"meeting_id": meeting_id}) for chunk in chunks]
        
        vector_store.add_documents(docs)
    except Exception as e:
        print(f"[INDEX] Error indexing documents: {e}")
    return {"meeting_id": meeting_id, "meeting_title": meeting_title}

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
        # Create Calendar events for any action items that have a deadline
        items_with_deadlines = [
            i for i in action_items
            if (i.get("deadline") if isinstance(i, dict) else getattr(i, "deadline", None))
        ]
        if items_with_deadlines:
            actions.append("create_calendar_events")

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

    meeting_id = state.get("meeting_id")
    meeting_title = state.get("meeting_title")
    if not meeting_title:
        meeting_title = f"Meeting {meeting_id}" if meeting_id else "Unknown Meeting"

    from reminder_service import schedule_reminders
    schedule_reminders(action_items, meeting_title)

    grouped_items = {}
    for item in action_items:
        if isinstance(item, dict):
            task = item.get("task", "")
            assignee = item.get("assignee")
            deadline = item.get("deadline")
            context_str = item.get("context")
            progress_str = item.get("progress")
            expected_outcome = item.get("expected_outcome")
            item_decisions = item.get("decisions")
            next_steps = item.get("next_steps")
            blockers = item.get("blockers")
        else:
            task = getattr(item, "task", "")
            assignee = getattr(item, "assignee", None)
            deadline = getattr(item, "deadline", None)
            context_str = getattr(item, "context", None)
            progress_str = getattr(item, "progress", None)
            expected_outcome = getattr(item, "expected_outcome", None)
            item_decisions = getattr(item, "decisions", None)
            next_steps = getattr(item, "next_steps", None)
            blockers = getattr(item, "blockers", None)

        assignee_name = assignee.strip() if assignee else "Unassigned"
        if assignee_name not in grouped_items:
            grouped_items[assignee_name] = []
        
        grouped_items[assignee_name].append({
            "task": task,
            "deadline": deadline,
            "context_str": context_str,
            "progress_str": progress_str,
            "expected_outcome": expected_outcome,
            "item_decisions": item_decisions,
            "next_steps": next_steps,
            "blockers": blockers
        })

    meeting_id = state.get("meeting_id") or "UNKNOWN_MEETING"
    notifications = []
    seen_keys = set()
    
    # Check already processed notifications in state scoped to current meeting_id
    existing_notifs = state.get("email_notifications") or []
    for notif in existing_notifs:
        notif_mid = notif.get("meeting_id")
        if not notif_mid or notif_mid == meeting_id:
            seen_keys.add(notif.get("assignee"))

    for assignee_name, tasks_list in grouped_items.items():
        # 1. Resolve registered email from team directory FIRST
        recipient_email = get_assignee_email(assignee_name)

        # 2. Check duplicate notification scoped by meeting_id and assignee
        if assignee_name in seen_keys:
            duplicate_record = {
                "status": "skipped_duplicate",
                "assignee": assignee_name,
                "email": recipient_email or "N/A",
                "meeting_id": meeting_id,
                "task": "Multiple tasks" if len(tasks_list) > 1 else tasks_list[0]['task'],
                "message": f"SKIPPED (duplicate notification for {assignee_name})"
            }
            print(f"[EMAIL NOTIFICATION] Assignee: {assignee_name} -> Email: {recipient_email or 'N/A'} -> Status: skipped_duplicate")
            notifications.append(duplicate_record)
            continue
        seen_keys.add(assignee_name)

        if len(tasks_list) == 1:
            t = tasks_list[0]
            task = t['task']
            deadline = t['deadline']
            context_str = t['context_str']
            progress_str = t['progress_str']
            expected_outcome = t['expected_outcome']
            item_decisions = t['item_decisions']
            next_steps = t['next_steps']
            blockers = t['blockers']
        else:
            task_parts = []
            deadline_parts = []
            context_parts = []
            
            def is_valid_field(val):
                if not val: return False
                banned = {"none", "n/a", "null", "", "not specified", "to-do", "pending", "not started", "in progress"}
                return str(val).lower().strip() not in banned

            for i, t in enumerate(tasks_list, 1):
                task_parts.append(f"{i}. {t['task']}")
                if is_valid_field(t['deadline']):
                    deadline_parts.append(f"{i}. {t['deadline']}")
                if is_valid_field(t['context_str']):
                    context_parts.append(f"{i}. {t['context_str']}")
            
            task = "\n".join(task_parts)
            deadline = "\n".join(deadline_parts) if deadline_parts else None
            context_str = "\n".join(context_parts) if context_parts else None
            progress_str = None
            expected_outcome = None
            item_decisions = None
            next_steps = None
            blockers = None

        # 3. If recipient email is missing for a named assignee, report clear error
        if not recipient_email or recipient_email == "email not found":
            if assignee_name and assignee_name.strip() not in ("", "Unassigned", "None", "Unknown"):
                err_msg = f"FAILED (Assignee '{assignee_name}' email not found in company directory)"
                error_record = {
                    "status": "FAILED",
                    "assignee": assignee_name,
                    "email": "None",
                    "meeting_id": meeting_id,
                    "task": task,
                    "message": err_msg,
                    "error": err_msg
                }
                print(f"[EMAIL NOTIFICATION] Assignee: {assignee_name} -> Status: FAILED ({err_msg})")
                notifications.append(error_record)
                continue
        
        # Send or generate preview via gmail_service
        raw_result = send_action_item_notification(
            to_email=recipient_email,
            assignee_name=assignee_name,
            task=task,
            deadline=deadline,
            summary=summary,
            context=context_str,
            progress=progress_str,
            expected_outcome=expected_outcome,
            decisions=item_decisions,
            next_steps=next_steps,
            blockers=blockers
        )
        result = dict(raw_result) if isinstance(raw_result, dict) else {"status": "UNKNOWN"}
        result["task"] = task
        result["deadline"] = deadline
        result["meeting_id"] = meeting_id
        if "assignee" not in result or not result["assignee"]:
            result["assignee"] = assignee_name
        if "email" not in result or not result["email"] or result["email"] == "None":
            result["email"] = recipient_email or "None"
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
    import re
    query = state.get("user_query", "")
    transcript = state.get("transcript", "")
    
    import hashlib
    # Derive meeting_id directly from current transcript to avoid stale state in LangGraph Studio
    meeting_id = hashlib.sha256(transcript.encode('utf-8')).hexdigest()[:12]
    
    incoming_title = state.get("meeting_title")

    
    q_lower = query.lower()
    
    # 1. Intercept deterministic participant queries
    participant_keywords = [
        "how many people", "how many members", "who are the people", 
        "who are the members", "who are the participants", "who attended", 
        "list the participants", "list the members", "list the people",
        "who is in this meeting", "how many people in this meeting",
        "who is in the meeting"
    ]
    if transcript and any(kw in q_lower for kw in participant_keywords):
        speakers = set()
        for line in transcript.split("\n"):
            match = re.match(r'^\s*([A-Za-z0-9_\-\s]+):', line)
            if match:
                speakers.add(match.group(1).strip())
        
        if speakers:
            speakers_list = sorted(list(speakers))
            count = len(speakers_list)
            names = ", ".join(speakers_list)
            answer = f"There are {count} people in this meeting: {names}."
            print(f"[RAG] Deterministic participant calculation used: {count}")
            return {"retrieved_context": "Deterministic calculation from transcript", "answer": answer}

    # 2. Normal RAG behavior
    historical_keywords = [
        "past meeting", "previous meeting", "other meeting", 
        "historical", "last meeting", "all meetings", "before this"
    ]
    
    # Default to current meeting if we have a transcript, unless user explicitly asks for history
    is_current_meeting = False
    if meeting_id:
        if any(kw in q_lower for kw in historical_keywords):
            is_current_meeting = False
        else:
            is_current_meeting = True

    vector_store = get_vector_store()
    
    if is_current_meeting:
        print(f"[RAG] Filtering context by meeting_id: {meeting_id}")
        retriever = vector_store.as_retriever(search_kwargs={"k": 5, "filter": {"meeting_id": meeting_id}})
    else:
        print("[RAG] Searching across all historical meetings")
        retriever = vector_store.as_retriever(search_kwargs={"k": 5})
        
    docs = retriever.invoke(query)
    
    # If we are strictly querying the current meeting, we can also inject the raw transcript
    # if it's short enough to fit in the context window.
    context = "\n\n".join([doc.page_content for doc in docs])
    
    if is_current_meeting and transcript and len(transcript) < 4000:
        context = f"Full Transcript:\n{transcript}\n\nRelevant Chunks:\n{context}"
    
    llm = get_llm()
    prompt = f"Answer the user's question based ONLY on the context below. Do not invent participants or events not in the context. If the information is not available in the context provided, clearly state that the information is not available in the current meeting.\n\nContext:\n{context}\n\nQuestion: {query}"
    response = llm.invoke(prompt)
    
    return {"retrieved_context": context, "answer": response.content}

def create_jira_ticket(state: AgentState):
    """Create Jira ticket(s) automatically from extracted meeting tasks, or manual router_output."""
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

    # In-execution duplicate prevention
    existing_results = state.get("ticket_result") or []
    seen_titles = set()
    for res_str in existing_results:
        for line in res_str.split("\n"):
            if line.startswith("Title:"):
                seen_titles.add(line.replace("Title:", "").strip().lower())

    results = []
    processed_in_batch = set()

    project_key = (state.get("project_key") or os.environ.get("JIRA_PROJECT_KEY") or "SCRUM").strip().upper()
    db_path = os.environ.get("SPRINT_COMPARISON_DB") or SPRINT_DB

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

        # Resolve item metadata
        task_id = item.get("task_id") if isinstance(item, dict) else getattr(item, "task_id", None)
        assignee = item.get("assignee") if isinstance(item, dict) else getattr(item, "assignee", None)
        meeting_id = state.get("meeting_id")

        # Correlate assignee and task_id from analysis action items if not directly on Jira task
        if not assignee or not task_id:
            raw_ais = []
            if isinstance(analysis, dict):
                raw_ais = analysis.get("action_items") or []
            elif analysis:
                raw_ais = getattr(analysis, "action_items", []) or []
            for ai in raw_ais:
                ai_t = ai.get("task", "") if isinstance(ai, dict) else getattr(ai, "task", "")
                if clean_title_key in ai_t.lower() or ai_t.lower() in clean_title_key:
                    if not assignee:
                        assignee = ai.get("assignee") if isinstance(ai, dict) else getattr(ai, "assignee", None)
                    if not task_id:
                        task_id = ai.get("task_id") if isinstance(ai, dict) else getattr(ai, "task_id", None)
                    break

        # 1. Deterministic Idempotency Key
        from sprint_database import (
            generate_jira_idempotency_key,
            claim_jira_creation,
            resolve_jira_claim,
            get_cached_jira_ticket
        )
        idempotency_key = generate_jira_idempotency_key(
            project_key=project_key,
            summary=title,
            meeting_id=meeting_id,
            task_id=task_id,
            assignee=assignee
        )

        # 2. Check 4-Tier Cached Jira Ticket
        cached_key = get_cached_jira_ticket(
            project_key=project_key,
            summary=title,
            meeting_id=meeting_id,
            task_id=task_id,
            assignee=assignee,
            db_path=db_path
        )
        if cached_key:
            print(f"[JIRA TICKET] Idempotency: Reusing cached Jira issue '{cached_key}' for '{title}'")
            jira_base = (os.environ.get("JIRA_BASE_URL") or os.environ.get("JIRA_URL") or "").rstrip("/")
            issue_url = f"{jira_base}/browse/{cached_key}" if jira_base else f"https://jira/{cached_key}"
            formatted_text = (
                f"REAL JIRA ISSUE\n"
                f"Issue Key: {cached_key}\n"
                f"Issue URL: {issue_url}\n"
                f"HTTP Status: 200 (REUSED)\n"
                f"Title: {title}\n"
                f"Status: Reused existing issue"
            )
            results.append(formatted_text)
            continue

        # 3. Check Jira Cloud directly if credentials are configured
        existing_jira_key = None
        try:
            from jira_service import get_jira_issues_for_project, get_jira_credentials
            base_url, email, token, _ = get_jira_credentials()
            if base_url and email and token:
                fetch_res = get_jira_issues_for_project(project_key)
                if fetch_res.get("status") == "SUCCESS":
                    from sprint_database import normalize_task_name
                    norm_title = normalize_task_name(title)
                    for j_issue in fetch_res.get("issues", []):
                        j_summary = (j_issue.get("summary") or "").strip().lower()
                        if j_summary == clean_title_key or normalize_task_name(j_summary) == norm_title:
                            existing_jira_key = j_issue["key"]
                            break
        except Exception as e:
            print(f"[JIRA TICKET] Note: could not check Jira for existing issue: {e}")

        if existing_jira_key:
            print(f"[JIRA TICKET] Idempotency: Reusing existing Jira issue '{existing_jira_key}' from Jira Cloud for '{title}'")
            jira_base = (os.environ.get("JIRA_BASE_URL") or os.environ.get("JIRA_URL") or "").rstrip("/")
            issue_url = f"{jira_base}/browse/{existing_jira_key}" if jira_base else f"https://jira/{existing_jira_key}"
            formatted_text = (
                f"REAL JIRA ISSUE\n"
                f"Issue Key: {existing_jira_key}\n"
                f"Issue URL: {issue_url}\n"
                f"HTTP Status: 200 (REUSED)\n"
                f"Title: {title}\n"
                f"Status: Reused existing issue"
            )
            results.append(formatted_text)
            resolve_jira_claim(idempotency_key, status="CREATED", ticket_key=existing_jira_key, db_path=db_path)
            continue

        # 4. Atomic Claim for Concurrency Lock
        claim_status, claim_ticket_key, claim_msg = claim_jira_creation(
            idempotency_key=idempotency_key,
            project_key=project_key,
            summary=title,
            meeting_id=meeting_id,
            task_id=task_id,
            db_path=db_path
        )

        if claim_status == "ALREADY_CREATED" and claim_ticket_key:
            jira_base = (os.environ.get("JIRA_BASE_URL") or os.environ.get("JIRA_URL") or "").rstrip("/")
            issue_url = f"{jira_base}/browse/{claim_ticket_key}" if jira_base else f"https://jira/{claim_ticket_key}"
            formatted_text = (
                f"REAL JIRA ISSUE\n"
                f"Issue Key: {claim_ticket_key}\n"
                f"Issue URL: {issue_url}\n"
                f"HTTP Status: 200 (REUSED)\n"
                f"Title: {title}\n"
                f"Status: Reused existing issue"
            )
            results.append(formatted_text)
            continue

        if claim_status == "IN_PROGRESS":
            print(f"[JIRA TICKET] Concurrency guard: Creation in progress for '{title}'. Skipping duplicate call.")
            in_prog_text = (
                f"IN_PROGRESS JIRA ISSUE\n"
                f"Title: {title}\n"
                f"Status: IN_PROGRESS\n"
                f"Message: {claim_msg}"
            )
            results.append(in_prog_text)
            continue

        if claim_status == "UNCERTAIN_TIMEOUT":
            print(f"[JIRA TICKET] Uncertain timeout guard: Prior attempt for '{title}' timed out. Review required before retry.")
            uncertain_text = (
                f"FAILED JIRA ISSUE (UNCERTAIN TIMEOUT)\n"
                f"Title: {title}\n"
                f"HTTP Status: UNCERTAIN_TIMEOUT\n"
                f"Status: FAILED\n"
                f"Error: {claim_msg}"
            )
            results.append(uncertain_text)
            continue

        # claim_status == "CLAIMED": Proceed with Jira Creation
        print(f"[JIRA TICKET] Processing Jira task: '{title}'...")
        try:
            api_result = create_jira_issue_api(title=title, description=desc, project_key=project_key)
            formatted_text = api_result.get("formatted_text", f"Jira task: {title}")
            print(f"[JIRA TICKET] Status: {api_result.get('status', 'unknown')} (HTTP {api_result.get('http_status')})")
            results.append(formatted_text)

            issue_key = api_result.get("issue_key")
            if not issue_key:
                m = re.search(r"Issue Key:\s*([A-Z0-9]+-\d+)", formatted_text, re.IGNORECASE)
                if m:
                    issue_key = m.group(1).strip().upper()

            status_res = api_result.get("status")
            err_str = api_result.get("error") or ""
            is_valid_key = bool(issue_key and re.match(r"^[A-Z][A-Z0-9]+-\d+$", str(issue_key).strip().upper()))

            if is_valid_key and status_res in ("SUCCESS", "SIMULATED"):
                clean_key = str(issue_key).strip().upper()
                resolve_jira_claim(idempotency_key, status="CREATED", ticket_key=clean_key, db_path=db_path)
            elif "timed out" in err_str.lower() and "read" in err_str.lower():
                resolve_jira_claim(idempotency_key, status="UNCERTAIN_TIMEOUT", error_message=err_str, db_path=db_path)
            else:
                resolve_jira_claim(idempotency_key, status="FAILED", error_message=err_str or "Creation failed: invalid or missing issue key", db_path=db_path)

        except Exception as e:
            err_msg = str(e)
            fallback_text = (
                f"FAILED JIRA ISSUE (API ERROR)\n"
                f"Title: {title}\n"
                f"HTTP Status: EXCEPTION\n"
                f"Error: {err_msg}\n"
                f"Status: FAILED"
            )
            print(f"[JIRA TICKET] Request Exception: {err_msg}")
            results.append(fallback_text)
            if "read" in err_msg.lower() and "timeout" in err_msg.lower():
                resolve_jira_claim(idempotency_key, status="UNCERTAIN_TIMEOUT", error_message=err_msg, db_path=db_path)
            else:
                resolve_jira_claim(idempotency_key, status="FAILED", error_message=err_msg, db_path=db_path)

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
    meeting_id = state.get("meeting_id")
    db_path = os.environ.get("SPRINT_COMPARISON_DB") or SPRINT_DB

    from sprint_database import get_cached_github_issue, record_created_github_issue
    from github_service import get_github_credentials

    token, owner, repo = get_github_credentials()

    for item in issues_to_create:
        if isinstance(item, dict):
            title = (item.get("title") or "").strip()
            desc = (item.get("description") or "No description provided").strip()
        else:
            title = (getattr(item, "title", "") or "").strip()
            desc = (getattr(item, "description", "") or "No description provided").strip()

        if not title:
            title = "Untitled GitHub Issue"

        clean_title_key = title.strip().lower()
        if clean_title_key in seen_titles or clean_title_key in processed_in_batch:
            print(f"[GITHUB ISSUE] Skipping duplicate issue within execution: '{title}'")
            continue
        processed_in_batch.add(clean_title_key)

        # 1. Check SQLite for existing created GitHub issue for this meeting to prevent duplicates
        cached = get_cached_github_issue(meeting_id=meeting_id, title=title, db_path=db_path)
        if cached:
            cached_num = cached.get("issue_number")
            cached_url = cached.get("issue_url") or f"https://github.com/{owner}/{repo}/issues/{cached_num}"
            print(f"[GITHUB ISSUE] Idempotency: Reusing cached GitHub issue #{cached_num} for '{title}'")
            formatted_text = (
                f"REAL GITHUB ISSUE\n"
                f"Issue Number: #{cached_num}\n"
                f"Issue URL: {cached_url}\n"
                f"HTTP Status: 200 (REUSED)\n"
                f"Title: {title}\n"
                f"Status: Reused existing issue"
            )
            results.append(formatted_text)
            continue

        # 2. Validate configuration
        if not (owner and repo):
            err_msg = "Invalid GitHub repository configuration: GITHUB_OWNER or GITHUB_REPO missing"
            fallback_text = (
                f"FAILED GITHUB ISSUE (CONFIG ERROR)\n"
                f"Title: {title}\n"
                f"Error: {err_msg}\n"
                f"Status: FAILED"
            )
            print(f"[GITHUB ISSUE] Configuration error: {err_msg}")
            results.append(fallback_text)
            continue

        print(f"[GITHUB ISSUE] Processing GitHub issue: '{title}' for repository {owner}/{repo}...")
        try:
            api_result = create_github_issue_api(title=title, body=desc)
            formatted_text = api_result.get("formatted_text", f"GitHub issue: {title}")
            status_res = api_result.get("status")
            print(f"[GITHUB ISSUE] Status: {status_res} (HTTP {api_result.get('http_status')})")
            results.append(formatted_text)

            if status_res in ("SUCCESS", "SIMULATED"):
                issue_num = api_result.get("issue_number")
                issue_url = api_result.get("issue_url")
                record_created_github_issue(
                    meeting_id=meeting_id,
                    issue_number=issue_num,
                    title=title,
                    issue_url=issue_url,
                    db_path=db_path
                )
        except Exception as e:
            fallback_text = (
                f"FAILED GITHUB ISSUE (API ERROR)\n"
                f"Title: {title}\n"
                f"HTTP Status: EXCEPTION\n"
                f"Error: {str(e)}\n"
                f"Status: FAILED"
            )
            print(f"[GITHUB ISSUE] Request Exception: {str(e)}")
            results.append(fallback_text)

    return {"ticket_result": results}


def create_calendar_events(state: AgentState):
    """Create Google Calendar events for action items that have valid deadlines."""
    analysis = state.get("analysis")
    if not analysis:
        return {}

    if isinstance(analysis, dict):
        action_items = analysis.get("action_items", []) or []
        summary = analysis.get("summary", "")
    else:
        action_items = getattr(analysis, "action_items", []) or []
        summary = getattr(analysis, "summary", "")

    meeting_id = state.get("meeting_id")
    meeting_title = state.get("meeting_title")
    if not meeting_title:
        meeting_title = f"Meeting {meeting_id}" if meeting_id else "Unknown Meeting"

    try:
        results = create_calendar_events_for_action_items(
            action_items=action_items,
            meeting_title=meeting_title
        )
    except Exception as e:
        print(f"[CALENDAR NODE] Unexpected error: {e}")
        results = [{"status": "FAILED", "error": str(e), "message": f"Calendar node exception: {e}"}]

    for r in results:
        print(
            f"[CALENDAR NODE] {r.get('assignee', '?')} | "
            f"{r.get('deadline', 'no deadline')} | Status: {r.get('status')}"
        )

    return {"calendar_events": results}


def sprint_comparison_node(state: AgentState) -> Dict[str, Any]:
    """
    Compare current sprint meeting against previous meeting in sprint_comparison.db.
    Classifies tasks into NEWLY ASSIGNED, COMPLETED, PENDING, IN PROGRESS, CHANGED, NEEDS REVIEW.
    Guarantees failure isolation so downstream actions (reminders, calendar, email) are never blocked.
    """
    try:
        analysis = state.get("analysis")
        if isinstance(analysis, dict):
            raw_action_items = analysis.get("action_items") or []
            summary = analysis.get("summary") or ""
        elif analysis:
            raw_action_items = getattr(analysis, "action_items", []) or []
            summary = getattr(analysis, "summary", "") or ""
        else:
            raw_action_items = []
            summary = ""

        transcript = state.get("transcript") or ""
        norm_transcript = normalize_transcript(transcript)
        meeting_id = state.get("meeting_id")
        if not meeting_id and norm_transcript:
            import hashlib
            meeting_id = hashlib.sha256(norm_transcript.encode("utf-8")).hexdigest()[:12]
        elif not meeting_id:
            import uuid
            meeting_id = f"m_{uuid.uuid4().hex[:12]}"

        meeting_title = state.get("meeting_title")
        if not meeting_title:
            meeting_title = f"Meeting {meeting_id}"

        project_key = state.get("project_key") or os.environ.get("JIRA_PROJECT_KEY") or "SCRUM"
        project_key = project_key.strip().upper()

        import hashlib
        transcript_hash = hashlib.sha256(norm_transcript.encode("utf-8")).hexdigest() if norm_transcript else hashlib.sha256(meeting_id.encode("utf-8")).hexdigest()

        db_path = os.environ.get("SPRINT_COMPARISON_DB") or SPRINT_DB

        # 1. Save meeting in sprint comparison database
        try:
            save_res = save_meeting(
                meeting_id=meeting_id,
                project_key=project_key,
                meeting_title=meeting_title,
                transcript_hash=transcript_hash,
                summary=summary,
                db_path=db_path
            )
            print(f"[SPRINT COMPARISON] Meeting {meeting_id} saved (status: {save_res.get('status')})")
        except Exception as e:
            print(f"[SPRINT COMPARISON] Warning: meeting record save issue: {e}")

        # 2. Correlate newly created Jira issue keys from ticket_result
        created_jira_keys = {}
        ticket_results = state.get("ticket_result") or []
        for res_str in ticket_results:
            issue_key_match = re.search(r"Issue Key:\s*([A-Z0-9]+-\d+)", res_str, re.IGNORECASE)
            title_match = re.search(r"Title:\s*(.+)", res_str, re.IGNORECASE)
            if issue_key_match and title_match:
                k = issue_key_match.group(1).strip().upper()
                t = title_match.group(1).strip().lower()
                created_jira_keys[t] = k

        # 3. Enrich incoming action items with Jira keys if created
        enriched_items = []
        for item in raw_action_items:
            if isinstance(item, dict):
                item_dict = dict(item)
            else:
                item_dict = {
                    "task": getattr(item, "task", ""),
                    "assignee": getattr(item, "assignee", None),
                    "deadline": getattr(item, "deadline", None),
                    "task_size": getattr(item, "task_size", None),
                    "context": getattr(item, "context", None),
                    "progress": getattr(item, "progress", None),
                    "expected_outcome": getattr(item, "expected_outcome", None),
                    "decisions": getattr(item, "decisions", None),
                    "next_steps": getattr(item, "next_steps", None),
                    "blockers": getattr(item, "blockers", None),
                    "jira_issue_key": getattr(item, "jira_issue_key", None) or getattr(item, "jira_key", None),
                    "task_id": getattr(item, "task_id", None),
                    "status": getattr(item, "status", None),
                }

            if not item_dict.get("jira_issue_key"):
                t_norm = (item_dict.get("task") or "").strip().lower()
                for ct_title, ct_key in created_jira_keys.items():
                    if ct_title in t_norm or t_norm in ct_title:
                        item_dict["jira_issue_key"] = ct_key
                        break
            enriched_items.append(item_dict)

        # 4. Invoke Sprint Comparison Engine
        comparison_result = compare_sprint_meetings(
            project_key=project_key,
            current_meeting_id=meeting_id,
            current_action_items=enriched_items,
            current_meeting_title=meeting_title,
            transcript=transcript,
            db_path=db_path
        )

        return {
            "sprint_comparison": comparison_result,
            "meeting_id": meeting_id,
            "meeting_title": meeting_title,
            "project_key": project_key
        }

    except Exception as e:
        print(f"[SPRINT COMPARISON] Error running sprint comparison: {e}")
        p_key = (state.get("project_key") or os.environ.get("JIRA_PROJECT_KEY") or "SCRUM").strip().upper()
        m_id = state.get("meeting_id") or "unknown"
        target_db = os.environ.get("SPRINT_COMPARISON_DB") or SPRINT_DB
        try:
            from sprint_database import save_comparison_run
            save_comparison_run(
                project_key=p_key,
                current_meeting_id=m_id,
                previous_meeting_id=None,
                status="FAILED",
                summary_counts={},
                comparison_data={},
                error_message=str(e),
                db_path=target_db
            )
        except Exception as pe:
            print(f"[SPRINT COMPARISON] Could not record comparison failure: {pe}")
        return {
            "sprint_comparison": {
                "status": "FAILED",
                "error": str(e),
                "project_key": p_key
            }
        }


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

def route_after_jira(state: AgentState) -> str:
    """Route after Jira ticket creation: proceed to sprint comparison in transcript flow, else END."""
    if state.get("transcript") and not state.get("user_query"):
        return "sprint_comparison_node"
    return END

def route_actions(state: AgentState) -> List[str]:
    """Route to all determined actions in parallel/conditional branches."""
    determined = state.get("determined_actions") or []
    # Filter out create_jira_ticket since it was already executed prior to sprint_comparison_node
    filtered = [a for a in determined if a != "create_jira_ticket"]
    if filtered:
        return filtered
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
workflow.add_node("create_jira_ticket", create_jira_ticket)
workflow.add_node("sprint_comparison_node", sprint_comparison_node)
workflow.add_node("determine_actions", determine_actions)
workflow.add_node("send_email_notifications", send_email_notifications)
workflow.add_node("create_github_issue", create_github_issue)
workflow.add_node("create_calendar_events", create_calendar_events)
workflow.add_node("router", router)
workflow.add_node("rag_query", rag_query)

# START transitions
workflow.add_conditional_edges(
    START,
    route_from_start,
    ["extract_meeting", "router", END]
)

# Automated Transcript Pipeline: extract -> index -> create_jira_ticket -> sprint_comparison_node -> determine_actions
workflow.add_edge("extract_meeting", "index_transcript")
workflow.add_edge("index_transcript", "create_jira_ticket")

workflow.add_conditional_edges(
    "create_jira_ticket",
    route_after_jira,
    ["sprint_comparison_node", END]
)

workflow.add_edge("sprint_comparison_node", "determine_actions")

workflow.add_conditional_edges(
    "determine_actions",
    route_actions,
    ["send_email_notifications", "create_github_issue", "create_calendar_events", END]
)

# Join action branches at END
workflow.add_edge("send_email_notifications", END)
workflow.add_edge("create_github_issue", END)
workflow.add_edge("create_calendar_events", END)

# Router transitions (for standalone user queries / backward-compatibility)
workflow.add_conditional_edges(
    "router",
    route_from_router,
    ["rag_query", "create_jira_ticket", "create_github_issue", END]
)
workflow.add_edge("rag_query", END)

graph = workflow.compile()
