import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "company_directory.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Create employees table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS employees (
            employee_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            email TEXT NOT NULL,
            role TEXT,
            department TEXT
        )
    ''')

    # Seed initial data based on team_members.json
    initial_employees = [
        ("Megashree", "megashree999@gmail.com", "Team Lead", "Engineering"),
        ("Kashif", "kashifahamad2003@gmail.com", "Developer", "Engineering"),
        ("Ali", "amohammedali2005@gmail.com", "Developer", "Engineering")
    ]

    for name, email, role, department in initial_employees:
        cursor.execute('''
            INSERT INTO employees (name, email, role, department)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                email=excluded.email,
                role=excluded.role,
                department=excluded.department
        ''', (name, email, role, department))

    conn.commit()
    conn.close()
    print("Database initialized and seeded successfully.")

if __name__ == "__main__":
    init_db()
