"""Start / stop / check the autonomous course runner. One switch for the operator.

  python control.py status      # what is running right now
  python control.py stop        # stop everything (supervisor, runner, waiter)
  python control.py start       # start the supervisor from the saved cursor
  python control.py pause       # alias of stop
  python control.py resume      # alias of start
  python control.py pending     # show the help request the runner is parked on
  python control.py reply <action> [k=v ...]     e.g. reply click x=100 y=200

Stopping matters because the runner drives the real mouse and keyboard: if you
want to use the computer yourself, stop it first or your clicks will fight it.
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
STATE = os.path.join(HERE, "auto_course_state.json")
LOG = os.path.join(HERE, "auto_course.log")


def ps(cmd):
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                       capture_output=True, text=True, timeout=60,
                       encoding="utf-8", errors="replace")
    return (r.stdout or "").strip()


def alive(pid):
    out = ps(f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | "
             f"Select-Object -First 1 -ExpandProperty Id")
    return out.strip() == str(pid)


def processes():
    """Our own processes, from the pid files the supervisor writes.

    Reading command lines through WMI (Get-CimInstance Win32_Process) is denied to
    this process -- "access denied" -- which made `status` report NOTHING while
    the runner was driving the real mouse. Get-Process on a pid we already know
    works, so the pids are recorded in _run/ instead of rediscovered.
    """
    run = os.path.join(HERE, "_run")
    rows = []
    for name, kind in (("supervisor.pid", "supervisor"), ("runner.pid", "runner"),
                       ("waiter.pid", "waiter")):
        path = os.path.join(run, name)
        try:
            pid = int(open(path, encoding="ascii").read().strip())
        except Exception:
            continue
        if alive(pid):
            rows.append((kind, pid, name))
        else:
            try:
                os.remove(path)          # stale marker from a crash
            except OSError:
                pass
    return rows


def read_state():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def cursor():
    return read_state().get("cursor") or ""


def clear_stopped():
    """Drop the parked marker and return what it was (None if there was none).

    The supervisor reads the state file at the top of every loop and finishes as soon as
    `stopped` is set -- that is how "the course is done" and "parked on a blocker" avoid a
    restart storm. So `start` has to clear it first: otherwise the new supervisor prints
    its pid and exits within a second, which looks like the command did nothing (the
    operator hit exactly that, and it is why resuming used to need a second command).
    """
    st = read_state()
    was = st.pop("stopped", None)
    if was is not None:
        with open(STATE, "w", encoding="utf-8") as fh:
            json.dump(st, fh, ensure_ascii=False, indent=1)
    return was


def log_age():
    if not os.path.exists(LOG):
        return None
    return time.time() - os.path.getmtime(LOG)


def cmd_status():
    rows = processes()
    print(f"cursor (last section dispatched): {cursor() or '-'}")
    age = log_age()
    print(f"log last written: {age:.0f}s ago" if age is not None else "no log yet")
    parked = read_state().get("stopped")
    if parked:
        print(f"parked     : {parked!r}  (control.py start clears this)")
    if not rows:
        print("running: NOTHING")
        return 0
    print("running:")
    for kind, pid, cmd in rows:
        print(f"  {kind:11} pid={pid}")
    return 0


def cmd_stop():
    rows = processes()
    if not rows:
        print("nothing to stop")
        return 0
    # supervisor first, otherwise it restarts the runner we just killed
    order = {"supervisor": 0, "waiter": 1, "runner": 2}
    for kind, pid, _marker in sorted(rows, key=lambda r: order.get(r[0], 3)):
        ps(f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue")
        print(f"stopped {kind} pid={pid}")
        time.sleep(0.6)
    time.sleep(1)
    left = processes()
    print("remaining:", left if left else "none")
    return 0


def cmd_start():
    if processes():
        print("something is already running; run 'stop' first")
        return 1
    was = clear_stopped()
    if was is not None:
        print(f"cleared the parked marker ({was!r}) so the supervisor will actually run")
    cur = cursor()
    sup = subprocess.Popen(
        [PY, os.path.join(HERE, "supervise.py"), "--start-after", cur,
         "--stall", "420", "--poll", "15"],
        cwd=HERE,
        stdout=open(os.path.join(HERE, "sup.out"), "a", encoding="utf-8"),
        stderr=open(os.path.join(HERE, "sup.err"), "a", encoding="utf-8"))
    print(f"supervisor pid={sup.pid}  cursor={cur or '-'}")
    print("the runner drives the real mouse/keyboard -- leave the computer alone "
          "or it will fight your input")
    return 0


def cmd_pending():
    import help_channel
    req = help_channel.pending()
    if not req:
        print("no pending request")
        return 0
    print(json.dumps(req, ensure_ascii=False, indent=1))
    return 2


def cmd_reply(rest):
    """control.py reply <action> [k=v ...]   e.g. reply click x=100 y=200"""
    import help_channel
    if not rest:
        print("usage: control.py reply <action> [k=v ...]")
        return 1
    action, *kvs = rest
    rep = {"action": action}
    for kv in kvs:
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        try:
            rep[k] = int(v)
        except ValueError:
            rep[k] = v
    req = help_channel.pending()
    if req:
        rep["id"] = req.get("id")
    help_channel._write(help_channel.REP, rep)
    print("reply written:", json.dumps(rep, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("status", "stop", "start", "pause", "resume",
                                       "pending", "reply"))
    ap.add_argument("rest", nargs="*")
    a = ap.parse_args()
    action = {"pause": "stop", "resume": "start"}.get(a.action, a.action)
    if action == "pending":
        sys.exit(cmd_pending())
    if action == "reply":
        sys.exit(cmd_reply(a.rest))
    sys.exit({"status": cmd_status, "stop": cmd_stop, "start": cmd_start}[action]())
