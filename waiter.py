"""Event-driven wake-up: block until the runner writes a help request, then exit.

The DSH harness notifies the session automatically when a background job ENDS.
So this script is launched as a background job and simply *waits*: it exits the
moment a help request appears (or when it is told to stand down). Its exit is
what wakes the agent, with the request contents printed for the notification.

  python waiter.py --timeout 3600
  python waiter.py --once          # report whether a request is pending

Files:
  auto_help/request.json  -- written by the runner when it needs a decision
  auto_help/reply.json    -- written by the agent (or control.py) to unblock it
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HELP_DIR = os.path.join(HERE, "auto_help")
REQ = os.path.join(HELP_DIR, "request.json")
REP = os.path.join(HELP_DIR, "reply.json")
STOP = os.path.join(HELP_DIR, "stop")


def read_json(path):
    """Tolerant JSON read.

    PowerShell's `Set-Content -Encoding UTF8` writes a BOM, which makes
    json.load raise -- and swallowing that silently turned a present request
    into "no request", leaving the waiter spinning forever.
    """
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, encoding=enc) as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None
        except Exception:
            continue
    print(f"WARNING: {path} exists but is not readable JSON", file=sys.stderr)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=3600.0)
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()

    os.makedirs(HELP_DIR, exist_ok=True)
    t0 = time.time()
    while time.time() - t0 < a.timeout:
        req = read_json(REQ)
        if req:
            print("=== RUNNER NEEDS A DECISION ===")
            print(json.dumps(req, ensure_ascii=False, indent=1))
            print("=== reply by writing", REP, "or run: python control.py reply <action> ===")
            return 0
        if os.path.exists(STOP):
            print("=== stand-down requested; waiter exiting ===")
            return 0
        if a.once:
            print("no pending request")
            return 0
        time.sleep(a.interval)

    print("waiter timed out with no request")
    return 1


if __name__ == "__main__":
    sys.exit(main())
