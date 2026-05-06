import sqlite3
import os

db_path = 'c:/Python/Duke/Standalone/duke_rates.db'

conn = sqlite3.connect(db_path)
res = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
for table in res:
    print(table[0])
