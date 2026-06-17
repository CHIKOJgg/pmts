import re

# Test the exact pattern from test_smoke.py
pattern = r'P\&L:\s*([+\-]\$[\d.]+)'
print('Pattern:', repr(pattern))

line = 'P&L:          $+42.45  (+0.42%)'
print('Line:', repr(line))

match = re.search(pattern, line)
print('Match:', match)
if match:
    print('Group:', match.group(1))

# Let's trace what the regex engine actually sees
import re as re_module
print('Regex pattern compiled:', re_module.compile(pattern).pattern)

# Try with single backslash in raw string (not double)
pattern2 = r'P&L:\s*([+\-]\$[\d.]+)'
print('Pattern2:', repr(pattern2))
print('Regex pattern compiled:', re_module.compile(pattern2).pattern)
match2 = re.search(pattern2, line)
print('Match2:', match2)
if match2:
    print('Group2:', match2.group(1))