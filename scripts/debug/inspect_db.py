import sqlite3
for r in sqlite3.connect('c:/Python/Duke/Standalone/data/db/duke_rates.db').execute("SELECT charge_type, rate_value, rate_unit FROM tariff_charges WHERE family_key = 'nc-carolinas-schedule-RS' LIMIT 5"):
    print(r)
