from graph import rag_query

def test_participant_count():
    transcript = """
Sprint Planning Meeting

Megashree: I will review the project architecture and provide feedback by Friday.

Kashif: I will investigate the authentication service issue and provide a report by Monday.

Rahul: I will investigate the login API error and fix the issue by Tuesday.

Priya: I will test the authentication changes and report any failures by Wednesday.

Megashree: Please create a Jira task for improving the deployment process.

Kashif: Please create a GitHub issue for reviewing the authentication code.
"""

    print("==========================================")
    print("TEST 1: how many people in this meeting?")
    print("==========================================")
    state1 = {
        "user_query": "how many people in this meeting?",
        "transcript": transcript,
        "meeting_id": "test1234"
    }
    res1 = rag_query(state1)
    print(res1["answer"])
    
    print("\n==========================================")
    print("TEST 2: what did Rahul say in this meeting?")
    print("==========================================")
    state2 = {
        "user_query": "what did Rahul say in this meeting?",
        "transcript": transcript,
        "meeting_id": "test1234"
    }
    res2 = rag_query(state2)
    print(res2["answer"])

if __name__ == "__main__":
    test_participant_count()
