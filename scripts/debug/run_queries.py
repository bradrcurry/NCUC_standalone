import sqlite3
import os

db_path = 'data/db/duke_rates.db'

if not os.path.exists(db_path):
    print(f"Error: Database not found at {db_path}")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

print("--- Charge type distribution by state ---")
cursor.execute('''
SELECT tf.state, tc.charge_type, COUNT(*) 
FROM tariff_charges tc 
JOIN tariff_versions tv ON tc.version_id = tv.id 
JOIN tariff_families tf ON tv.family_key = tf.family_key 
GROUP BY tf.state, tc.charge_type ORDER BY tf.state, tc.charge_type;
''')
for row in cursor.fetchall():
    print(f"State: {row[0]}, Charge Type: {row[1]}, Count: {row[2]}")
print("\n")

print("--- Families with zero charges ---")
cursor.execute('''
SELECT tf.family_key, tf.title, tf.state, tf.family_type
FROM tariff_families tf
JOIN tariff_versions tv ON tv.family_key = tf.family_key
LEFT JOIN tariff_charges tc ON tc.version_id = tv.id
WHERE tc.id IS NULL
ORDER BY tf.state, tf.family_key;
''')
for row in cursor.fetchall():
    print(f"Family Key: {row[0]}, Title: {row[1]}, State: {row[2]}, Type: {row[3]}")
print("\n")

print("--- Rider applicability coverage ---")
cursor.execute('''
SELECT tf.state, COUNT(DISTINCT ra.applies_to_family_key) as schedules_with_riders
FROM rider_applicability ra
JOIN tariff_families tf ON ra.applies_to_family_key = tf.family_key
GROUP BY tf.state;
''')
for row in cursor.fetchall():
    print(f"State: {row[0]}, Schedules with Riders: {row[1]}")
print("\n")

conn.close()
