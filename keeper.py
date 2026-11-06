"""Keep the course run alive overnight: restart the supervisor if it dies.

The supervisor already restarts the runner; this covers the one case it cannot cover
itself -- the supervisor process going away (crash, killed, session churn). It is the
third layer:

    runner      -> does the work, parks on STUCK (and writes a help request)
    supervisor  -> restarts the runner, stops on `stopped: done`/`stuck: ...`
    keeper      -> restarts the supervisor (this file), stops when the course is done

  python keeper.py --interval 300 --max-hours 14
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
RUN = os.path.join(HERE, "_run")
STATE = os.path.join(HERE, "auto_course_state.json")
LOG = os.path.join(HERE, "keeper.log")


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line, flush=True)


def alive(pid):
    """Is the pid running? (OpenProcess only -- os.kill(pid, 0) TERMINATES on Windows.)"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h = k32.OpenProcess(0x1000, False, int(pid))       # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return bool(ok) and code.value == 259             # STILL_ACTIVE


def pid_of(name):
    try:
        with open(os.path.join(RUN, name), encoding="ascii") as fh:
            return int(fh.read().strip())
    except Exception:
        return None


def state():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=300.0)
    ap.add_argument("--max-hours", type=float, default=14.0)
    ap.add_argument("--max-restarts", type=int, default=24)
    a = ap.parse_args()

    t0 = time.time()
    restarts = 0
    log(f"keeper started (interval {a.interval:.0f}s, max {a.max_hours:.0f}h)")
    while time.time() - t0 < a.max_hours * 3600:
        st = state()
        stopped = st.get("stopped")
        if stopped == "done":
            log("course reports done; keeper exiting")
            return 0
        sup = pid_of("supervisor.pid")
        if sup and alive(sup):
            time.sleep(a.interval)
            continue
        if stopped:                       # parked on a blocker: not the keeper's job
            log(f"runner is parked ({stopped!r}); leaving it for the operator")
            time.sleep(a.interval)
            continue
        if restarts >= a.max_restarts:
            log("restart budget exhausted; keeper exiting")
            return 1
        restarts += 1
        log(f"supervisor is not running (pid={sup}) -> starting it (restart {restarts})")
        try:
            with open(os.path.join(HERE, "sup.out"), "a", encoding="utf-8") as out, \
                    open(os.path.join(HERE, "sup.err"), "a", encoding="utf-8") as err:
                subprocess.Popen([PY, os.path.join(HERE, "supervise.py"), "--poll", "15"],
                                 cwd=HERE, stdout=out, stderr=err)
        except Exception as e:
            log(f"could not start the supervisor: {type(e).__name__}: {e}")
        time.sleep(max(60.0, a.interval))
    log("keeper reached its time limit; exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
