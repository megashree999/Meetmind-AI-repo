from graph import extract_meeting, AgentState
import json

transcript = """Kashif: We need to complete the user authentication module using JWT. I'll finish the backend authentication API by Friday.

Ali: I'll handle the database tables and migration for the authentication module. I'll complete that by Thursday.

Megashree: I'll work on the frontend login dashboard and integrate it with the backend API. I'll finish the integration by Monday.

Kashif: We also need to make sure the authentication API is properly documented.

Megashree: Agreed. Please create a Jira ticket for the authentication module completion and include the implementation details.

Ali: We should also create a GitHub issue for testing the authentication API and database integration.

Kashif: Agreed. The team should test the complete login flow after the backend and database work is finished."""

state: AgentState = {
    "transcript": transcript,
    "retrieved_context": "Sprint Planning Meeting (OLD) - Priya will test the authentication changes. Rahul will investigate the login API error. The project architecture will be reviewed."
}

# The retrieved_context is injected into the state, just like what happens in a stale LangGraph Studio thread.
result = extract_meeting(state)

analysis = result["analysis"]
print("--- SUMMARY ---")
print(analysis.summary)
