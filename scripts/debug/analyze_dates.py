import sqlite3
import pandas as pd
from pathlib import Path

db_path = "c:/Python/Duke/Standalone/data/db/duke_rates.db"

with sqlite3.connect(db_path) as conn:
    # Query to get the effective start dates for NC Progress families
    query = """
    SELECT 
        f.family_key,
        f.title,
        f.family_type,
        v.effective_start,
        v.revision_label
    FROM tariff_families f
    JOIN tariff_versions v ON f.family_key = v.family_key
    WHERE f.state = 'NC' AND f.company = 'progress'
    ORDER BY f.family_key, v.effective_start
    """
    
    df = pd.read_sql_query(query, conn)

if df.empty:
    print("No data found for NC Progress.")
else:
    # Convert effective_start to datetime for analysis
    df['effective_start'] = pd.to_datetime(df['effective_start'], errors='coerce')
    
    print("=== OVERALL TIME PERIOD SUMMARY (NC Progress) ===")
    print(f"Total tariff versions parsed: {len(df)}")
    print(f"Earliest effective date: {df['effective_start'].min().date()}")
    print(f"Latest effective date: {df['effective_start'].max().date()}")
    print(f"Number of versions missing effective dates: {df['effective_start'].isna().sum()}")
    print("\n")
    
    # Group by year
    df['year'] = df['effective_start'].dt.year
    print("=== VERSIONS BY YEAR ===")
    year_counts = df['year'].value_counts().sort_index()
    for year, count in year_counts.items():
        print(f"  {int(year)}: {count} documents")
        
    print("\n")
    print("=== BREAKDOWN BY TARIFF TYPE ===")
    type_summary = df.groupby('family_type')['effective_start'].agg(['min', 'max', 'count'])
    for idx, row in type_summary.iterrows():
        min_date = row['min'].date() if pd.notna(row['min']) else "Unknown"
        max_date = row['max'].date() if pd.notna(row['max']) else "Unknown"
        print(f"  {idx.upper()}: {int(row['count'])} versions (Range: {min_date} to {max_date})")

    # Let's list a few key schedules to show their specific ranges
    print("\n=== KEY SCHEDULES DATES ===")
    key_schedules = ['nc-progress-leaf-500', 'nc-progress-leaf-501', 'nc-progress-leaf-504'] # RES, R-TOUD, R-TOU
    for ks in key_schedules:
        sched_df = df[df['family_key'] == ks]
        if not sched_df.empty:
            title = sched_df.iloc[0]['title']
            dates = sched_df['effective_start'].dt.date.dropna().tolist()
            dates_str = ", ".join(str(d) for d in dates) if dates else "Unknown"
            print(f"  {ks} ({title}): {dates_str}")
