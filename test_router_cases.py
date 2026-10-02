import os
import sys

# Ensure the project directory is on sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)

from graph import graph, MeetingAnalysis, ActionItem

def test_routing():
    print("=" * 60)
    print("MEETMIND AI ROUTER VERIFICATION")
    print("=" * 60)

    sample_analysis = MeetingAnalysis(
        summary="The team met to discuss completing the login module by Friday using JWT authentication.",
        action_items=[
            ActionItem(task="Handle backend API", assignee="Speaker 2", deadline="Friday"),
            ActionItem(task="Test login flow", assignee="Speaker 3", deadline="Friday"),
            ActionItem(task="Create API ticket", assignee="Speaker 2", deadline="Today")
        ],
        decisions=["Use JWT authentication"]
    )

    test_cases = [
        {
            "category": "1. Information / Knowledge Question",
            "query": "What did we decide about authentication?",
            "expected_node": "rag_query"
        },
        {
            "category": "2. Jira Request",
            "query": "Create a Jira ticket for the backend API.",
            "expected_node": "create_jira_ticket"
        },
        {
            "category": "3. GitHub Request",
            "query": "Create a GitHub issue for testing the login flow.",
            "expected_node": "create_github_issue"
        }
    ]

    all_passed = True

    for tc in test_cases:
        print(f"\n--- Testing Category: {tc['category']} ---")
        print(f"Query: \"{tc['query']}\"")
        print(f"Expected Route: router -> {tc['expected_node']} ONLY")

        initial_state = {
            "transcript": "Speaker 1: Let's use JWT authentication.\nSpeaker 2: I will handle the backend API.",
            "analysis": sample_analysis,
            "user_query": tc["query"],
            "ticket_result": []
        }

        executed_nodes = []
        for step in graph.stream(initial_state):
            for node_name in step.keys():
                if node_name not in executed_nodes:
                    executed_nodes.append(node_name)

        print(f"Executed Nodes: {executed_nodes}")
        
        # After router, there should be exactly one downstream node matching expected_node
        downstream = [n for n in executed_nodes if n != "router"]
        
        if downstream == [tc["expected_node"]]:
            print(f"Result: PASS (Only '{tc['expected_node']}' executed)")
        else:
            print(f"Result: FAIL (Expected ['{tc['expected_node']}'], but got {downstream})")
            all_passed = False

    print("\n" + "=" * 60)
    if all_passed:
        print("ALL 3 ROUTING CASES PASSED SUCCESSFULLY!")
    else:
        print("SOME CASES FAILED.")
    print("=" * 60)
    return all_passed

if __name__ == "__main__":
    success = test_routing()
    sys.exit(0 if success else 1)
