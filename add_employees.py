import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "company_directory.db")

def add_new_employees():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Get current count
    cursor.execute("SELECT COUNT(*) FROM employees")
    before_count = cursor.fetchone()[0]
    print(f"Existing employee count before: {before_count}")

    new_employees = [
        ("Arun", "arun@company.com", "Backend Developer", "Engineering"),
        ("Priya", "priya@company.com", "Frontend Developer", "Engineering"),
        ("Rahul", "rahul@company.com", "QA Engineer", "Testing"),
        ("Divya", "divya@company.com", "Data Analyst", "Data"),
        ("Sanjay", "sanjay@company.com", "DevOps Engineer", "Cloud"),
        ("Anitha", "anitha@company.com", "Project Manager", "Management")
    ]

    for name, email, role, department in new_employees:
        cursor.execute('''
            INSERT INTO employees (name, email, role, department)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                email=excluded.email,
                role=excluded.role,
                department=excluded.department
        ''', (name, email, role, department))

    conn.commit()

    # Get new count
    cursor.execute("SELECT COUNT(*) FROM employees")
    after_count = cursor.fetchone()[0]
    print(f"Final employee count: {after_count}")
    
    print("\nFinal employees table:")
    print("ID | Name | Email | Role | Department")
    print("-" * 80)
    cursor.execute("SELECT employee_id, name, email, role, department FROM employees")
    for row in cursor.fetchall():
        print(" | ".join(str(item) for item in row))

    conn.close()

if __name__ == "__main__":
    add_new_employees()
