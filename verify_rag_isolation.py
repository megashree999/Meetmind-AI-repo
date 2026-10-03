import os
from dotenv import load_dotenv
load_dotenv(override=True)

from graph import workflow

TEST_TRANSCRIPT = """Payment System Sprint Meeting

Kashif: We need to complete the payment gateway integration. I will implement the backend payment API and finish it by Friday.

Ali: I will update the database schema required for storing payment transactions. I will complete the migration by Thursday.

Megashree: I will integrate the payment API with the frontend checkout page. I will finish the integration by Monday.

Kashif: I will also prepare the API documentation for the payment endpoints.

Priya: I will test the complete payment flow and report any failures by Wednesday.

Ali: Please create a GitHub issue for testing the transaction database changes.

Megashree: We should also create a Jira ticket for tracking the payment gateway integration.

Kashif: After the integration is completed, the team should perform an end-to-end payment test."""

from langgraph.checkpoint.memory import MemorySaver

def main():
    memory = MemorySaver()
    app = workflow.compile(checkpointer=memory)
    
    # We use ONE thread to simulate LangGraph Studio
    config = {"configurable": {"thread_id": "test_rag_isolation_thread"}}
    
    queries = [
        "Summarize this meeting",
        "What are the roles or tasks of the people involved?",
        "What did Kashif say?",
        "What did Ali say?"
    ]
    
    for idx, query in enumerate(queries, 1):
        print(f"\n{'='*50}\nQuery {idx}: {query}\n{'='*50}")
        
        # In LangGraph studio, they append the state. We simulate this by passing the state.
        # But wait, index_transcript only needs to run once, but RAG query runs directly.
        # Since we use router, we can just pass transcript and user_query.
        state_input = {
            "transcript": TEST_TRANSCRIPT,
            "user_query": query
        }
        
        result = app.invoke(state_input, config=config)
        
        # Get retrieved_context and answer
        retrieved_context = result.get("retrieved_context", "None")
        answer = result.get("answer", "None")
        
        print("\n--- RETRIEVED CONTEXT ---")
        if "Sprint Planning Meeting" in retrieved_context or "Rahul" in retrieved_context:
            print("ERROR: OLD CONTEXT FOUND!")
        else:
            print("OK - No old context detected.")
            
        print("\n--- ANSWER ---")
        print(answer)

if __name__ == "__main__":
    main()
