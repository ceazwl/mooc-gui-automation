"""Explicit state machine for the course runner.

Why a state machine: the previous version was a linear loop with ad-hoc patches,
and it had no state for "the answer was wrong". It treated "the modal closed" as
success, so a red 回答错误 went unnoticed and the section was abandoned -- the
answer was never corrected because nothing ever looked.

States and their transitions
    INIT     -> decide where we are            -> OBSERVE
    OBSERVE  -> read the page                  -> PLAY | QUIZ | ADVANCE
    PLAY     -> ensure the video is running    -> OBSERVE | STUCK
    QUIZ     -> answer (model) and submit       -> VERIFY
    VERIFY   -> correct?                       -> ADVANCE(retry not needed)
              -> wrong?                        -> QUIZ (retry, rejected letter excluded)
              -> unknown?                      -> OBSERVE (re-reads the page)
    ADVANCE  -> confirm navigation by chapterId -> OBSERVE
    DONE     -> no further section             -> stop
    STUCK    -> hand to the help channel       -> OBSERVE | ABORT

A state that cannot make progress says so and leaves for STUCK with a reason --
PLAY in particular used to retry a click that could never work (a stale target, or
input dropped by UIPI at Low integrity) forever, which is indistinguishable in the
log from a slow video.

Every state writes its name to the log, so a stall is always attributable and the
"why did it stop" question is answerable from the log alone.
"""
from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass, field
from enum import Enum

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


class State(str, Enum):
    INIT = "INIT"
    OBSERVE = "OBSERVE"
    PLAY = "PLAY"
    QUIZ = "QUIZ"
    VERIFY = "VERIFY"
    ADVANCE = "ADVANCE"
    DONE = "DONE"
    STUCK = "STUCK"
    ABORT = "ABORT"


@dataclass
class Ctx:
    """Everything a state needs; no hidden globals."""
    ac: object = None                     # the auto_course module
    cursor: str = ""
    state: State = State.INIT
    last_cursor: str = ""
    quiz_attempts: int = 0
    max_quiz_attempts: int = 4
    rejected: list = field(default_factory=list)   # letters the platform refused
    play_attempts: int = 0
    max_play_attempts: int = 3
    refreshed_for_section: bool = False    # the post-video refresh already happened
    advance_attempts: int = 0
    max_advance_attempts: int = 3
    # must start at "now", not 0: the stall check runs BEFORE the first handler, and
    # with 0 it saw 1.79 billion seconds of no progress and parked the run in STUCK
    # on the very first step (only when no --start-after was given, because that is
    # what used to move stuck_since forward on step 1)
    stuck_since: float = field(default_factory=time.time)
    stuck_after: float = 1500.0
    stuck_reason: str = ""
    poll: float = 15.0
    words: list = field(default_factory=list)
    img: str = ""
    options: list = field(default_factory=list)
    question: str = ""
    kind: str = ""
    verdict: str = ""                     # correct | wrong | unknown
    note: str = ""
    pick: str = ""                        # the letter last submitted
    help_timeout: float = 1800.0
    history: list = field(default_factory=list)

    def log(self, msg):
        self.ac.log(f"[{self.state.value}] {msg}")

    def go(self, state):
        self.history.append((time.time(), self.state.value, state.value))
        self.state = state
        self.ac.log(f"--> state {state.value}")
