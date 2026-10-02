import os
import sys
from dotenv import load_dotenv

# Ensure fresh environment variables
load_dotenv(override=True)

from github_service import verify_github_connection, get_github_credentials

def main():
    print("=" * 60)
    print("GITHUB CONNECTION & REPOSITORY ACCESS VERIFICATION")
    print("=" * 60)

    token, owner, repo = get_github_credentials()

    print(f"GITHUB_OWNER : {owner or 'NOT SET'}")
    print(f"GITHUB_REPO  : {repo or 'NOT SET'}")
    print(f"GITHUB_TOKEN : {'[CONFIGURED]' if token else 'NOT SET'}")
    print("-" * 60)

    result = verify_github_connection()

    auth_status = result.get("auth_status", "FAILED")
    repo_status = result.get("repo_status", "FAILED")
    owner_repo = f"{result.get('owner')}/{result.get('repo')}"
    error_msg = result.get("error")
    auth_user = result.get("authenticated_user")

    print(f"GitHub authentication status : {auth_status}")
    if auth_user:
        print(f"Authenticated user           : {auth_user}")
    print(f"Repository access status     : {repo_status}")
    print(f"Owner/Repository             : {owner_repo}")
    if error_msg:
        print(f"Error                        : {error_msg}")
    print("=" * 60)

    if auth_status == "SUCCESS" and repo_status == "SUCCESS":
        print("VERIFICATION RESULT: SUCCESS - Ready for GitHub issue creation")
        sys.exit(0)
    else:
        print("VERIFICATION RESULT: FAILED - Connection or access check failed")
        sys.exit(1)

if __name__ == "__main__":
    main()
