import sqlite3
import os

db_path = 'c:/Python/Duke/Standalone/data/db/duke_rates.db'

conn = sqlite3.connect(db_path)
print(f"Searching {db_path}...")

# Search discovery records
print("\n--- Discovery Records ---")
try:
    for r in conn.execute("SELECT id, docket_number, docket_id, title FROM ncuc_discovery_records WHERE docket_number LIKE '%1023%' OR title LIKE '%1023%'"):
        print(r)
except sqlite3.OperationalError as e:
    print(f"ncuc_discovery_records error: {e}")

# Search historical documents
print("\n--- Historical Documents ---")
try:
    for r in conn.execute("SELECT id, family_key, docket_number, docket_id, title FROM historical_documents WHERE docket_number LIKE '%1023%' OR title LIKE '%1023%'"):
        print(r)
except sqlite3.OperationalError as e:
    print(f"historical_documents error: {e}")
