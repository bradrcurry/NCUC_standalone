import re, pathlib

def fix_file(filepath):
    path = pathlib.Path(filepath)
    content = path.read_text(encoding='utf-8')
    
    # Simple replace for rate_unit
    content = content.replace('rate_unit="cents/kWh",', 'rate_unit="$/kWh",')
    content = content.replace("rate_unit='cents/kWh',", "rate_unit='$/kWh',")
    content = content.replace('rate_unit = "cents/kWh"', 'rate_unit = "$/kWh"')
    content = content.replace("rate_unit = 'cents/kWh'", "rate_unit = '$/kWh'")
    content = content.replace('rate_unit = "$/kWh" if header_dollar else "cents/kWh"', 'rate_unit = "$/kWh"')
    
    # Find rate_value=X where X is anything NOT ending in comma, and divide by 100 IF followed by rate_unit="$/kWh"
    # Actually, let's just use re.sub for the exact Pydantic instantiations
    
    # Replace rate_value=rt, \n rate_unit="$/kWh"
    # -> rate_value=round(rt / 100.0, 5), \n rate_unit="$/kWh"
    def repl(m):
        val = m.group(1)
        # Avoid double modifying if we run this twice
        if '/ 100.0' in val:
            return m.group(0)
        return f'rate_value=round(({val}) / 100.0, 6),\n{m.group(2)}rate_unit="$/kWh",'

    content = re.sub(r'rate_value=(.*?),\n(\s*)rate_unit="\$/kWh",', repl, content)
    
    # Handle _add('all', rate_val, 'cents/kWh', snippet)
    content = content.replace('cents/kWh', '$/kWh') # Replaced in string literals! Careful...
    # Revert the comment in nc_progress "X.XXXcents/kWh"
    content = content.replace('X.XXX$/kWh', 'X.XXXcents/kWh')
    
    # We must divide the rate_val when passed to _add in nc_progress
    # _add("all", rate_val, "$/kWh", snippet)  -> _add("all", round(rate_val / 100.0, 6), "$/kWh", snippet)
    def add_repl(m):
        cls = m.group(1)
        val = m.group(2)
        if '/ 100.0' in val:
            return m.group(0)
        return f'_add({cls}, round(({val}) / 100.0, 6), "$/kWh",'
        
    content = re.sub(r'_add\(([^,]+),\s*([^,]+),\s*"\$/kWh",', add_repl, content)

    path.write_text(content, encoding='utf-8')

import glob
files = [
    'c:/Python/Duke/Standalone/src/duke_rates/parse/nc_progress.py',
    'c:/Python/Duke/Standalone/src/duke_rates/parse/nc_carolinas.py',
    'c:/Python/Duke/Standalone/src/duke_rates/parse/fl_florida.py',
]
for f in files:
    fix_file(f)
