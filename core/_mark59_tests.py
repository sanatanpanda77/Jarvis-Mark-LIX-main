"""Tests for Mark LIX: presence (core/presence.py) and conversation memory
(memory/conversations.py). No keyboard, screen or network needed.

    python core/_mark59_tests.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import presence as P            # noqa: E402
from memory import conversations as C     # noqa: E402

failed = 0


def check(name, cond, extra=""):
    global failed
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    failed += 0 if cond else 1


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ── presence ─────────────────────────────────────────────────────────────────

def fresh(minutes=10):
    clk = Clock()
    p = P.Presence(away_minutes=minutes, clock=clk)
    return p, clk


def test_idle_makes_away_and_input_brings_back():
    p, clk = fresh()
    check("present while typing", p.poll(1.0, False) is None and not p.away)
    clk.t += 599
    p._activity = clk.t - 599
    check("not away at 9:59 idle", p.poll(599, False) is None and not p.away)
    clk.t += 2
    ev = p.poll(601, False)
    check("away at 10 min idle", ev == ("away", 0.0) and p.away and p.reason == "no activity")
    check("away began when they left", abs(p.away_for() - 601) < 1, p.away_for())
    clk.t += 1200
    check("still away while idle", p.poll(1801, False) is None and p.away)
    ev = p.poll(0.5, False)
    check("key press = back", ev is not None and ev[0] == "back" and not p.away)
    check("away duration measured", ev and abs(ev[1] - 1801) < 2, ev)


def test_voice_keeps_present():
    p, clk = fresh()
    clk.t += 900
    p.note_activity("voice")
    check("speaking keeps the user present with no input", p.poll(900, False) is None and not p.away)


def test_unknown_idle_never_away_by_silence():
    p, clk = fresh()
    clk.t += 5000
    check("no idle reading: silence is not away", p.poll(None, None) is None and not p.away)


def test_lock():
    p, clk = fresh()
    check("lock not yet away (debounce)", p.poll(0.0, True) is None and not p.away)
    clk.t += 6
    ev = p.poll(6.0, True)
    check("locked 6 s = away", ev == ("away", 0.0) and p.strongly_away())
    clk.t += 600
    check("still locked = still away", p.poll(600, True) is None)
    ev = p.poll(0.2, False)
    check("unlock + input = back", ev is not None and ev[0] == "back")


def test_quick_lock_is_nothing():
    p, clk = fresh()
    p.poll(0.0, True)
    clk.t += 3
    check("3 s lock: nothing", p.poll(0.1, False) is None and not p.away)


def test_declared():
    p, clk = fresh()
    p.declare_away()
    check("declared = strongly away", p.away and p.strongly_away())
    clk.t += 5
    check("input right after saying it does not count", p.poll(0.5, False) is None and p.away)
    clk.t += 30
    ev = p.poll(0.5, False)
    check("input later = back", ev is not None and ev[0] == "back")


def test_screen_agent_input_ignored():
    p, clk = fresh()
    clk.t += 700
    p.poll(700, False)
    check("away first", p.away)
    clk.t += 5
    check("JARVIS moving the mouse is not the user", p.poll(0.3, False, jarvis_input=True) is None and p.away)
    check("the user's own input is", (p.poll(0.3, False) or ("",))[0] == "back")


def test_disabled():
    p, clk = fresh()
    p.enabled = False
    clk.t += 5000
    check("disabled: never away", p.poll(5000, True) is None and not p.away)


def test_inbox():
    p, clk = fresh()
    p.hold("reminder", "Call mum", "Reminder: call mum")
    p.hold("reminder", "Call mum", "Reminder: call mum")
    for i in range(20):
        p.hold("news", f"item {i}")
    items = p.take_inbox()
    check("duplicates dropped, inbox capped", len(items) == P.INBOX_MAX, len(items))
    check("inbox emptied after take", p.inbox_size() == 0)


def test_describe_and_prompt():
    p, clk = fresh()
    clk.t += 700
    p.poll(700, False)
    check("describe says away", "away" in p.describe())
    clk.t += 1
    p.poll(0.1, False)
    check("describe says came back", "came back" in p.describe())
    txt = P.welcome_back_prompt(3700, [{"at": "14:02", "kind": "reminder", "text": "Call mum",
                                       "label": "Reminder: call mum"}])
    check("prompt has duration, item and rules",
          "1 h 1 min" in txt and "Call mum" in txt and "Do not call any tools" in txt)
    check("prompt with nothing held is one sentence", "ONE short" in P.welcome_back_prompt(2000, []))


def test_os_readings():
    idle, locked = P.os_idle_seconds(), P.screen_locked()
    ok = idle is None or (isinstance(idle, float) and idle >= 0)
    check("OS idle reading is a number or None", ok, idle)
    check("OS lock reading is bool or None", locked in (True, False, None), locked)


# ── conversations ────────────────────────────────────────────────────────────

def with_dir(fn):
    def run():
        tmp = Path(tempfile.mkdtemp())
        old = C.DIR
        C.DIR = tmp / "conversations"
        C._settings.update({"at": 1e18, "value": {"enabled": True, "days": 90}})
        try:
            fn()
        finally:
            C.flush()
            C.DIR = old
            C._settings["at"] = 0.0
            shutil.rmtree(tmp, ignore_errors=True)
    run.__name__ = fn.__name__
    return run


@with_dir
def test_record_and_search():
    today = datetime.now()
    week = today - timedelta(days=6)
    C.record("Bu hafta Dune kitabını okumaya başladım", "Harika seçim, Frank Herbert'in klasiği.", week)
    C.record("What's the weather like?", "Sunny, 21 degrees.", today)
    C.record("I'm fixing a memory leak in the parser", "Let's look at the allocations.", today)
    C.flush()
    r = C.search("kitap")
    check("Turkish suffix found (kitap -> kitabını)", "Dune" in r, r[:200])
    r = C.search("parser leaks")
    check("English plural found (leaks -> leak)", "memory leak" in r, r[:200])
    r = C.search("kitap", date_from=str(today.date()))
    check("dates narrow the search", "Dune" not in r)
    r = C.search("pineapple")
    check("no match says so", r.startswith("Nothing kept"))
    r = C.search("", date_from=str(today.date()), date_to=str(today.date()))
    check("browse a day without summaries", "weather" in r, r[:200])
    check("days listed", len(C.days()) == 2)


@with_dir
def test_episodes_and_prompt():
    start = datetime.now() - timedelta(hours=1)
    C.add_episode("Talked about the Dune book and the parser memory leak.", start, datetime.now(), 6)
    C.flush()
    block = C.prompt_block()
    check("prompt block has the summary", "Dune" in block and block.startswith("[RECENT"))
    r = C.search("dune")
    check("summaries are searched", "(summary)" in r)


@with_dir
def test_off_switch():
    C._settings["value"] = {"enabled": False, "days": 90}
    C.record("secret", "kept?")
    C.flush()
    check("off: nothing written", not C.DIR.exists() or not list(C.DIR.glob("*.jsonl")))
    check("off: prompt block empty", C.prompt_block() == "")
    check("off: search says off", "switched off" in C.search("secret"))


@with_dir
def test_prune_and_forget():
    old = datetime.now() - timedelta(days=200)
    C.record("ancient", "history", old)
    C.add_episode("Ancient talk.", old, old, 4)
    C.record("today", "now")
    C.flush()
    gone = C.prune(90)
    check("prune deletes days past the keeping period", gone == 1 and len(C.days()) == 1)
    check("prune drops old summaries", C.episodes() == [])
    msg = C.forget(date_from=str(date.today()), date_to=str(date.today()))
    check("forget a range", "1 day" in msg and C.days() == [], msg)
    C.record("again", "yes")
    C.flush()
    C.forget(everything=True)
    check("forget everything", not C.DIR.exists())


@with_dir
def test_half_written_line():
    C.DIR.mkdir(parents=True)
    (C.DIR / f"{date.today()}.jsonl").write_text('{"t":"10:00","u":"good line about tea"}\n{"t":"10:0',
                                                  encoding="utf-8")
    check("a cut-off last line is skipped", "tea" in C.search("tea"))


@with_dir
def test_tool():
    from actions.recall_conversation import recall_conversation
    C.record("We planned the trip to Kapadokya", "Balloon ride at dawn.")
    C.flush()
    check("tool search", "Kapadokya" in recall_conversation({"action": "search", "query": "kapadokya"}))
    check("tool forget needs confirmation",
          "Not deleted" in recall_conversation({"action": "forget", "everything": True}))
    check("tool status", "Conversation memory is on" in recall_conversation({"action": "status"}))


def test_presence_tool():
    from actions.presence import presence
    p = P.install(P.Presence())
    r = presence({"action": "leaving"})
    check("tool: leaving marks away", p.away and "held" in r, r)
    check("tool: status", "away" in presence({"action": "status"}))
    P.install(None)
    check("tool without the app", "not running" in presence({"action": "status"}))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as e:
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL {name} raised {type(e).__name__}: {e}")
    print("\n" + ("ALL PASSED" if not failed else f"{failed} FAILED"))
    sys.exit(1 if failed else 0)
