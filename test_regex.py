import re
line = 'P&L:          $+42.45  (+0.42%)'
print('Line:', repr(line))
# Test pattern without dollar sign
pat = r'P&L:\s*'
match = re.search(pat, line)
print('Match P&L:', match)

pat2 = r'\$'
match2 = re.search(pat2, line)
print('Match $:', match2)

pat3 = r'P&L:\s*.'
match3 = re.search(pat3, line)
print('Match P&L + any:', match3)

# Full pattern
pat4 = r'P&L:\s*([+\-]\$\d+\.\d+)'
match4 = re.search(pat4, line)
print('Full pattern match:', match4)
if match4:
    print('Group:', match4.group(1))

# Try with raw $
pat5 = r'P&L:\s*([+\-]\$\d+\.\d+)'
print('Pattern repr:', repr(pat5))
match5 = re.search(pat5, line)
print('Match5:', match5)