from pydantic import BaseModel, Field
from typing import List
from graph import get_llm

class SimpleTask(BaseModel):
    assignee: str
    task: str

class SimpleExtraction(BaseModel):
    items: List[SimpleTask]

transcript = """
Arun: I will complete the authentication API by Friday.
Priya: I will test the authentication API by Monday.
Kashif: I have finished my previous work. I will fix the authentication API by Monday. I will also update the API documentation by Wednesday.
Rahul: Please create a Jira task for fixing the login timeout.
Divya: I will review the database changes by Tuesday.
Sanjay: Please create a GitHub issue for reviewing the authentication code.
Anitha: I will prepare the deployment documentation by Wednesday.
"""

def test():
    llm = get_llm()
    structured = llm.with_structured_output(SimpleExtraction)
    prompt = f"Extract every task. Transcript: {transcript}"
    res = structured.invoke(prompt)
    print(res)

if __name__ == "__main__":
    test()
