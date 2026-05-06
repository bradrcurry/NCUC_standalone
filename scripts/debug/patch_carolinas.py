import re

f = 'src/duke_rates/parse/nc_carolinas.py'
with open(f, 'rb') as fh:
    raw = fh.read()

src = raw.decode('utf-8')

# Find the docstring end and insert the guard before "# --- Version record ---"
old_marker = '    # --- Version record ---\n    version = TariffVersionRecord('
new_marker = '''    # Guard: multi-schedule PDFs contain Schedule OL (Outdoor Lighting Service) after
    # the target schedule rate table. For non-OL families, truncate text at the
    # OL section header to prevent phantom Rider Adjustment rows (-1.0/-8.0 $/kWh).
    _fk_lower = family_key.lower()
    if "outdoor" not in _fk_lower and "-ol" not in _fk_lower and "lighting" not in _fk_lower:
        _ol_m = re.search(r"(?m)^OUTDOOR LIGHTING SERVICE", text, re.I)
        if _ol_m:
            text = text[: _ol_m.start()]

    # --- Version record ---
    version = TariffVersionRecord('''

if old_marker in src:
    result = src.replace(old_marker, new_marker, 1)
    with open(f, 'w', encoding='utf-8', newline='') as fh:
        fh.write(result)
    print('OK - replaced successfully')
    # Verify
    with open(f, encoding='utf-8') as fh:
        lines = fh.readlines()
    for i, line in enumerate(lines[188:205], start=189):
        print(f'{i}: {line}', end='')
else:
    # Try with \r\n
    old_marker_crlf = old_marker.replace('\n', '\r\n')
    if old_marker_crlf in src:
        new_marker_crlf = new_marker.replace('\n', '\r\n')
        result = src.replace(old_marker_crlf, new_marker_crlf, 1)
        with open(f, 'wb') as fh:
            fh.write(result.encode('utf-8'))
        print('OK - replaced (CRLF)')
    else:
        print('NOT FOUND - checking context...')
        idx = src.find('Version record')
        print(repr(src[max(0,idx-100):idx+200]))
