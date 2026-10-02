import os
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(env_path, override=True)

jira_base_url = (os.environ.get("JIRA_BASE_URL") or os.environ.get("JIRA_URL") or "").rstrip("/")
jira_email = (os.environ.get("JIRA_EMAIL") or "").strip()
jira_token = (os.environ.get("JIRA_API_TOKEN") or "").strip()
project_key = (os.environ.get("JIRA_PROJECT_KEY") or "SCRUM").strip()

print(f"Jira Base URL : {jira_base_url}")
print(f"Jira Email    : {jira_email}")
print(f"Project Key   : {project_key}")
print(f"Token Length  : {len(jira_token) if jira_token else 0}")

auth = HTTPBasicAuth(jira_email, jira_token)
headers = {"Accept": "application/json"}

print("\n--- Testing Authentication (/rest/api/3/myself) ---")
r = requests.get(f"{jira_base_url}/rest/api/3/myself", headers=headers, auth=auth)
print(f"Status: {r.status_code}")
if r.status_code == 200:
    print(f"Authenticated as: {r.json().get('displayName')}")
else:
    print(r.text)

print("\n--- Testing Project List ---")
r = requests.get(f"{jira_base_url}/rest/api/3/project", headers=headers, auth=auth)
print(f"Status: {r.status_code}")
if r.status_code == 200:
    projects = r.json()
    print(f"Found {len(projects)} projects:")
    for p in projects:
        print(f" - {p.get('key')} : {p.get('name')}")
else:
    print(r.text)

print("\n--- Testing Issue Create with SCRUM ---")
payload = {
    "fields": {
        "project": {"key": "SCRUM"},
        "summary": "Test issue",
        "description": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Test"}]}]},
        "issuetype": {"name": "Task"}
    }
}
r = requests.post(f"{jira_base_url}/rest/api/3/issue", headers={"Accept": "application/json", "Content-Type": "application/json"}, json=payload, auth=auth)
print(f"Status: {r.status_code}")
print(r.text)
