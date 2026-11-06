"""Offline regression tests: everything checkable without touching the screen.

Fixture: testdata/poster_5_2.jpg -- a real full-screen shot of section 5.2 with the
video paused on its poster. It is the frame that exposed both bugs that stopped
playback, so it is the frame that guards them:

  1. the play click aimed at a hardcoded screen fraction (0.231W, 0.389H) which is
     the lecturer's FACE on this layout, 590 px left and 490 px above the real
     button -- and this player ignores a click on the video body, so playback
     could never start;
  2. "the picture never started moving" was indistinguishable from a click that
     was discarded by UIPI at Low integrity, so the state machine retried it
     forever.

Runs in seconds, needs no browser and no API key (the model is stubbed out).

  python test_offline.py
"""
from __future__ import annotations

import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FIXTURE = os.path.join(HERE, "testdata", "poster_5_2.jpg")
# our OWN subdirectory: a live run keeps its screenshots in _dbg/run-<pid>/, and a
# test must never delete another process's working files (that killed a live run)
WORK = os.path.join(HERE, "_dbg", "test")
# hand-measured from the fixture: the centre play button of the poster
REAL_BUTTON = (1477, 1332)
# what the code used to click; must never be chosen again
OLD_BAD_CLICK = (887, 840)

fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def main() -> int:
    if not os.path.exists(FIXTURE):
        print("SKIP: fixture missing:", FIXTURE)
        return 0
    os.makedirs(WORK, exist_ok=True)

    import auto_course as ac
    from PIL import Image

    # keep this test out of the live run's log (the runner may be running right now
    # and its log is its own record of what it did)
    ac.LOG = os.path.join(WORK, "test.log")

    print("=========== play button geometry ===========")
    rect = ac.video_rect(FIXTURE)
    point = ac.poster_play_point(FIXTURE)
    print(f"    video box = {rect}   play point = {point}")
    check("video box measured", bool(rect), str(rect))
    if rect:
        x, y, w, h = rect
        check("box is player-shaped", 0.9 <= w / max(h, 1) <= 3.2, f"{w}x{h}")
        check("box sits inside the left pane", x + w < 0.75 * 3840,
              f"right edge {x + w}")
    check("play point on the real button", point and dist(point, REAL_BUTTON) <= 40,
          f"{point} vs hand-measured {REAL_BUTTON}")
    check("play point is not the old bad click",
          point and dist(point, OLD_BAD_CLICK) > 300,
          f"distance from {OLD_BAD_CLICK}: {dist(point, OLD_BAD_CLICK):.0f}px")
    check("play_button_point prefers the measured box",
          ac.play_button_point(img=FIXTURE)[0] == point)

    # the same page at half resolution: the answer must scale with it, which is
    # what "no hardcoded pixel constants" means in practice
    half = os.path.join(WORK, "half.png")
    Image.open(FIXTURE).resize((1920, 1080)).save(half)
    half_point = ac.poster_play_point(half)
    print(f"    half-scale (1920x1080) play point = {half_point}")
    check("half-scale point is the halved button",
          half_point and dist(half_point, (REAL_BUTTON[0] // 2, REAL_BUTTON[1] // 2)) <= 30,
          str(half_point))

    print("=========== white-slide fallback ===========")
    # 5.3 showed a lecture over a white background: 75% of the player was brighter
    # than the background threshold, the scan found no block, and the play click
    # fell back to a stale screen fraction. The layout does not change within a
    # run, so the last good box must be reused instead.
    from PIL import ImageDraw

    white = Image.open(FIXTURE).copy()
    ImageDraw.Draw(white).rectangle(
        [rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3]], fill=(252, 252, 252))
    white_path = os.path.join(WORK, "white_slide.png")
    white.save(white_path)
    check("a white slide defeats the scan",
          ac.measure_video_rect(Image.open(white_path)) is None)
    ac.video_rect(FIXTURE)                    # remember a good box of this size
    check("the last good box is reused for it", ac.video_rect(white_path) == rect,
          str(ac.video_rect(white_path)))

    print("=========== start-or-leave-it-alone ===========")
    # A click on the player body while it PLAYS pauses it, so ensure_playing may only
    # click when the centre play button (or the control bar) is actually visible.
    check("the poster's play button is seen",
          ac.centre_play_button(FIXTURE, rect)[0] is True,
          str(ac.centre_play_button(FIXTURE, rect)))
    check("a white slide does not fake a play button",
          ac.centre_play_button(white_path, rect)[0] is False,
          str(ac.centre_play_button(white_path, rect)))
    playing = Image.open(FIXTURE).copy()
    ImageDraw.Draw(playing).rectangle(
        [rect[0] + rect[2] // 2 - 200, rect[1] + rect[3] // 2 - 200,
         rect[0] + rect[2] // 2 + 200, rect[1] + rect[3] // 2 + 200], fill=(40, 90, 60))
    playing_path = os.path.join(WORK, "playing.png")
    playing.save(playing_path)
    check("a playing frame shows no play button",
          ac.centre_play_button(playing_path, rect)[0] is False,
          str(ac.centre_play_button(playing_path, rect)))

    print("=========== motion detection ===========")
    a = ac.video_crop(FIXTURE, rect, "test_a")
    b = ac.video_crop(FIXTURE, rect, "test_b")
    check("identical frames are static", not ac.video_is_moving(a, b))

    moved = Image.open(FIXTURE).copy()
    px = moved.load()
    for yy in range(rect[1] + 40, rect[1] + 140):
        for xx in range(rect[0] + 40, rect[0] + 240):
            px[xx, yy] = (0, 0, 0)
    moved_path = os.path.join(WORK, "moved.png")
    moved.save(moved_path)
    c = ac.video_crop(moved_path, rect, "test_c")
    check("a changed frame is moving", ac.video_is_moving(b, c))

    print("=========== page reading on the fixture ===========")
    import ocr_click
    png = os.path.join(WORK, "fixture_5_2.png")
    Image.open(FIXTURE).save(png)
    words = ocr_click.run_ocr(png, os.path.join(WORK, "fixture_5_2.json"))["words"]
    print(f"    OCR words = {len(words)}")
    check("no false quiz on a video page", ac.detect_quiz(words)[0] is False,
          str(ac.detect_quiz(words)))
    check("task point not done", ac.taskpoint_done(words) is False)
    title = ac.course_title(words)
    check("section title reads 认清伪科学", title == "认清伪科学", repr(title))
    nums = [r["num"] for r in ac.catalog_rows(words)]
    print(f"    catalog numbers = {nums}")
    check("catalog covers 5.1-5.3 and 6.5",
          all(n in nums for n in ("5.1", "5.2", "5.3", "6.5")), str(nums))

    print("=========== quiz option rows (real 5.5 frame) ===========")
    # On this frame the task point was already complete, and the page badges ABOVE the
    # player (the "1 / 2 作业" tab pill and the green 任务点已完成 check) were a bigger,
    # better-spaced "pair" than the real options. Every answer was clicked into page
    # chrome, nothing was selected, the platform said 回答错误, and the runner read
    # that as "the model was wrong" -- burning both option letters on a question the
    # model had answered correctly.
    quiz_fixture = os.path.join(HERE, "testdata", "quiz_5_5.png")
    if not os.path.exists(quiz_fixture):
        print("SKIP: fixture missing:", quiz_fixture)
    else:
        qpng = os.path.join(WORK, "quiz_5_5.png")
        shutil.copyfile(quiz_fixture, qpng)
        qwords = ocr_click.run_ocr(qpng, os.path.join(WORK, "quiz_5_5.json"))["words"]
        banner = ac.quiz_banner_y(qwords)
        rows, disc_x = ac.quiz_option_rows(qwords, qpng)
        print(f"    banner y={banner} disc_x={disc_x} rows="
              f"{[(r['letter'], r['x'], r['cy'], r['text']) for r in rows]}")
        check("two option rows found", len(rows) == 2, str(rows))
        if len(rows) == 2:
            check("options are below the banner",
                  bool(banner) and all(r["cy"] > banner for r in rows), f"banner={banner}")
            check("options are inside the video box",
                  all(rect[0] < r["x"] < rect[0] + rect[2]
                      and rect[1] < r["cy"] < rect[1] + rect[3] for r in rows),
                  str(rect))
            check("options carry their labels",
                  [r["text"] for r in rows] == ["对", "错"],
                  str([r["text"] for r in rows]))
            check("the two rows are a plausible stack",
                  60 <= rows[1]["cy"] - rows[0]["cy"] <= 300,
                  str(rows[1]["cy"] - rows[0]["cy"]))

    print("=========== section id from a lossy URL read ===========")
    # Both strings are real OCR output from this machine. The second one is a
    # navigation that HAD happened: Windows OCR dropped the whole
    # "ng.com/mycourse/studentstudy?chapterId" chunk from the full-width bar, the
    # id could not be read, and the runner reported a successful click as a failure.
    url_with_token = ("https://moocl.chaoxing.com/mycourse/studentstudy?chapterld="
                      "1234567890&course|d：1234567892&clazzid=1234567893&cpi：1234567894")
    url_token_lost = ("令Chttps://mooc1.chaoxi=1234567891&courseld=1234567892"
                      "&clazzid=1234567893&cpi=1234567894&enc=abc123def456abc1")
    check("chapterId token survives -> id read",
          ac.parse_chapter_id(url_with_token) == "1234567890",
          str(ac.parse_chapter_id(url_with_token)))
    check("chapterId token lost -> id still read",
          ac.parse_chapter_id(url_token_lost) == "1234567891",
          str(ac.parse_chapter_id(url_token_lost)))
    check("a URL without an id reads as None",
          ac.parse_chapter_id("https://moocl.chaoxing.com/mycourse/studentstudy?courseld=1")
          is None)
    check("the full bar is still available as a fallback",
          ac.url_band(left_only=False)[2] > ac.url_band(left_only=True)[2],
          f"{ac.url_band(True)} vs {ac.url_band(False)}")

    print("=========== white slide hiding the player (real 6.4 frame) ===========")
    # The slide is white, so the scan found no player block and locked onto the red
    # "?" graphic INSIDE the slide (13% of the player's area); its centre is white,
    # so the play-button test passed and four clicks went into the picture.
    slide_fixture = os.path.join(HERE, "testdata", "slide_6_4.png")
    if not os.path.exists(slide_fixture):
        print("SKIP: fixture missing:", slide_fixture)
    else:
        player_6_4 = (264, 776, 2208, 1240)
        slide_im = Image.open(slide_fixture)
        raw = ac.measure_video_rect(slide_im)
        print(f"    raw scan = {raw}   real player = {player_6_4}")
        check("the raw scan locks onto content on a white slide",
              raw != player_6_4, str(raw))
        if raw:
            check("...and that content is much smaller than the player",
                  (raw[2] * raw[3]) < 0.5 * (player_6_4[2] * player_6_4[3]),
                  f"{raw[2] * raw[3]} vs {player_6_4[2] * player_6_4[3]}")
            check("the graphic is not mistaken for a play button",
                  ac.centre_play_button(slide_fixture, raw)[0] is False,
                  str(ac.centre_play_button(slide_fixture, raw)))
        ac._VIDEO_RECT.update(size=slide_im.size, box=player_6_4)
        check("the known player box survives the bad scan",
              ac.video_rect(slide_fixture) == player_6_4,
              str(ac.video_rect(slide_fixture)))
        check("slide content at the player centre is not a play button",
              ac.centre_play_button(slide_fixture, player_6_4)[0] is False,
              str(ac.centre_play_button(slide_fixture, player_6_4)))
        ac._VIDEO_RECT.update(size=slide_im.size, box=rect)   # restore for later parts

    print("=========== catalog numbers as separate OCR tokens ===========")
    # Live shape on the 6.x page: the number arrives as '6' at x=2827 and '1' at
    # x=2857 (with a stray '·' token below), and sometimes pre-merged as '63'. The
    # calibrated number column had gone stale (x~3028), so every row was filtered
    # out and ADVANCE parked with "0 rows visible". Positions are derived from the
    # live screen size, like the code does.
    sw, sh = ac.mouse_click.screen_size()
    nx = int(sw * 0.736)
    base_y = int(sh * 0.307)
    step = int(sh * 0.046)
    shapes = [("6", "1", "数据收集与保存之道"), ("6", "2", "诚实精准地记录数据"),
              ("63", None, "数据与研究的真实性"), ("6", "4", "谁的数据")]
    band = []
    for i, (d1, d2, title) in enumerate(shapes):
        y = base_y + i * step
        band.append({"text": d1, "x": nx, "y": y, "w": 17, "h": 28})
        if d2:
            band.append({"text": d2, "x": nx + 30, "y": y, "w": 15, "h": 28})
            band.append({"text": "·", "x": nx + 20, "y": y + 23, "w": 5, "h": 5})
        band += [{"text": title[0], "x": nx + 58, "y": y, "w": 35, "h": 34},
                 {"text": title[1], "x": nx + 93, "y": y, "w": 35, "h": 34}]
    # a yellow task-point badge: a lone digit far right, must never become a row
    band.append({"text": "2", "x": int(sw * 0.962), "y": base_y + 200, "w": 44, "h": 47})
    rows = ac.catalog_rows(band)
    nums = [r["num"] for r in rows]
    print(f"    column = {ac.catalog_num_column(ac._merge_digits(band))}   rows = {nums}")
    check("split digits merge into a row number", "6.1" in nums, str(nums))
    check("the pre-merged two-digit shape is read too", "6.3" in nums, str(nums))
    check("the number column is measured from the frame",
          abs(ac.catalog_num_column(ac._merge_digits(band))[0] - nx) < 40,
          str(ac.catalog_num_column(ac._merge_digits(band))))
    check("a lone badge digit is not a row", all(not n.endswith(".2") or n == "6.2" for n in nums),
          str(nums))
    nxt, _max = ac.resolve_next({"catalog": rows, "title": ""}, "6.1")
    check("the next section is the following one, not a jump", (nxt or {}).get("num") == "6.2",
          str(nxt))

    # Chapter 8: OCR reads its '8' as '&' ("&1"), so no number matched, the chapter
    # was invisible, and the run parked with "no section after 7.5" while 8.1/8.2 sat
    # in the catalog, in the OCR text and on screen.
    band8 = []
    for i, (d1, d2, title) in enumerate([("&", "1", "纽伦堡法典"), ("&", "2", "知情同意原则"),
                                         ("&", "3", "第三"), ("&", "4", "第四")]):
        y = base_y + i * step
        band8 += [{"text": d1, "x": nx, "y": y, "w": 26, "h": 28},
                  {"text": d2, "x": nx + 31, "y": y, "w": 15, "h": 28},
                  {"text": title[0], "x": nx + 58, "y": y, "w": 35, "h": 34},
                  {"text": title[1], "x": nx + 93, "y": y, "w": 35, "h": 34}]
    rows8 = ac.catalog_rows(band8)
    nums8 = [r["num"] for r in rows8]
    print(f"    chapter-8 rows = {nums8}")
    check("an '&' read for '8' becomes 8.x", "8.1" in nums8, str(nums8))
    nxt8, max8 = ac.resolve_next({"catalog": rows8, "title": ""}, "7.5")
    check("a chapter boundary is crossed", (nxt8 or {}).get("num") == "8.1", str(nums8))
    # NOTE: the older assertions here said "nothing after 8.4 => the course is over".
    # That was the bug: it declared the course complete at 8.2 while 8.3 existed. The
    # end is now the hardcoded LAST_SECTION, checked in its own section below.

    print("=========== a real button with little contrast (7.1 frame) ===========")
    # The 7.1 poster's play button stands only +16 over a pale background, so the
    # first version of the test (threshold +18) refused to click it: the run polled a
    # never-started video for 25 minutes and then parked. Both real buttons must pass
    # and both fakes must still fail.
    poster_7_1 = os.path.join(HERE, "testdata", "poster_7_1.png")
    if not os.path.exists(poster_7_1):
        print("SKIP: fixture missing:", poster_7_1)
    else:
        ok_71, why_71 = ac.centre_play_button(poster_7_1, (264, 776, 2208, 1240))
        check("a low-contrast real button is still recognised", ok_71 is True, why_71)

    # ...and the third palette: a GREY button on a black frame (7.3). Whiteness
    # thresholds had to go entirely -- each new section brought a new palette
    # (white on photo, white on pale blue, grey on black) and broke the previous
    # tuning, at 25 minutes of silent stall per miss.
    poster_7_3 = os.path.join(HERE, "testdata", "poster_7_3.png")
    if not os.path.exists(poster_7_3):
        print("SKIP: fixture missing:", poster_7_3)
    else:
        ok_73, why_73 = ac.centre_play_button(poster_7_3, (264, 776, 2208, 1240))
        check("a grey button on black is recognised too", ok_73 is True, why_73)

    print("=========== timecode split into OCR pieces (real 7.1 tokens) ===========")
    # On the live 7.1 page the label "8:38 / 11:1x" arrived as seven tokens --
    # '8' '：' '38' '/' '11' '．' '1' -- with the '：' and '．' 7 px lower, plus a stray
    # '0' further along the same visual row. No token contained "M:SS", so the
    # timecode looked absent: a PAUSED video with its control bar up was reported as
    # playing and the run polled it for 25 minutes instead of pressing play.
    sw2, sh2 = ac.mouse_click.screen_size()
    y = int(sh2 * 0.8977)
    pieces = [(0.11667, "8", 17), (0.12161, "：", 5), (0.12370, "38", 35),
              (0.13542, "/", 10), (0.14115, "11", 26), (0.14974, "．", 5),
              (0.15703, "1", 10)]
    row = [{"text": t, "x": int(sw2 * fx), "y": y + (7 if t in ("：", "．") else 0),
            "w": w, "h": 24} for fx, t, w in pieces]
    row.append({"text": "0", "x": int(sw2 * 0.565), "y": y - 9, "w": 56, "h": 42})
    tc = ac.find_timecode(row)
    print(f"    rebuilt = {tc}")
    check("a split timecode is rebuilt", bool(tc) and "38" in tc["text"], str(tc))
    check("the label starts at the timecode",
          bool(tc) and abs(tc["x"] - int(sw2 * 0.11667)) < 30, str(tc and tc["x"]))
    check("a stray numeral does not widen the label",
          bool(tc) and tc["w"] < int(sw2 * 0.08), str(tc and tc["w"]))
    check("the play button is placed left of the label",
          bool(tc) and ac.find_play_button(row)[0] < tc["x"],
          str(ac.find_play_button(row)))

    # The Windows taskbar clock is a perfect timecode match. Without the player box
    # limiting the search, the run derived a "play button" in the taskbar -- measured
    # live: '23:23' at x=3722 -> target (3607,2079), i.e. the system tray.
    player_box = (264, 776, 2208, 1240)
    clock = [{"text": "23:23", "x": int(sw2 * 0.969), "y": int(sh2 * 0.992),
              "w": 60, "h": 30}]
    check("a taskbar clock is not the player's timecode",
          ac.find_timecode(clock, within=player_box) is None,
          str(ac.find_timecode(clock, within=player_box)))
    check("the same clock is not a play target either",
          ac.find_play_button(clock, within=player_box) is None)
    check("without a player box it is still found",
          ac.find_timecode(clock) is not None)

    print("=========== the end of the course is a constant ===========")
    # The catalog cannot answer "is the course over": the scroll search declared it
    # finished at 8.2 while 8.3 existed, and the operator's refresh trick then revealed
    # 9.1..9.5 as well. So the end is hardcoded, and "no row after the cursor" means
    # the read was incomplete.
    check("the last section is 20.5", ac.LAST_SECTION == "20.5", ac.LAST_SECTION)
    check("20.5 is the end", ac.is_last_section("20.5") is True)
    check("8.3 is not the end", ac.is_last_section("8.3") is False)
    check("an empty cursor is not the end", ac.is_last_section("") is False)
    seq = [{"num": n, "title": "t", "y": i * 80, "cy": i * 80, "x0": 0}
           for i, n in enumerate(["20.4", "20.5", "20.6"])]
    nxt_cap, _ = ac.resolve_next({"catalog": seq, "title": ""}, "20.4")
    check("it never steps past 20.5", (nxt_cap or {}).get("num") == "20.5", str(nxt_cap))
    nxt_after, _ = ac.resolve_next({"catalog": seq, "title": ""}, "20.5")
    check("nothing follows 20.5", nxt_after is None, str(nxt_after))

    print("=========== chapter 10+ catalog numbers ===========")
    # Real frame from the run that PARKED at 9.5: the catalog plainly showed 10.1..12.4,
    # but a single-digit `\d\.\d` rule could not see any of them, so the run reported
    # "no section after 9.5" while the next section was on screen. OCR splits the chapter
    # number into two tokens ('1' '0') and puts the separator ~19px lower.
    ten_x = os.path.join(HERE, "testdata", "catalog_10x.png")
    if not os.path.exists(ten_x):
        print("SKIP: testdata/catalog_10x.png missing")
    else:
        import ocr_click
        w10 = ocr_click.run_ocr(ten_x, os.path.join(HERE, "_dbg", "test", "10x.json"))["words"]
        rows10 = ac.catalog_rows(w10)
        nums10 = [r["num"] for r in rows10]
        for want in ("9.4", "9.5", "10.1", "10.2", "10.3", "10.4", "10.5",
                     "11.1", "11.5", "12.1", "12.4"):
            check(f"{want} is parsed from the real chapter-10 catalog", want in nums10,
                  f"parsed: {nums10}")
        nxt10, mx10 = ac.resolve_next({"catalog": rows10, "title": ""}, "9.5")
        check("after 9.5 the next section is 10.1", (nxt10 or {}).get("num") == "10.1",
              f"got {nxt10 and nxt10['num']} (largest seen {mx10})")
        nxt11, _ = ac.resolve_next({"catalog": rows10, "title": ""}, "10.1")
        check("after 10.1 the next section is 10.2 (no skipping)",
              (nxt11 or {}).get("num") == "10.2", f"got {nxt11 and nxt11['num']}")

    print("=========== the tab's audio indicator ===========")
    # The operator's rule: the speaker shows on the course tab exactly while the video
    # plays. Both references are real crops of the same 58x54 slot (idle holds the
    # title's first characters, playing holds the speaker).
    idle_ref = os.path.join(HERE, "testdata", "tab_idle.png")
    play_ref = os.path.join(HERE, "testdata", "tab_playing.png")
    if not (os.path.exists(idle_ref) and os.path.exists(play_ref)):
        print("SKIP: tab fixtures missing")
    else:
        got_idle, why_idle = ac.classify_tab_slot(Image.open(idle_ref))
        got_play, why_play = ac.classify_tab_slot(Image.open(play_ref))
        print(f"    idle -> {got_idle} ({why_idle})")
        print(f"    playing -> {got_play} ({why_play})")
        check("the idle slot reads as not playing", got_idle is False, why_idle)
        check("the speaker slot reads as playing", got_play is True, why_play)
        blank = Image.new("L", Image.open(idle_ref).size, 128)
        check("an unknown slot is reported as unknown, not guessed",
              ac.classify_tab_slot(blank)[0] is None, str(ac.classify_tab_slot(blank)))

    print("=========== the play retry budget ===========")
    # PLAY is re-entered on every poll, so a counter that is not reset on success is spent
    # by the time a click genuinely fails -- measured live: "[PLAY] attempt 11" while the
    # video was simply playing, which meant the first real failure escalated straight to
    # STUCK instead of retrying three times.
    import runner_states as rs
    import runner_states_impl as impl

    class _PlayStub:
        def __init__(self, seq):
            self.seq = list(seq)
            self.lines = []
        def ensure_playing(self, words=None):
            return self.seq.pop(0) if self.seq else (False, "exhausted")
        def log(self, m):
            self.lines.append(m)

    c = rs.Ctx()
    c.ac = _PlayStub([(True, "fine")] * 3)
    c.poll = 0.01
    for _ in range(3):
        impl.st_play(c)
    check("a healthy poll does not spend the play budget", c.play_attempts == 0,
          f"play_attempts={c.play_attempts}")
    c.ac.seq = [(False, "the picture never moved")]
    st = impl.st_play(c)
    check("the first failed click retries instead of escalating",
          st is rs.State.OBSERVE and c.play_attempts == 1,
          f"->{st.name} play_attempts={c.play_attempts}")
    st = impl.st_play(c)
    st = impl.st_play(c)
    check("three consecutive failures still escalate to STUCK", st is rs.State.STUCK,
          f"->{st.name} play_attempts={c.play_attempts}")
    c.ac.seq = [(True, "fine")]
    impl.st_play(c)
    check("recovery resets the budget again", c.play_attempts == 0,
          f"play_attempts={c.play_attempts}")

    print("=========== answering: model only, never a rule ===========")
    opts = [{"letter": "A", "text": "对"}, {"letter": "B", "text": "错"}]
    import llm_answer

    calls = []

    def fake(question, options, kind="判断题", reject=(), timeout=40.0, model=None):
        calls.append(tuple(reject))
        return {"answer": "B", "confidence": 0.9, "reason": "stub", "model": "stub"}

    real = llm_answer.llm_answer
    llm_answer.llm_answer = fake
    try:
        pick, note = ac.choose_answer("判断题", "这是一道离线测试用的题目，不会被缓存。", opts)
        check("model answer accepted", pick == "B", f"pick={pick}")
        # the platform refused B: the retry must not come back with B
        pick2, note2 = ac.choose_answer("判断题", "这是一道离线测试用的题目，不会被缓存。",
                                        opts, reject=("B",))
        check("a refused letter is never submitted again", pick2 is None, f"pick={pick2}")
        check("the model was told what was refused", set(calls[-1]) == {"B"}, str(calls))

        def broken(*a, **k):
            return {"answer": None, "error": "stub failure"}

        llm_answer.llm_answer = broken
        pick3, note3 = ac.choose_answer("判断题", "另一道离线题目：模型不可用时也不能猜。", opts)
        check("no rule fallback when the model fails", pick3 is None, f"pick={pick3}")
        check("failure is reported as such", "refusing to guess" in note3, note3)
    finally:
        llm_answer.llm_answer = real

    print()
    print("RESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    code = main()
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(code)
