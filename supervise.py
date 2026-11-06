"""Supervise auto_course.py: restart it if it dies OR silently deadlocks.

A deadlock showed up as a live process at 0% CPU with no new log lines, so
liveness is judged by log freshness, not just by the process existing.

The supervisor also writes the pids it owns into _run/, because that is the only
reliable way for control.py to find and stop them: reading command lines through
WMI (Get-CimInstance Win32_Process) is denied to this process, which made
`control.py status` report NOTHING while the runner was driving the mouse.

  python supervise.py                 # run until the course finishes
  python supervise.py --start-after 4.3
"""
import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "auto_course.log")
WAITING = os.path.join(HERE, "auto_help", "waiting")
RUN = os.path.join(HERE, "_run")
PY = sys.executable
sys.path.insert(0, HERE)


def pid_path(name):
    os.makedirs(RUN, exist_ok=True)
    return os.path.join(RUN, name)


def write_pid(name, pid):
    with open(pid_path(name), "w", encoding="ascii") as fh:
        fh.write(str(pid))


def clear_pid(name):
    try:
        os.remove(pid_path(name))
    except OSError:
        pass


def read_pid(name):
    try:
        with open(pid_path(name), encoding="ascii") as fh:
            return int(fh.read().strip())
    except Exception:
        return None


def pid_alive(pid):
    """Is the pid running? (OpenProcess only -- os.kill(pid, 0) TERMINATES on Windows.)"""
    import ctypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h = k32.OpenProcess(0x1000, False, int(pid))       # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return bool(ok) and code.value == 259             # STILL_ACTIVE


def kill_pid(pid):
    import subprocess as sp
    sp.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    time.sleep(2)


def log_age():
    if not os.path.exists(LOG):
        return 0.0
    return time.time() - os.path.getmtime(LOG)


def waiting_for_help():
    """True while the runner is parked on a help request.

    Critical: the runner logs only every couple of minutes while it waits, so a
    log-age stall check would kill it -- two of my own mechanisms fighting, which
    made a whole run spin in a kill/restart loop.
    """
    try:
        return os.path.exists(WAITING)
    except OSError:
        return False


def start_waiter():
    """Keep a waiter alive so a help request turns into a session notification.

    The waiter exits as soon as auto_help/request.json appears; the harness then
    reports the background job's completion in-session, which wakes the agent.
    """
    proc = subprocess.Popen(
        [PY, os.path.join(HERE, "waiter.py"), "--timeout", "86400", "--interval", "3"],
        cwd=HERE,
        stdout=open(os.path.join(HERE, "waiter.out"), "a", encoding="utf-8"),
        stderr=open(os.path.join(HERE, "waiter.err"), "a", encoding="utf-8"))
    write_pid("waiter.pid", proc.pid)
    WAITER["proc"] = proc
    return proc


WAITER = {"proc": None}      # so the exit path can stop it


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-after", default="")
    ap.add_argument("--stall", type=float, default=420.0,
                    help="restart if the log has not been written for this long")
    ap.add_argument("--poll", type=float, default=15.0)
    ap.add_argument("--max-restarts", type=int, default=40)
    a = ap.parse_args()

    # Single-supervisor lock. Two supervisors means two runners driving the same mouse:
    # measured twice tonight, once leaving an old parked runner alongside the new one.
    other = read_pid("supervisor.pid")
    if other and other != os.getpid() and pid_alive(other):
        print(f"[supervisor] another supervisor is already running (pid={other}); "
              f"refusing to start a second one", flush=True)
        return 1

    write_pid("supervisor.pid", os.getpid())
    try:
        return supervise(a)
    finally:
        # stop the waiter child too: it polls for a help request forever, and a
        # finished run used to leave one orphaned process behind every time
        proc = WAITER.get("proc")
        if proc and proc.poll() is None:
            proc.terminate()
        for name in ("supervisor.pid", "runner.pid", "waiter.pid"):
            clear_pid(name)


def supervise(a):
    cursor = a.start_after
    restarts = 0
    waiter = start_waiter()
    print(f"[supervisor] waiter pid={waiter.pid} (event wake-up armed)", flush=True)
    while restarts <= a.max_restarts:
        if waiter.poll() is not None:
            waiter = start_waiter()
            print(f"[supervisor] waiter re-armed pid={waiter.pid}", flush=True)
        args = [PY, os.path.join(HERE, "auto_course.py"),
                "--poll", str(a.poll),
                "--help-timeout", "1800", "--stuck-after", "1500",
                "--max-cycles", "60"]
        # A runner left over from an earlier supervisor (killed supervisor, session churn)
        # would otherwise run alongside the new one -- two processes clicking the same
        # page. The runner is stateless (the cursor is in the state file), so replacing it
        # is safe.
        stray = read_pid("runner.pid")
        if stray and pid_alive(stray):
            print(f"[supervisor] a runner from an earlier supervisor is alive "
                  f"(pid={stray}); stopping it to keep one instance", flush=True)
            kill_pid(stray)
        if cursor:
            args += ["--start-after", cursor]
        out = open(os.path.join(HERE, "auto_run.out"), "a", encoding="utf-8")
        err = open(os.path.join(HERE, "auto_run.err"), "a", encoding="utf-8")
        out.write(f"\n=== supervisor launch (cursor={cursor or '-'}) "
                  f"{time.strftime('%H:%M:%S')} ===\n")
        out.flush()
        proc = subprocess.Popen(args, cwd=HERE, stdout=out, stderr=err)
        write_pid("runner.pid", proc.pid)
        print(f"[supervisor] started pid={proc.pid} cursor={cursor or '-'}", flush=True)

        while True:
            time.sleep(20)
            rc = proc.poll()
            if rc is not None:
                print(f"[supervisor] runner exited rc={rc}", flush=True)
                break
            if waiting_for_help():
                # the runner is parked on a help request on purpose; do not kill it
                if int(time.time()) % 300 < 20:
                    print(f"[supervisor] runner is waiting for a decision "
                          f"(log age {log_age():.0f}s) -- leaving it alone", flush=True)
                continue
            if log_age() > a.stall:
                print(f"[supervisor] runner stalled (log age {log_age():.0f}s) "
                      f"-> killing pid {proc.pid}", flush=True)
                proc.kill()
                proc.wait(timeout=30)
                break

        # pick the cursor up from the state the runner wrote
        st = {}
        try:
            import json
            st = json.load(open(os.path.join(HERE, "auto_course_state.json"),
                                encoding="utf-8")) or {}
        except Exception:
            st = {}
        if st.get("cursor"):
            cursor = st["cursor"]
        elif st.get("chapter_id"):
            # no cursor: resume from wherever the browser actually is
            try:
                import auto_course as ac
                cursor = ac.infer_cursor_from_chapter(st["chapter_id"]) or cursor
            except Exception:
                pass

        # A finished course, or a blocker that restarting cannot clear (input
        # dropped by UIPI, no answer available), must not become a restart storm:
        # the runner writes `stopped` for exactly that reason.
        stopped = st.get("stopped")
        if stopped:
            print(f"[supervisor] runner reports {stopped!r}; finishing", flush=True)
            return 0 if stopped == "done" else 1

        restarts += 1
        out.close()
        err.close()
        time.sleep(5)

    print("[supervisor] max restarts reached", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
