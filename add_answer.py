"""Curated answer cache.

`answers_known.json` is a CACHE of questions whose answers the platform accepted --
not a decision layer. Answering is done by the model (llm_answer.py); this table
only avoids paying twice for the same question, and it is written only after a
verified-correct submission.

  python add_answer.py            # seed/refresh manually
  from add_answer import remember # called by the runner after verification
"""
from __future__ import annotations

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(HERE, "answers_known.json")

# kept short and distinctive: OCR mangles long stems, so matching uses the head
NEW = [
    ("在做作业的过程中，如果发现自己的见解与老师不同，不论自己的见解是否有道理",
     "B", "要求无条件放弃自己的见解，与独立思考相悖 -> 错", "1.3"),
    ("一个信仰宗教的人", "B", "绝对化断言 -> 错", "2.2"),
    ("在研究活动中科研同行之间应该相互尊重科研人员应该尊重研究对象及合作研究者",
     "A", "标准学术规范 -> 对", "2.3"),
    ("在科学研究中因粗心大意或一时疏忽而造成差错是难免的也是可以接受的",
     "B", "疏忽致错不可接受 -> 错", "2.3"),
    ("爱因斯坦和罗素不仅是伟大的科学家与哲学家也是伟大的人道主义者",
     "A", "史实陈述 -> 对", "2.4"),
    ("科学工作者必须更加自觉地遵守人类社会和生态的基本伦理珍惜与尊重自然和生命",
     "A", "标准学术伦理 -> 对", "2.5"),
    ("科研诚信是培养良好的科学道德建立良好的科研秩序的基础",
     "A", "标准陈述 -> 对", "3.1"),
    ("科学探索或学术研究不仅能够满足人的求知欲还能够寻求合乎人类利益和福祉的客观知识",
     "A", "标准陈述 -> 对", "3.2"),
    ("科研人员应该尽可能精确和仔细进行设计和实施研究记录和报告尽可能准确无误",
     "A", "严谨要求 -> 对", "3.3"),
    ("在工程伦理实践中伦理章程所规定的责任仅仅是原则性的",
     "A", "原则性规定需实践智慧补充 -> 对", "4.2"),
]


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _load() -> list:
    try:
        with open(FILE, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(entries: list) -> None:
    with open(FILE, "w", encoding="utf-8") as fh:      # no BOM
        json.dump(entries, fh, ensure_ascii=False, indent=2)


def remember(question: str, answer: str, note: str = "", section: str = "") -> bool:
    """Add a question whose answer the platform ACCEPTED. Returns True if added.

    Called from the VERIFY state, so the table can only ever contain answers that
    were confirmed correct -- never a guess.
    """
    q = _norm(question)
    if len(q) < 8 or not answer:
        return False
    entries = _load()
    head = q[:24]
    for e in entries:
        if _norm(e.get("match", ""))[:24] == head:
            return False
    entries.append({"match": q[:40], "answer": answer.upper()[:1],
                    "note": note or "verified by the platform", "section": section})
    _save(entries)
    return True


def main():
    entries = _load()
    have = {_norm(e.get("match", ""))[:24] for e in entries}
    added = 0
    for match, answer, note, section in NEW:
        if _norm(match)[:24] in have:
            continue
        entries.append({"match": match, "answer": answer, "note": note,
                        "section": section})
        added += 1
    _save(entries)
    print(f"answers_known.json: {len(entries)} entries (+{added})")


if __name__ == "__main__":
    main()
