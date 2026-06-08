import re
line = 'P&L:          $+42.45  (+0.42%)'
print('Line:', repr(line))

# Working patterns - capturing without $
p1 = r'P&L:\s*\$([+\-]\d+\.\d+)'
m1 = re.search(p1, line)
print(f'Pattern 1: {repr(p1)} => group1={m1.group(1) if m1 else "NO MATCH"}')

# What about including $ in capture
p2 = r'P&L:\s*(\$[+\-]\d+\.\d+)'
m2 = re.search(p2, line)
print(f'Pattern 2: {repr(p2)} => group1={m2.group(1) if m2 else "NO MATCH"}')

# With optional spaces
p3 = r'P&L:\s*\$([+\-]\d+\.\d+)'
m3 = re.search(p3, line)
print(f'Pattern 3: {repr(p3)} => group1={m3.group(1) if m3 else "NO MATCH"}')

# Test with $ in char class
p4 = r'P&L:\s*([\$][+\-]\d+\.\d+)'
m4 = re.search(p4, line)
print(f'Pattern 4: {repr(p4)} => group1={m4.group(1) if m4 else "NO MATCH"}')