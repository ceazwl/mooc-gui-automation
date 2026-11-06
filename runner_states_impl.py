"""State implementations for the course runner.

Each function takes the shared Ctx and returns the next State. They are small and
single-purpose so a stall is always attributable to one state, and the log always
shows which state was running.

The two states the previous linear loop lacked entirely:
  * VERIFY -- decides correct / wrong / unknown after submitting
  * QUIZ retry -- re-answers after a wrong verdict instead of abandoning the section
"""
from __future__ import annotations

import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from runner_states import Ctx, State  # noqa: E402

FEEDBACK_BOX_FRAC = (0.56, 0.86, 0.24, 0.10)   # around the submit button / marker


def feedback_box():
    """Screen box that holds the 回答错误 marker (fractions of the screen)."""
    import mouse_click
    w, h = mouse_click.screen_size()
    fx, fy, fw, fh = FEEDBACK_BOX_FRAC
    return (int(w * fx), int(h * fy), int(w * fw), int(h * fh))


# ------------------------------------------------------------------ states
def st_init(ctx: Ctx) -> State:
    ac = ctx.ac
    # A closed browser is the one failure nothing else can recover from: relaunch it on
    # the course page before doing anything else (measured: the operator closed Edge and
    # the run could only park).
    if ac.browser_window() is None:
        ac.relaunch_browser(ctx.cursor)
    # Refresh next: the operator measured that a reload re-anchors the catalog to the
    # current section (revealing rows the scroll search never reached) and clears a
    # blocked player ("clicked play, nothing happened").
    ac.focus_course_tab()
    if ac.refresh_page():
        ctx.log("page refreshed at INIT")
    ac.autocalibrate()
    if not ctx.cursor:
        ctx.cursor = ac.infer_cursor_from_chapter() or ""
        if ctx.cursor:
            ctx.log(f"inferred current section = {ctx.cursor}")
        else:
            ctx.log("could not infer the current section")
    ctx.stuck_since = time.time()
    ctx.last_cursor = ctx.cursor
    return State.OBSERVE


def _same_question(a: str, b: str) -> bool:
    """Compare question stems the way the answer cache does.

    OCR mangles long stems, so a prefix comparison is the stable identity; the
    distinction matters because only a DIFFERENT question may clear the list of
    letters the platform already refused.
    """
    na = re.sub(r"\s+", "", a or "")[:14]
    nb = re.sub(r"\s+", "", b or "")[:14]
    return bool(na) and na == nb


def st_observe(ctx: Ctx) -> State:
    """Read the page once and decide what the situation is."""
    ac = ctx.ac
    try:
        words, img = ac.capture_page("sm")
    except ac.CaptureError as e:
        # the browser may simply be gone (closed/crashed): relaunch it once and retry
        if ac.browser_window() is None and ac.relaunch_browser(ctx.cursor):
            ctx.log("browser was gone; relaunched it on the course page")
            return State.OBSERVE
        ctx.stuck_reason = f"could not capture the course page: {e}"
        ctx.log(f"capture failed: {e}")
        return State.STUCK

    ctx.words, ctx.img = words, img
    quiz, why = ac.detect_quiz(words)
    done = ac.taskpoint_done(words)
    options, _dx = ac.quiz_option_rows(words, img)
    ctx.options = options
    question = ac.quiz_question_text(words)
    if not _same_question(question, ctx.question):
        ctx.quiz_attempts = 0
        ctx.rejected = []
    ctx.question = question
    ctx.kind = ac.QUESTION_TYPES and next(
        (t for t in ac.QUESTION_TYPES if t in "".join(w["text"] for w in words)), "?")

    # OCR misses the small option letters often (measured on the 6.4 quiz: neither
    # "A" nor "B" was read, so the quiz was not recognised, the runner tried to
    # advance instead, and the question stayed unanswered). A 题型 banner plus two
    # option discs plus a 提交 button IS a quiz -- no letter recognition needed.
    if not quiz and len(options) >= 2 and ac.quiz_banner_y(words) and \
            "提交" in "".join(w["text"] for w in words):
        quiz, why = True, (why + "; structurally: 题型 banner + option discs + 提交")

    ctx.log(f"done={done} quiz={quiz} ({why}) options={len(options)} "
            f"cursor={ctx.cursor or '-'}")

    if quiz and len(options) >= 2:
        return State.QUIZ
    if done:
        return State.ADVANCE
    return State.PLAY


