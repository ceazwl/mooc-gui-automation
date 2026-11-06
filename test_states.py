"""Exercise the state machine against the live page, one state at a time."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auto_course as ac
import runner_states as rs
import runner_states_impl as impl

ctx = rs.Ctx(ac=ac, cursor="5.1", poll=15, stuck_after=1500, help_timeout=60)

print("== INIT ==")
nxt = impl.st_init(ctx)
print(f"   -> {nxt.value}  cursor={ctx.cursor}")

print("== OBSERVE ==")
nxt = impl.st_observe(ctx)
print(f"   -> {nxt.value}")
print(f"   kind={ctx.kind} quiz_options={len(ctx.options)}")
print(f"   question={ctx.question[:60]!r}")

if nxt == rs.State.QUIZ:
    print("== QUIZ (decide, no clicking in this test) ==")
    pick, note = ac.choose_answer(ctx.kind, ctx.question, ctx.options)
    print(f"   model pick={pick}")
    print(f"   note={note}")

print("== VERIFY (current on-screen state, no submit) ==")
import answer_feedback
ac.focus_browser()
time.sleep(0.5)
v = answer_feedback.verdict_for(ctx.img, True, impl.feedback_box())
print(f"   verdict={v}")
print(f"   feedback box={impl.feedback_box()}")
