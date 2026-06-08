import re

line = 'P&L:          $+42.45  (+0.42%)'
print('Line:', repr(line))

# The issue: [\d.]+ doesn't match + sign
p1 = r'P&L:\s*([+\-]\$[\d.]+)'
m1 = re.search(p1, line)
print(f'Pattern 1: {repr(p1)} => {m1.group(1) if m1 else "NO MATCH"}')

# Fixed: allow optional + or - after $
p2 = r'P&L:\s*([+\-]\$[+\-]?[\d.]+)'
m2 = re.search(p2, line)
print(f'Pattern 2: {repr(p2)} => {m2.group(1) if m2 else "NO MATCH"}')

# Or capture the whole thing
p3 = r'P&L:\s*(\$[+\-]?[\d.]+)'
m3 = re.search(p3, line)
print(f'Pattern 3: {repr(p3)} => {m3.group(1) if m3 else "NO MATCH"}')

# Best: exactly match the format
p4 = r'P&L:\s*([+\-]\$\d+\.\d+)'
m4 = re.search(p4, line)
print(f'Pattern 4: {repr(p4)} => {m4.group(1) if m4 else "NO MATCH"}')

# The test expects format like $+42.45 or $-12.34
p5 = r'P&L:\s*([+\-]\$\d+\.\d+)'
m5 = re.search(p5, line)
print(f'Pattern 5: {repr(p5)} => {m5.group(1) if m5 else "NO MATCH"}')

# Test what the actual test uses
p6 = r'P\&L:\s*([+\-]\$[\d.]+)'
m6 = re.search(p6, line)
print(f'Test pattern: {repr(p6)} => {m6.group(1) if m6 else "NO MATCH"}')