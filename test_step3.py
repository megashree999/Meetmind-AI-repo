import os
from unittest.mock import patch, MagicMock
from graph import graph, MeetingAnalysis, ActionItem, RouterOutput

transcript = """Speaker 1: Good morning everyone. We need to complete the login module by Friday.
Speaker 2: I will handle the backend API.
Speaker 3: I will test the login flow.
Speaker 1: Let's use JWT authentication.
Speaker 2: I will create the API ticket today."""

def run_tests():
    print("====================================")
    print("TESTING MEETMIND AI - STEP 3/4/5")
    print("====================================\n")
    
    api_key = os.environ.get("OPENAI_API_KEY")
    is_mock = not api_key or api_key == "your_openai_api_key_here"
    
    print(f"USING {'MOCK' if is_mock else 'REAL'} OPENAI API\n")

    if is_mock:
        # We need to bypass the API key checks and mock the LLM/Chroma calls.
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake_key", "LANGCHAIN_TRACING_V2": "false"}), \
             patch("graph.ChatOpenAI") as MockChat, \
             patch("graph.OpenAIEmbeddings"), \
             patch("graph.Chroma") as MockChroma:
             
             # Setup Mock LLM for structured output (extract & router)
             mock_llm_instance = MagicMock()
             MockChat.return_value = mock_llm_instance
             
             def mock_structured_invoke(prompt):
                 if "summary, action items, and decisions" in prompt:
                     return MeetingAnalysis(
                         summary="The team discussed completing the login module by Friday and agreed to use JWT authentication.",
                         action_items=[
                             ActionItem(task="Handle the backend API", assignee="Speaker 2", deadline="Friday"),
                             ActionItem(task="Test the login flow", assignee="Speaker 3", deadline="Friday"),
                             ActionItem(task="Create the API ticket today", assignee="Speaker 2", deadline="Today")
                         ],
                         decisions=["Use JWT authentication"]
                     )
                 elif "You are a router" in prompt:
                     routes = []
                     if "authentication" in prompt.lower() or "who" in prompt.lower() or "tasks" in prompt.lower():
                         routes.append("RAG_QUERY")
                     if "ticket" in prompt.lower() and "jira" in prompt.lower():
                         routes.append("CREATE_TICKET_JIRA")
                     if "ticket" in prompt.lower() and "github" in prompt.lower():
                         routes.append("CREATE_TICKET_GITHUB")
                     if "tell me who" in prompt.lower() and "create a ticket" in prompt.lower():
                         routes = ["RAG_QUERY", "CREATE_TICKET_JIRA"]
                     return RouterOutput(
                         routes=routes, 
                         ticket_title="Implement backend API for login module", 
                         ticket_description="Handle the backend API for the login module discussed in the meeting."
                     )
             
             mock_structured_llm = MagicMock()
             mock_structured_llm.invoke = mock_structured_invoke
             mock_llm_instance.with_structured_output.return_value = mock_structured_llm
             
             # Setup Mock LLM for direct text output (RAG)
             def mock_invoke(prompt):
                 ans = MagicMock()
                 if "authentication" in prompt:
                     ans.content = "JWT authentication."
                 elif "who" in prompt:
                     ans.content = "Speaker 2."
                 elif "tasks" in prompt:
                     ans.content = "- Handle the backend API.\n- Create the API ticket today."
                 else:
                     ans.content = "Mocked answer."
                 return ans
             mock_llm_instance.invoke = mock_invoke
             
             # Setup Mock Chroma
             mock_chroma_instance = MagicMock()
             MockChroma.return_value = mock_chroma_instance
             
             mock_retriever = MagicMock()
             doc1 = MagicMock()
             doc1.page_content = "Mock Context for test."
             mock_retriever.invoke.return_value = [doc1]
             mock_chroma_instance.as_retriever.return_value = mock_retriever

             execute_test_cases()
    else:
        execute_test_cases()

def execute_test_cases():
    print("1. Testing Extraction & Indexing...")
    state1 = {"transcript": transcript}
    res1 = graph.invoke(state1)
    print("  -> Extraction complete. Summary:", res1['analysis'].summary)
    
    print("\n2. Testing RAG (Query: What did we decide about authentication?)")
    state2 = {"transcript": transcript, "analysis": res1['analysis'], "user_query": "What did we decide about authentication?"}
    res2 = graph.invoke(state2)
    print("  -> Route taken:", res2['router_output'].routes)
    print("  -> Answer:", res2.get('answer'))

    print("\n3. Testing RAG (Query: Who is responsible for the backend API?)")
    state3 = {"transcript": transcript, "analysis": res1['analysis'], "user_query": "Who is responsible for the backend API?"}
    res3 = graph.invoke(state3)
    print("  -> Route taken:", res3['router_output'].routes)
    print("  -> Answer:", res3.get('answer'))

    print("\n4. Testing RAG (Query: What tasks were assigned to Speaker 2?)")
    state4 = {"transcript": transcript, "analysis": res1['analysis'], "user_query": "What tasks were assigned to Speaker 2?"}
    res4 = graph.invoke(state4)
    print("  -> Route taken:", res4['router_output'].routes)
    print("  -> Answer:", res4.get('answer'))
    
    print("\n5. Testing Jira Mock Routing (Query: Create a Jira ticket for the backend API.)")
    state5 = {"transcript": transcript, "analysis": res1['analysis'], "user_query": "Create a Jira ticket for the backend API."}
    res5 = graph.invoke(state5)
    print("  -> Route taken:", res5['router_output'].routes)
    print("  -> Ticket Result:")
    for t in res5.get('ticket_result', []):
        print("      " + t.replace("\n", "\n      "))

    print("\n6. Testing BOTH (Query: Tell me who handles the backend API and create a ticket for it in Jira.)")
    state6 = {"transcript": transcript, "analysis": res1['analysis'], "user_query": "Tell me who handles the backend API and create a ticket for it in Jira."}
    res6 = graph.invoke(state6)
    print("  -> Route taken:", res6['router_output'].routes)
    print("  -> Answer:", res6.get('answer'))
    print("  -> Ticket Result:")
    for t in res6.get('ticket_result', []):
        print("      " + t.replace("\n", "\n      "))

if __name__ == "__main__":
    run_tests()
