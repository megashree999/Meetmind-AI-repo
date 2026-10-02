import os
from graph import graph

def run_test():
    transcript = """Speaker 1: Good morning everyone. We need to complete the login module by Friday.

Speaker 2: I will handle the backend API.

Speaker 3: I will test the login flow.

Speaker 1: Let's use JWT authentication.

Speaker 2: I will create the API ticket today."""

    print("Running graph with sample transcript...\n")
    try:
        # Check API key before invoking to give a clear message
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key or api_key == "your_openai_api_key_here":
            print("ERROR: OPENAI_API_KEY is not set or is still the placeholder in the .env file.")
            print("Please edit the .env file and add your actual OpenAI API key.")
            return

        result = graph.invoke({"transcript": transcript})
        
        analysis = result.get("analysis")
        
        if analysis:
            print("Summary:")
            print(analysis.summary)
            print("\nAction items:")
            for item in analysis.action_items:
                assignee = item.assignee if item.assignee else "Unassigned"
                deadline = f" (Deadline: {item.deadline})" if item.deadline else ""
                print(f"- {assignee} -> {item.task}{deadline}")
            print("\nDecision:")
            for decision in analysis.decisions:
                print(f"- {decision}")
        else:
            print("Error: No analysis found in the state.")
            
    except Exception as e:
        print(f"Graph execution failed: {e}")

if __name__ == "__main__":
    run_test()
