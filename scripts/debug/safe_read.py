import os

with open('1023_analysis.txt', 'rb') as f:
    content = f.read()
    # Decode as utf-16le if it starts with the BOM
    if content.startswith(b'\xff\xfe'):
        text = content.decode('utf-16le')
    else:
        text = content.decode('utf-8', errors='ignore')
    
    # Print in chunks to avoid terminal issues
    lines = text.splitlines()
    for line in lines:
        print(line)
