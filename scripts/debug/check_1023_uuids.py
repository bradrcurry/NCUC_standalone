import sqlite3
db_path = 'c:/Python/Duke/Standalone/data/db/duke_rates.db'
conn = sqlite3.connect(db_path)
uuids = [
    '04efc351-c973-4618-8f57-a1b7a2c71f33',
    '05f7300f-ed44-46d9-a20f-12dcf1ad470d',
    '966bf93a-dcd1-4412-96ae-d74b617fb7e2',
    'e36b8e24-5117-48e3-898e-1a35e3e5d7d7',
    'fb10c3f0-8c58-479f-aae5-e40f596e2123'
]
print("\n--- Discovery Records for Unregistered 1023 UUIDs ---")
for uid in uuids:
    for r in conn.execute("SELECT id, filing_title, filing_date FROM ncuc_discovery_records WHERE filing_title LIKE ?", (f'%{uid}%',)):
        print(r)