def st_play(ctx: Ctx) -> State:
    """Start the video, or say why it cannot be started.

    This state used to click a stale screen fraction four times, in a loop that
    re-read the whole page every cycle, and log nothing an operator could act on
    ("picture never started moving"). Now the result carries a reason: a video that
    will not start because the click target is wrong or because input is dropped by
    UIPI goes to STUCK with that reason instead of spinning.
    """
    ac = ctx.ac
    ctx.play_attempts += 1
    ok, why = ac.ensure_playing(words=ctx.words)
    ctx.log(f"attempt {ctx.play_attempts}: "
            f"{'playing' if ok else 'NOT playing'} - {why}")

    if ok:
        # Reset the counter on success: PLAY is re-entered for every poll (measured:
        # "attempt 11" while the video was simply playing), so without this the budget
        # was already spent by the time a click genuinely failed, and the very first
        # real failure escalated to STUCK instead of retrying.
        ctx.play_attempts = 0
        # poll instead of spin: one page read costs ~4s of OCR
        time.sleep(max(1.0, min(ctx.poll, 20.0)))
        return State.OBSERVE

    if "UIPI" in why or "dropped" in why:
        # environmental, not transient: retrying cannot help
        ctx.stuck_reason = f"cannot click at all: {why}"
        return State.STUCK

    if "foreground" in why:
        # TRANSIENT: another window owns the foreground (a system flyout, or the
        # operator using the machine). Waiting is correct; asking for help is not,
        # and it must not burn a play attempt either.
        ctx.play_attempts = max(0, ctx.play_attempts - 1)
        ctx.log("browser is not focusable right now; will retry")
        time.sleep(5.0)
        return State.OBSERVE

    if ctx.play_attempts >= ctx.max_play_attempts:
        ctx.stuck_reason = f"the video will not start: {why}"
        return State.STUCK
    return State.OBSERVE


def st_quiz(ctx: Ctx) -> State:
    """Decide an answer with the model and submit it. Never guesses by rule."""
    ac = ctx.ac
    # NOTE: the per-question budget is reset in st_observe when the question text changes
    # (this platform gives each section "2 作业"), so the counter here only counts the
    # retries of one question. Do not add a second reset here.
    ctx.quiz_attempts += 1
    if ctx.quiz_attempts > ctx.max_quiz_attempts:
        ctx.stuck_reason = (f"{ctx.max_quiz_attempts} answers all refused by the "
                            f"platform (tried {ctx.rejected})")
        ctx.log(f"gave up after {ctx.max_quiz_attempts} attempts")
        return State.STUCK

    if not ctx.question.strip():
        ctx.log("question text is empty; re-reading the page")
        return State.OBSERVE

    # `rejected` is what makes a retry different from the attempt that just failed:
    # without it the same prompt produced the same wrong letter until the attempts
    # ran out, and the section was abandoned with the question still open.
    pick, note = ac.choose_answer(ctx.kind, ctx.question, ctx.options,
                                  reject=tuple(ctx.rejected))
    ctx.log(f"attempt {ctx.quiz_attempts}: kind={ctx.kind} pick={pick} note={note}"
            + (f" (already refused: {ctx.rejected})" if ctx.rejected else ""))
    if not pick:
        ctx.stuck_reason = f"no answer available: {note}"
        ctx.log("model produced no usable answer; handing over instead of guessing")
        return State.STUCK

    row = next((o for o in ctx.options if o["letter"] == pick), None)
    if not row:
        ctx.log(f"option {pick} is not among the on-screen rows")
        return State.OBSERVE

    ac.focus_browser()
    ac.mouse_click.click(row["x"], row["cy"])
    time.sleep(1.2)

    sx, sy = ac.submit_point(ctx.words)
    injectable, why = ac.injectable_at(sx, sy)
    if not injectable:
        ctx.stuck_reason = f"cannot submit the answer: {why}"
        ctx.log(f"BLOCKED at the submit button: {why}")
        return State.STUCK
    ac.mouse_click.click(sx, sy)
    time.sleep(2.5)
    # log the option coordinates too: when a click lands on page chrome instead of
    # the option, that is the first thing worth checking (it is not visible from the
    # submit coordinates alone)
    ctx.log(f"selected {pick} at ({row['x']},{row['cy']}) and submitted at ({sx},{sy})")
    ctx.note = note
    ctx.pick = pick
    return State.VERIFY


