import sys
import os

from team_directory import get_assignee_email, load_team_members

def run_tests():
    print("=" * 60)
    print("TESTING TEAM MEMBER DIRECTORY LOOKUP")
    print("=" * 60)

    # Verify team directory loading
    members = load_team_members()
    print(f"Loaded {len(members)} team members from directory: {list(members.keys())}\n")

    test_cases = [
        ("Megashree", "megashree999@gmail.com"),
        ("Kashif", "kashifahamad2003@gmail.com"),
        ("Ali", "amohammedali2005@gmail.com"),
        ("Unknown person", "email not found"),
        ("John Doe", "email not found"),
        ("megashree", "megashree999@gmail.com"),  # case-insensitive check
        ("kashif", "kashifahamad2003@gmail.com"),     # case-insensitive check
        ("ali", "amohammedali2005@gmail.com"),        # case-insensitive check
        ("", "email not found"),                     # empty string check
        (None, "email not found")                    # None check
    ]

    all_passed = True

    for name, expected in test_cases:
        result = get_assignee_email(name)
        status = "PASS" if result == expected else "FAIL"
        if status == "FAIL":
            all_passed = False
        print(f"[{status}] Assignee: '{name}' -> Email: '{result}' (Expected: '{expected}')")

    print("\n" + "=" * 60)
    if all_passed:
        print("ALL TESTS PASSED SUCCESSFULLY!")
    else:
        print("SOME TESTS FAILED.")
    print("=" * 60)

    return all_passed

if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
