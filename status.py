"""Report what the runner is doing: state, progress, blockers, recent log.

Pure local file reads -- no screen access, no OCR, no API. The log is the runner's
only account of itself, and every state names itself, so this is enough to answer
"where is it and why did it stop" without spending model tokens.

  python status.py            # summary + last 25 log lines
  python status.py --tail 60
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "auto_course.log")
STATE = os.path.join(HERE, "auto_course_state.json")
HELP_REQ = os.path.join(HERE, "auto_help", "request.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tail", type=int, default=25)
    a = ap.parse_args()

    if not os.path.exists(LOG):
        print("no log yet at", LOG)
        return 1
    with open(LOG, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()

    states = re.findall(r"--> state (\w+)", "\n".join(lines))
    advanced = re.findall(r"advanced to (\S+)", "\n".join(lines))
    blocked = [l for l in lines if "BLOCKED" in l]
    wrong = [l for l in lines if "WRONG ANSWER" in l]
    quizzes = [l for l in lines if "verdict=" in l]
    age = time.time() - os.path.getmtime(LOG)

    print(f"log        : {LOG}  (last write {age:.0f}s ago)")
    print(f"entries    : {len(lines)}")
    print(f"last state : {states[-1] if states else '-'}")
    print(f"states seen: {dict(Counter(states))}")
    print(f"advanced   : {', '.join(advanced[-8:]) or '-'}")
    print(f"answers    : {len(quizzes)} verdicts, {len(wrong)} wrong")
    if blocked:
        print(f"BLOCKED    : {blocked[-1].strip()}")

    # rate and ETA: the log timestamps are HH:MM:SS, and a run spans midnight, so the
    # deltas are taken modulo a day
    def secs(line):
        m = re.match(r"\[(\d\d):(\d\d):(\d\d)\]", line)
        if not m:
            return None
        h, mi, s = map(int, m.groups())
        return h * 3600 + mi * 60 + s

    stamps = [(secs(l), l) for l in lines]
    adv_lines = [l for l in lines if "advanced to" in l]
    marks = []
    for l in adv_lines:
        t = secs(l)
        m = re.search(r"advanced to (\S+)", l)
        if t is not None and m:
            marks.append((t, m.group(1)))
    if len(marks) >= 3:
        t0, n0 = marks[0]
        t1, n1 = marks[-1]
        span = (t1 - t0) if t1 >= t0 else (t1 + 86400 - t0)
        if span > 0:
            per = span / (len(marks) - 1)
            last = n1
            remaining = None
            try:
                # NOTE: do not name these `a`/`b` -- argparse already owns `a` in main()
                maj, minor = (int(x) for x in last.split("."))
                remaining = max(0, (20 - maj) * 5 + (5 - minor) + 1)
            except Exception:
                remaining = None
            print(f"rate       : {len(marks)} sections logged, {per / 60:.0f} min/section "
                  f"({n0} .. {n1})")
            if remaining:
                print(f"to 20.5    : ~{remaining} sections -> ETA ~{remaining * per / 3600:.0f}h")

    if os.path.exists(STATE):
        try:
            st = json.load(open(STATE, encoding="utf-8"))
            print(f"state file : cursor={st.get('cursor')!r} stopped={st.get('stopped')!r}")
        except Exception as e:
            print(f"state file : unreadable ({e})")
    if os.path.exists(HELP_REQ):
        try:
            req = json.load(open(HELP_REQ, encoding="utf-8"))
            print(f"HELP WANTED: {req.get('reason')}  (detail: {str(req.get('detail'))[:120]})")
        except Exception:
            pass

    print(f"\n--- last {a.tail} log lines ---")
    for l in lines[-a.tail:]:
        print(l)
    return 0


if __name__ == "__main__":
    sys.exit(main())
