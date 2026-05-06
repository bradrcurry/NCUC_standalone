import pathlib

p = pathlib.Path('c:/Python/Duke/Standalone/src/duke_rates/billing/tariff_engine.py')
text = p.read_text(encoding='utf-8')

text = text.replace('rate = (c.rate_value or 0.0) / 100.0  # cents/kWh → $/kWh', 'rate = (c.rate_value or 0.0)')
text = text.replace('rate = (c.rate_value or 0.0) / 100.0', 'rate = (c.rate_value or 0.0)')
text = text.replace('rate_unit=c.rate_unit or "cents/kWh"', 'rate_unit=c.rate_unit or "$/kWh"')
text = text.replace('amount = round(usage.monthly_kwh * avg_rate / 100.0, 2)', 'amount = round(usage.monthly_kwh * avg_rate, 2)')
text = text.replace('rate_unit="cents/kWh",', 'rate_unit="$/kWh",')
p.write_text(text, encoding='utf-8')
