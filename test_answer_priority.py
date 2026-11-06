"""Compare the new answer priority (LLM first) against the old behaviour.

Uses the two real questions from this course, whose correct answers are known.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auto_course import choose_answer

# (stem, options, kind, expected)
CASES = [
    ("很多高科技是探索性的和不成熟的，其技术后果和伦理价值影响难以预见。",
     [{"letter": "A", "text": "对"}, {"letter": "B", "text": "错"}], "判断题", "A"),
    ("在科学研究中因粗心大意或一时疏忽而造成差错是难免的，也是可以接受的。",
     [{"letter": "A", "text": "对"}, {"letter": "B", "text": "错"}], "判断题", "B"),
    ("一个信仰宗教的人，不可能成为一名优秀的科学家。",
     [{"letter": "A", "text": "对"}, {"letter": "B", "text": "错"}], "判断题", "B"),
    ("在研究活动中，科研同行之间应该相互尊重，科研人员应该尊重研究对象及合作研究者。",
     [{"letter": "A", "text": "对"}, {"letter": "B", "text": "错"}], "判断题", "A"),
    ("这是一道从未见过的全新题目：科研数据应当妥善保存以备核查。",
     [{"letter": "A", "text": "对"}, {"letter": "B", "text": "错"}], "判断题", "A"),
]

fails = 0
for stem, opts, kind, want in CASES:
    got, note = choose_answer(kind, stem, opts)
    ok = got == want
    fails += 0 if ok else 1
    src = note.split("(")[0] if note else "?"
    print(f"{'PASS' if ok else 'FAIL'}  want={want} got={got}  via={src}")
    print(f"      {stem[:34]}")
    print(f"      note: {note[:90]}")

print()
print("RESULT:", "ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
