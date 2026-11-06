"""Catalog parsing regression: every section number must be recovered.

Windows OCR renders section numbers three different ways on the same page, and
the 3.x rows have NO separator at all ("31", "32", "33"). Missing that made 3.3
vanish from the catalog.

Pure synthetic cases -- no screenshot needed. Reading a real page's catalog off a
screenshot is covered by test_offline.py against its fixture.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

fails = []


def check(name, got, want):
    ok = got == want
    if not ok:
        fails.append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got={got!r} want={want!r}")


# synthetic cases covering all three OCR shapes
BAND = [
    # shape A: 1 . 4
    {"text": "1", "x": 1434, "y": 523, "w": 5, "h": 13},
    {"text": "．", "x": 1443, "y": 534, "w": 2, "h": 2},
    {"text": "4", "x": 1446, "y": 523, "w": 10, "h": 13},
    # shape B: 3 1 (no dot)
    {"text": "3", "x": 1433, "y": 907, "w": 8, "h": 13},
    {"text": "1", "x": 1443, "y": 907, "w": 8, "h": 13},
    # shape B: 3 2
    {"text": "3", "x": 1433, "y": 955, "w": 8, "h": 13},
    {"text": "2", "x": 1443, "y": 955, "w": 8, "h": 13},
    # shape B: 3 3
    {"text": "3", "x": 1433, "y": 1003, "w": 8, "h": 13},
    {"text": "3", "x": 1443, "y": 1003, "w": 8, "h": 13},
    # shape C: already merged
    {"text": "23", "x": 1433, "y": 763, "w": 22, "h": 13},
    # titles so rows are kept
    {"text": "诚", "x": 1461, "y": 523, "w": 17, "h": 16},
    {"text": "诚", "x": 1461, "y": 907, "w": 17, "h": 16},
    {"text": "学", "x": 1461, "y": 955, "w": 17, "h": 16},
    {"text": "科", "x": 1461, "y": 1003, "w": 17, "h": 16},
    {"text": "科", "x": 1461, "y": 763, "w": 17, "h": 16},
]
from auto_course import _merge_digits, NUM_RE

merged = _merge_digits(BAND)
nums = [w["text"] for w in merged if NUM_RE.match(w["text"].strip())]
print("merged numbers:", nums)
check("shape A '1.4' recovered", "1.4" in nums, True)
check("shape B '3.1' recovered (no dot)", "3.1" in nums, True)
check("shape B '3.2' recovered (no dot)", "3.2" in nums, True)
check("shape B '3.3' recovered (no dot)", "3.3" in nums, True)
check("shape C '23' -> '2.3'", "2.3" in nums, True)

print()
print("RESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
