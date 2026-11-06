"""Turn Window OCR word boxes into clickable targets and search them.

Windows.Media.Ocr emits CJK text one character per "word", so raw boxes are too
small to click reliably. `merge_segments` glues horizontally adjacent words that
share a text line into one segment, which is what you actually want to aim at.

CLI:
  python ocr_find.py ocr.json --list
  python ocr_find.py ocr.json --find "新聊天" --find "设置"
  python ocr_find.py ocr.json --find "设置" --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher


@dataclass
class Segment:
    text: str
    x: int
    y: int
    w: int
    h: int

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2

    def as_dict(self) -> dict:
        d = asdict(self)
        d["cx"], d["cy"] = self.cx, self.cy
        return d


def merge_segments(words: list[dict], max_gap: int = 14, y_tol: int = 6) -> list[Segment]:
    """Group words into line segments. Gap threshold tuned for 100%-scale UI text."""
    if not words:
        return []
    items = sorted(words, key=lambda w: (w["y"], w["x"]))
    segments: list[Segment] = []
    cur: list[dict] = []

    def flush():
        if not cur:
            return
        x0 = min(w["x"] for w in cur)
        y0 = min(w["y"] for w in cur)
        x1 = max(w["x"] + w["w"] for w in cur)
        y1 = max(w["y"] + w["h"] for w in cur)
        text = "".join(w["text"] for w in cur)
        segments.append(Segment(text, x0, y0, x1 - x0, y1 - y0))

    for w in items:
        if not cur:
            cur = [w]
            continue
        prev = cur[-1]
        same_line = abs(w["y"] - prev["y"]) <= y_tol
        gap = w["x"] - (prev["x"] + prev["w"])
        if same_line and gap <= max_gap:
            cur.append(w)
        else:
            flush()
            cur = [w]
    flush()
    # reading order: top-to-bottom, then left-to-right
    segments.sort(key=lambda s: (s.y, s.x))
    return segments


def normalize(s: str) -> str:
    """Drop whitespace so '问 问 ChatGPT' style joins still match."""
    return re.sub(r"\s+", "", s).lower()


def _runs(words: list[dict], max_gap: int = 16, y_tol: int = 4) -> list[list[dict]]:
    """Group words into runs of horizontally adjacent words on the same visual row.

    Windows OCR returns words in an order that is *not* sorted by x within a row
    (a stray word far to the right can sit between two neighbours), so group into
    rows by y first, then sort each row by x before applying the gap rule.
    """
    rows: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (w["y"], w["x"])):
        if rows and abs(w["y"] - rows[-1][0]["y"]) <= y_tol:
            rows[-1].append(w)
        else:
            rows.append([w])

    runs: list[list[dict]] = []
    for row in rows:
        row.sort(key=lambda w: w["x"])
        cur: list[dict] = []
        for w in row:
            if cur:
                prev = cur[-1]
                gap = w["x"] - (prev["x"] + prev["w"])
                if not (-2 <= gap <= max_gap):
                    runs.append(cur)
                    cur = []
            cur.append(w)
        if cur:
            runs.append(cur)
    return runs


def _box(run: list[dict]) -> Segment:
    x0 = min(w["x"] for w in run)
    y0 = min(w["y"] for w in run)
    x1 = max(w["x"] + w["w"] for w in run)
    y1 = max(w["y"] + w["h"] for w in run)
    return Segment("".join(w["text"] for w in run), x0, y0, x1 - x0, y1 - y0)


def find(segments: list[Segment], query: str, exact: bool = False) -> list[Segment]:
    """Search already-merged segments. Prefer `find_phrase` on raw words."""
    q = normalize(query)
    hits = [s for s in segments
            if (normalize(s.text) == q if exact else q in normalize(s.text))]
    hits.sort(key=lambda s: (len(s.text) - len(query), s.y, s.x))
    return hits


def find_phrase(words: list[dict], query: str, exact: bool = False,
                max_gap: int = 16, y_tol: int = 4, fuzzy: float = 0.8,
                near: tuple[int, int] | None = None, region: tuple[int, int, int, int] | None = None,
                size_bonus: float = 0.25) -> list[Segment]:
    """Locate `query` as a contiguous run of OCR words.

    Matches inside a row run first (so a multi-character CJK label merges into
    one box), then falls back to single-word containment. `fuzzy` (0-1) allows
    near matches such as OCR reading 位 as 亻; pass 1.0 to require an exact hit.
    `size_bonus` counters the bias towards long low-confidence rows; `near` /
    `region` bias the result towards a screen area you already trust.
    """
    q = normalize(query)
    if not q:
        return []

    def score_of(sub: str) -> float:
        if sub == q:
            return 1.0
        # weight coverage: a run covering more of the query beats a short
        # high-ratio fragment, which matters for short CJK queries.
        return SequenceMatcher(None, q, sub).ratio() * (len(sub) / max(len(q), 1)) ** 0.5

    def contextualize(seg: Segment) -> float:
        delta = 0.0
        if near is not None:
            dist = ((seg.cx - near[0]) ** 2 + (seg.cy - near[1]) ** 2) ** 0.5
            delta -= min(dist / 2000.0, 0.5)
        if region is not None:
            rx, ry, rw, rh = region
            inside = rx <= seg.cx <= rx + rw and ry <= seg.cy <= ry + rh
            delta += 0.3 if inside else -0.5
        return delta

    def area_of(seg: Segment) -> float:
        return seg.w * seg.h

    hits: list[tuple[float, Segment]] = []
    for run in _runs(words, max_gap=max_gap, y_tol=y_tol):
        texts = [normalize(w["text"]) for w in run]
        for i in range(len(run)):
            acc = ""
            for j in range(i, len(run)):
                acc += texts[j]
                if len(acc) > len(q) + 6:
                    break
                score = score_of(acc)
                if (acc == q) if exact else (score >= fuzzy and len(acc) <= len(q) + 2):
                    hits.append((score, _box(run[i:j + 1])))
    if not hits:  # single-word fallback
        for w in words:
            t = normalize(w["text"])
            if (t == q) if exact else (q in t):
                hits.append((1.0, Segment(w["text"], w["x"], w["y"], w["w"], w["h"])))
            elif not exact and score_of(t) >= fuzzy:
                hits.append((score_of(t), Segment(w["text"], w["x"], w["y"], w["w"], w["h"])))

    def rank(pair: tuple[float, Segment]) -> tuple:
        score, seg = pair
        return (-(score + contextualize(seg)),           # best text match first
                0 if normalize(seg.text) == q else 1,    # then exact text
                area_of(seg),                            # then the tightest box
                seg.y, seg.x)

    hits.sort(key=rank)
    return [seg for _score, seg in hits]


def load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    p = argparse.ArgumentParser(description="Search OCR boxes for clickable text.")
    p.add_argument("ocr_json")
    p.add_argument("--find", action="append", default=[], help="text to locate (repeatable)")
    p.add_argument("--list", action="store_true", help="list all merged segments")
    p.add_argument("--exact", action="store_true", help="require exact text match")
    p.add_argument("--min-len", type=int, default=1, help="skip segments shorter than this")
    p.add_argument("--json", action="store_true", help="emit JSON")
    p.add_argument("--max-gap", type=int, default=16)
    args = p.parse_args()

    data = load(args.ocr_json)
    words = data.get("words", [])
    segments = [s for s in merge_segments(words, max_gap=args.max_gap) if len(s.text) >= args.min_len]

    if args.json:
        out = {
            "image": data.get("image"),
            "size": data.get("size"),
            "lang": data.get("lang"),
            "segment_count": len(segments),
            "queries": {
                q: [s.as_dict() for s in find_phrase(words, q, args.exact, max_gap=args.max_gap)]
                for q in args.find
            },
        }
        if args.list:
            out["segments"] = [s.as_dict() for s in segments]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"image={data.get('image')} size={data.get('size')} lang={data.get('lang')} "
          f"words={len(words)} segments={len(segments)}")
    if args.list:
        for s in segments:
            print(f"  [{s.x:5},{s.y:5} {s.w:4}x{s.h:3}] center=({s.cx:5},{s.cy:5})  {s.text}")
    for q in args.find:
        hits = find_phrase(words, q, args.exact, max_gap=args.max_gap)
        print(f"find {q!r}: {len(hits)} hit(s)")
        for s in hits:
            print(f"  -> center=({s.cx},{s.cy}) box=[{s.x},{s.y} {s.w}x{s.h}] text={s.text!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
