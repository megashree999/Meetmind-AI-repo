import os
import shutil
import warnings
warnings.filterwarnings("ignore")

from graph import index_transcript, rag_query, get_vector_store

def test_rag_isolation():
    # Setup: clean chromadb for predictable testing
    if os.path.exists("./chroma_db"):
        shutil.rmtree("./chroma_db")
        
    print("==========================================")
    print("TEST 2 Setup: Indexing OLD meeting into ChromaDB")
    print("==========================================")
    
    old_transcript = """
    Sprint Planning Meeting (OLD)
    Megashree: Welcome to the old meeting.
    Kashif: I am here.
    Arun: Backend update.
    Priya: Frontend update.
    Rahul: QA update.
    Divya: Data update.
    Sanjay: DevOps update.
    Anitha: Project manager update.
    """
    state_old = {"transcript": old_transcript, "user_query": ""}
    res_old = index_transcript(state_old)
    print(f"Old meeting indexed with ID: {res_old.get('meeting_id')}")
    
    print("\n==========================================")
    print("TEST 1 & 2 Setup: Indexing CURRENT meeting")
    print("==========================================")
    
    current_transcript = """
    Sprint Planning Meeting
    Megashree: I will review the project architecture and provide feedback by Friday.
    Kashif: I will investigate the authentication service issue and provide a report by Monday.
    Rahul: I will investigate the login API error and fix the issue by Tuesday.
    Priya: I will test the authentication changes and report any failures by Wednesday.
    Megashree: Please create a Jira task for improving the deployment process.
    Kashif: Please create a GitHub issue for reviewing the authentication code.
    """
    state_current = {"transcript": current_transcript, "user_query": ""}
    res_current = index_transcript(state_current)
    current_meeting_id = res_current.get("meeting_id")
    print(f"Current meeting indexed with ID: {current_meeting_id}")
    
    print("\n==========================================")
    print("TEST 1 & 2: how many people in this meeting")
    print("==========================================")
    state_q1 = {
        "transcript": current_transcript, 
        "user_query": "how many people in this meeting? list their names.",
        "meeting_id": current_meeting_id
    }
    ans1 = rag_query(state_q1)
    print(f"Answer:\n{ans1['answer']}")
    
    print("\n==========================================")
    print("TEST 3: what did Rahul say in this meeting?")
    print("==========================================")
    state_q2 = {
        "transcript": current_transcript, 
        "user_query": "what did Rahul say in this meeting?",
        "meeting_id": current_meeting_id
    }
    ans2 = rag_query(state_q2)
    print(f"Answer:\n{ans2['answer']}")
    
    print("\n==========================================")
    print("TEST 4: Historical Query")
    print("==========================================")
    state_q3 = {
        "transcript": current_transcript, 
        "user_query": "Did Sanjay attend any sprint planning meetings?",
        "meeting_id": current_meeting_id
    }
    ans3 = rag_query(state_q3)
    print(f"Answer:\n{ans3['answer']}")

if __name__ == "__main__":
    test_rag_isolation()
