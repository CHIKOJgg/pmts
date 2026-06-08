import re
line = 'P&L:          $+42.45  (+0.42%)'
print('Line:', repr(line))

# Test different patterns
patterns = [
    r'P&L:\s*([+\-]\$\d+\.\d+)',
    r'P&L:\s*([+\-]\$\d+\.\d+)',
    r'P&L:\s*([+\-]\x24\d+\.\d+)',
    r'P&L:\s*([+\-][\$]\d+\.\d+)',
    r'P&L:\s*([+\-]\$\d+\.\d+)',  # literal $
    r'P&L:\s*\$([+\-]\d+\.\d+)',
    r'P&L:\s*\$?([+\-]\d+\.\d+)',
]

for i, p in enumerate(patterns):
    match = re.search(p, line)
    print(f'Pattern {i}: {repr(p)} => {match.group(1) if match else "NO MATCH"}')