def st_verify(ctx: Ctx) -> State:
    """THE state that was missing: did the platform accept the answer?

    A red 回答错误 marker means the modal stays open and the answer must be redone.
    Previously "modal closed" was treated as success, so a wrong answer was logged
    as VERIFIED and the section was abandoned with the question still wrong.
    """
    ac = ctx.ac
    import answer_feedback

    ac.focus_browser()
    time.sleep(0.6)
    try:
        words, img = ac.capture_page("vf")
    except ac.CaptureError as e:
        ctx.log(f"cannot re-read the page to verify: {e}")
        return State.OBSERVE

    quiz_open, why = ac.detect_quiz(words)
    verdict, reason = answer_feedback.verdict_for(img, quiz_open, feedback_box())
    ctx.verdict = verdict
    ctx.log(f"verdict={verdict} ({reason})  task_done={ac.taskpoint_done(words)}")

    if verdict == "correct":
        # only cache answers the platform actually accepted
        if ctx.note.startswith("llm") and ctx.pick:
            try:
                import add_answer
                add_answer.remember(ctx.question, ctx.pick,
                                    f"verified by the platform; {ctx.note}")
                ctx.log(f"cached {ctx.pick} after the platform accepted it")
            except Exception as e:
                ctx.log(f"could not cache the answer: {type(e).__name__}: {e}")
        # Back to OBSERVE rather than straight to ADVANCE: a quiz can appear BEFORE the
        # video is finished, and advancing then would throw away the rest of the video.
        # OBSERVE re-reads the page and decides (done -> ADVANCE, else -> PLAY).
        return State.OBSERVE

    if verdict == "wrong":
        if ctx.pick and ctx.pick not in ctx.rejected:
            ctx.rejected.append(ctx.pick)
        ctx.log(f"WRONG ANSWER ({ctx.pick}) -> re-answering without it")
        return State.QUIZ          # retry, now with the refused letter excluded

    ctx.log("verdict unknown; re-reading the page before deciding")
    return State.OBSERVE


