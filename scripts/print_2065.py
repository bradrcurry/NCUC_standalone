import sqlite3
import os

conn = sqlite3.connect('data/db/duke_rates.db')
row = conn.execute('SELECT raw_text_path FROM historical_documents WHERE id=2065').fetchone()
if row:
    with open(row[0], encoding='utf-8') as f:
        print(f.read()[:500])
