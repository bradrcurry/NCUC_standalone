import sqlite3

def analyze_db(db_path):
    print(f"\n--- Analyzing {db_path} ---")
    try:
        conn = sqlite3.connect(db_path)
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        for (table,) in tables:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"Table '{table}': {count} rows")
        conn.close()
    except Exception as e:
        print(f"Error reading {db_path}: {e}")

analyze_db("duke_rates.db")
analyze_db("data/db/duke_rates.db")
