import os
import shutil
import warnings
warnings.filterwarnings("ignore")

from graph import index_transcript, rag_query

def test_new_rag_isolation():
    # Setup: clean chromadb for predictable testing
    if os.path.exists("./chroma_db"):
        shutil.rmtree("./chroma_db")
        
    print("==========================================")
    print("Setup: Indexing OLD meeting into ChromaDB")
    print("==========================================")
    
    old_transcript = """
    Sprint Planning Meeting (OLD)
    Megashree: Welcome to the old meeting.
    Kashif: I am here.
    Ali: I am here.
    Arun: Backend update.
    Priya: Frontend update.
    Rahul: QA update.
    Divya: Data update.
    Sanjay: DevOps update.
    """
    state_old = {"transcript": old_transcript, "user_query": ""}
    # Simulate a run that generated an old meeting ID
    res_old = index_transcript(state_old)
    old_meeting_id = res_old.get('meeting_id')
    print(f"Old meeting indexed with ID: {old_meeting_id}")
    
    print("\n==========================================")
    print("Setup: Indexing NEW meeting")
    print("==========================================")
    
    new_transcript = """
    Arun: I will fix the API.
    Priya: I will test the API.
    Rahul: I will deploy the changes.
    """
    
    # Simulating STALE LangGraph Studio State (where the old meeting_id is still passed in the state)
    state_current = {"transcript": new_transcript, "user_query": "", "meeting_id": old_meeting_id}
    res_current = index_transcript(state_current)
    current_meeting_id = res_current.get("meeting_id")
    print(f"New meeting indexed with ID: {current_meeting_id}")
    print(f"Is meeting ID correctly overridden? {current_meeting_id != old_meeting_id}")
    
    print("\n==========================================")
    print("TEST 1: how many members are in the team?")
    print("==========================================")
    # Stale state simulation in rag_query
    state_q1 = {
        "transcript": new_transcript, 
        "user_query": "how many members are in the team?",
        "meeting_id": old_meeting_id
    }
    ans1 = rag_query(state_q1)
    print(f"Answer:\n{ans1['answer']}")
    
    print("\n==========================================")
    print("TEST 2: who are the members in this meeting?")
    print("==========================================")
    state_q2 = {
        "transcript": new_transcript, 
        "user_query": "who are the members in this meeting?",
        "meeting_id": old_meeting_id
    }
    ans2 = rag_query(state_q2)
    print(f"Answer:\n{ans2['answer']}")
    
    print("\n==========================================")
    print("TEST 3: Proving ChromaDB is isolated")
    print("==========================================")
    state_q3 = {
        "transcript": new_transcript, 
        "user_query": "what did Megashree say?",
        "meeting_id": old_meeting_id
    }
    ans3 = rag_query(state_q3)
    print(f"Answer:\n{ans3['answer']}")

if __name__ == "__main__":
    test_new_rag_isolation()