def st_advance(ctx: Ctx) -> State:
    ac = ctx.ac
    # After a video finishes, refresh once (operator's instruction). The reload
    # re-anchors the catalog -- measured on 8.3: the visible rows became
    # 8.2/8.3/8.4/8.5/9.1..9.5, which the catalog scroll search never reached -- and
    # it also clears a blocked player. Only done when the task point IS finished, so a
    # mid-video quiz cannot throw away the rest of the video.
    if ctx.words and ac.taskpoint_done(ctx.words) and not ctx.refreshed_for_section:
        if ac.refresh_page():
            ctx.refreshed_for_section = True
            ctx.log("page refreshed after the video finished")
            try:
                ctx.words, ctx.img = ac.capture_page("adv_r")
            except ac.CaptureError as e:
                ctx.log(f"  re-read after refresh failed: {e}")
                return State.OBSERVE
            # A reload can surface a quiz that the finished video had queued (this platform
            # gives each section "2 作业", and one of 6.4's was missed exactly this way).
            # Answer it before navigating away.
            quiz_after, why_after = ac.detect_quiz(ctx.words)
            options_after, _dx = ac.quiz_option_rows(ctx.words, ctx.img)
            if (quiz_after or (len(options_after) >= 2 and ac.quiz_banner_y(ctx.words)
                               and "提交" in "".join(w["text"] for w in ctx.words))):
                ctx.log(f"a quiz is on screen after the refresh ({why_after}); "
                        f"answering it before advancing")
                return State.OBSERVE

    rows = ac.catalog_rows(ctx.words)
    if len(rows) < 2:
        # A single bad OCR pass reads almost no catalog rows (measured: 2 of 17).
        # Re-read before declaring that there is no next section.
        for attempt in range(2):
            ctx.log(f"catalog read only {len(rows)} rows; re-reading the page "
                    f"({attempt + 1}/2)")
            try:
                ctx.words, ctx.img = ac.capture_page("adv")
            except ac.CaptureError as e:
                ctx.log(f"  re-read failed: {e}")
                break
            rows = ac.catalog_rows(ctx.words)
            if len(rows) >= 2:
                break

    nxt, max_seen = ac.resolve_next({"catalog": rows, "title": ""}, ctx.cursor)
    if not nxt:
        if ac.is_last_section(ctx.cursor):
            ctx.log(f"{ctx.cursor} is the last section of the course "
                    f"(LAST_SECTION={ac.LAST_SECTION}); the course is complete")
            return State.DONE
        # NOT the end: the catalog read is incomplete. Do not finish the course on a
        # failed read (that is exactly how the run "completed" at 8.2 while 8.3+ existed).
        ctx.stuck_reason = (f"no section after {ctx.cursor or '?'} could be found "
                            f"({len(rows)} rows read, largest {max_seen or '-'}); "
                            f"the course runs to {ac.LAST_SECTION}")
        ctx.log("no further section found")
        return State.STUCK
    if ctx.cursor and ac.order_key(nxt["num"]) <= ac.order_key(ctx.cursor):
        ctx.log(f"refusing to go backwards ({ctx.cursor} -> {nxt['num']})")
        return State.DONE

    if not ac.advance_to(nxt, ctx.cursor):
        # name the blocker: it reached STUCK before with the reason "unclassified",
        # which told the operator nothing about what to do
        ctx.stuck_reason = (f"clicked the catalog row for {nxt['num']} but navigation "
                            f"could not be confirmed (cursor stays {ctx.cursor})")
        ctx.log(f"could not navigate to {nxt['num']}")
        return State.STUCK

    ctx.cursor = nxt["num"]
    ctx.last_cursor = ctx.cursor
    ctx.stuck_since = time.time()
    # a new section starts with a clean slate: nothing from the previous one (a
    # refused answer, a failed play) applies here
    ctx.quiz_attempts = 0
    ctx.rejected = []
    ctx.play_attempts = 0
    ctx.refreshed_for_section = False
    ctx.stuck_reason = ""
    ac.save_state({"cursor": ctx.cursor, "section": nxt["num"]})
    ctx.log(f"advanced to {nxt['num']} {nxt['title']!r}")
    return State.OBSERVE


def st_stuck(ctx: Ctx) -> State:
    """Park and ask. Never guess, never die silently."""
    ac = ctx.ac
    reason = ctx.stuck_reason or "unclassified"
    # leave a trace the supervisor can read, so a genuine blocker does not turn
    # into a kill/restart storm
    try:
        ac.save_state({"cursor": ctx.cursor, "section": ctx.cursor,
                       "stopped": f"stuck: {reason}"})
    except Exception:
        pass
    rep = ac.ask_for_help(
        reason, st={"img": ctx.img, "words": ctx.words, "question": ctx.question,
                    "options": ctx.options},
        args=type("A", (), {"help_timeout": ctx.help_timeout})(),
        detail=f"state={ctx.state.value} cursor={ctx.cursor or '-'} "
               f"verdict={ctx.verdict}")
    if not ac.apply_help_reply(rep, {"words": ctx.words}):
        return State.ABORT
    ctx.stuck_reason = ""
    ctx.stuck_since = time.time()
    return State.OBSERVE
