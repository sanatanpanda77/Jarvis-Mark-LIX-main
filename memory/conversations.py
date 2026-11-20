"""
memory/conversations.py — what was said, kept on this computer so JARVIS can
recall it later ("what was that book I mentioned last week?").

    long_term.json keeps FACTS ("sister: Ayşe"). It never kept what was talked
    about, and the session recap the briefing uses is consumed once and gone.
    This keeps the conversation itself, and a short summary of each stretch of
    it, searchable by words and by date.

FILES (all under memory/conversations/, never published — see .gitignore)
    YYYY-MM-DD.jsonl   one line per exchange: {"t": "14:05", "u": ..., "a": ...}
    episodes.jsonl     one line per summarised stretch:
                       {"date", "from", "to", "summary", "turns"}

PRIVACY
    Plain text on this computer only; nothing is uploaded. Off switch:
    conversation_memory.enabled in the settings (SETUP → CONVERSATION MEMORY,
    or by voice). Days older than conversation_memory.days (90) are deleted at
    start-up. forget() deletes a range or everything.

SEARCH
    Local and instant: words are folded (case, accents, Turkish ı/ş/ğ…) and
    matched by their first letters, so "kitap" finds "kitabı" and "books"
    finds "book". Summaries count more than single lines. Optional dates narrow
    it; the model turns "last week" into dates itself.

Writing happens on one background thread: the voice loop only drops a line in
a queue.
"""
from __future__ import annotations

import json
import queue
import re
import shutil
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

DIR = Path(__file__).resolve().parent / "conversations"
EPISODES = "episodes.jsonl"
_LINE_MAX = 1200
_OUT_MAX = 2600

_q: "queue.Queue[tuple[str, dict]]" = queue.Queue()
_writer: Optional[threading.Thread] = None
_file_lock = threading.Lock()
_settings = {"at": 0.0, "value": {"enabled": True, "days": 90}}


def settings() -> dict:
    """The switch and retention, re-read at most every 10 s."""
    now = time.monotonic()
    if now - _settings["at"] > 10:
        try:
            from memory.config_manager import get_conversation_memory
            _settings["value"] = get_conversation_memory()
        except Exception:
            pass
        _settings["at"] = now
    return _settings["value"]


def refresh_settings() -> None:
    _settings["at"] = 0.0


def enabled() -> bool:
    return bool(settings().get("enabled", True))


# ── writing ──────────────────────────────────────────────────────────────────

def _write_loop() -> None:
    while True:
        name, row = _q.get()
        try:
            with _file_lock:
                DIR.mkdir(parents=True, exist_ok=True)
                with open(DIR / name, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[Conversations] could not save: {type(e).__name__}: {e}")
        finally:
            _q.task_done()


def _put(name: str, row: dict) -> None:
    global _writer
    if _writer is None:
        _writer = threading.Thread(target=_write_loop, name="conversations", daemon=True)
        _writer.start()
    _q.put((name, row))


def flush(timeout: float = 3.0) -> None:
    """Wait until everything queued is on disk (shutdown, tests)."""
    end = time.monotonic() + timeout
    while _q.unfinished_tasks and time.monotonic() < end:
        time.sleep(0.02)


def record(user: str = "", assistant: str = "", when: Optional[datetime] = None) -> None:
    """One exchange. Either side may be empty (JARVIS speaking unprompted)."""
    user, assistant = (user or "").strip(), (assistant or "").strip()
    if not (user or assistant) or not enabled():
        return
    when = when or datetime.now()
    row = {"t": when.strftime("%H:%M")}
    if user:
        row["u"] = user[:_LINE_MAX]
    if assistant:
        row["a"] = assistant[:_LINE_MAX]
    _put(f"{when:%Y-%m-%d}.jsonl", row)


def add_episode(summary: str, start: datetime, end: datetime, turns: int) -> None:
    summary = " ".join((summary or "").split())
    if not summary or not enabled():
        return
    _put(EPISODES, {"date": f"{start:%Y-%m-%d}", "from": f"{start:%H:%M}",
                    "to": f"{end:%H:%M}", "summary": summary[:600], "turns": int(turns)})


# ── reading ──────────────────────────────────────────────────────────────────

def _read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        out.append(row)
                except ValueError:
                    continue                     # a half-written last line
    except FileNotFoundError:
        pass
    return out


def episodes() -> list[dict]:
    return _read_jsonl(DIR / EPISODES)


def recent_episodes(n: int = 3) -> list[dict]:
    return episodes()[-n:]


def days() -> list[date]:
    out = []
    if DIR.exists():
        for p in DIR.glob("*.jsonl"):
            try:
                out.append(datetime.strptime(p.stem, "%Y-%m-%d").date())
            except ValueError:
                continue
    return sorted(out)


def _parse_day(text) -> Optional[date]:
    text = str(text or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


_FOLD = str.maketrans({"ı": "i", "İ": "i", "ş": "s", "ğ": "g", "ç": "c", "ö": "o", "ü": "u",
                       "ß": "ss", "æ": "ae", "ø": "o", "œ": "oe"})
_STOP_WORDS = """
the a an and or of to in on at for is are was were be been it this that with what
which who whom about from my your me you i we our did do does have has had can could
would should will not no yes so as by how when where why there their they them he she
his her its just like then than also some any all more most very really thing things
remember said say told talk talked mention mentioned earlier last week yesterday today
bir ve ile de da mi mu mı mü ne ki bu su şu o ben sen biz siz onlar icin için gibi
daha cok çok var yok ama fakat hani neydi nedir dedim demistim demiştim konustuk
konuşmuştuk konusmustuk bahsettim bahsetmistim bahsetmiştim gecen geçen hafta dun dün
bugun bugün once önce sonra simdi şimdi
"""


def _fold(text: str) -> str:
    text = str(text or "").translate(_FOLD).casefold()
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


_STOP = {_fold(w) for w in _STOP_WORDS.split()}


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]{2,}", _fold(text))


def _stem(word: str) -> str:
    """The part of a word its other forms share: kitap/kitabı -> "kita",
    book/books -> "book". Short words must match whole."""
    if len(word) <= 4:
        return word
    return word[:max(4, len(word) - 3)]


def _terms(query: str) -> list[str]:
    seen, out = set(), []
    for w in _words(query):
        if w in _STOP or len(w) < 3:
            continue
        s = _stem(w)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _hits(terms: list[str], text: str) -> int:
    words = _words(text)
    n = 0
    for t in terms:
        n += any(w == t or (len(t) >= 4 and w.startswith(t)) for w in words)
    return n


def search(query: str = "", date_from=None, date_to=None, limit: int = 6,
           name: str = "JARVIS") -> str:
    """What was said about `query` (between the dates, inclusive), as text for
    the model. With no query: the summaries of that time."""
    if not enabled() and not days():
        return "Conversation memory is switched off, so nothing has been kept."
    d_from, d_to = _parse_day(date_from), _parse_day(date_to)
    if d_from and d_to and d_from > d_to:
        d_from, d_to = d_to, d_from

    def in_range(d: Optional[date]) -> bool:
        return d is not None and (d_from is None or d >= d_from) and (d_to is None or d <= d_to)

    terms = _terms(query)
    eps = [e for e in episodes() if in_range(_parse_day(e.get("date")))]
    span = ("" if not (d_from or d_to) else
            f" between {d_from or 'the start'} and {d_to or 'today'}")

    if not terms:
        if eps:
            rows = [f"{e['date']} {e.get('from', '')}-{e.get('to', '')}: {e['summary']}"
                    for e in eps[-12:]]
            return _cut("Conversations" + span + ":\n" + "\n".join(rows))
        lines = []
        for d in [d for d in days() if in_range(d)][-3:]:
            rows = _read_jsonl(DIR / f"{d}.jsonl")
            for r in rows[:4]:
                lines.append(_fmt(d, r, name))
        if not lines:
            return f"Nothing was said{span or ' yet'} that I kept."
        return _cut("Some of what was said" + span + ":\n" + "\n".join(lines))

    scored: list[tuple[float, str, str]] = []
    today = date.today()
    for e in eps:
        h = _hits(terms, e.get("summary", ""))
        if h:
            d = _parse_day(e.get("date")) or today
            scored.append((h * 3 + _recency(d, today), f"{e['date']}",
                           f"{e['date']} {e.get('from', '')}-{e.get('to', '')} (summary): {e['summary']}"))
    for d in days():
        if not in_range(d):
            continue
        for r in _read_jsonl(DIR / f"{d}.jsonl"):
            text = f"{r.get('u', '')} {r.get('a', '')}"
            h = _hits(terms, text)
            if h:
                scored.append((h * 2 + _recency(d, today) + (0.5 if _hits(terms, r.get("u", "")) else 0),
                               f"{d} {r.get('t', '')}", _fmt(d, r, name)))
    if not scored:
        return (f"Nothing kept{span} mentions {query!r}. "
                f"(Conversations are kept for {settings().get('days', 90)} days.)")
    scored.sort(key=lambda s: s[0], reverse=True)
    best = sorted(scored[:max(1, limit)], key=lambda s: s[1])
    return _cut(f"What was said about {query!r}{span}, oldest first:\n"
                + "\n".join(s[2] for s in best))


def _recency(d: date, today: date) -> float:
    return max(0.0, 1.0 - (today - d).days / 60.0)


def _fmt(d: date, r: dict, name: str) -> str:
    parts = []
    if r.get("u"):
        parts.append(f"User: {_clip(r['u'], 280)}")
    if r.get("a"):
        parts.append(f"{name}: {_clip(r['a'], 280)}")
    return f"{d} {r.get('t', '')} — " + " / ".join(parts)


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _cut(text: str) -> str:
    return text if len(text) <= _OUT_MAX else text[:_OUT_MAX - 1] + "…"


# ── the prompt's short block ─────────────────────────────────────────────────

def prompt_block(n: int = 3, chars: int = 420) -> str:
    """The last few summaries, for the system prompt: enough for JARVIS to know
    what it talked about lately, small enough to ride in every session."""
    if not enabled():
        return ""
    eps = recent_episodes(n)
    if not eps:
        return ""
    rows, used = [], 0
    for e in reversed(eps):
        row = f"- {e.get('date', '')} {e.get('from', '')}: {_clip(e.get('summary', ''), 200)}"
        if used + len(row) > chars:
            break
        rows.insert(0, row)
        used += len(row)
    return ("[RECENT CONVERSATIONS] (summaries; recall_conversation finds the rest)\n"
            + "\n".join(rows) + "\n")


# ── housekeeping ─────────────────────────────────────────────────────────────

def prune(keep_days: Optional[int] = None) -> int:
    """Delete days older than the retention. Returns how many files went."""
    keep = int(keep_days or settings().get("days", 90))
    cutoff = date.today() - timedelta(days=keep)
    gone = 0
    with _file_lock:
        for d in days():
            if d < cutoff:
                try:
                    (DIR / f"{d}.jsonl").unlink()
                    gone += 1
                except OSError:
                    pass
        eps = _read_jsonl(DIR / EPISODES)
        kept = [e for e in eps if (_parse_day(e.get("date")) or date.today()) >= cutoff]
        if len(kept) != len(eps):
            _rewrite(DIR / EPISODES, kept)
    return gone


def _rewrite(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def forget(date_from=None, date_to=None, everything: bool = False) -> str:
    """Delete what was kept: everything, or the days in a range."""
    flush()
    with _file_lock:
        if everything:
            if DIR.exists():
                shutil.rmtree(DIR, ignore_errors=True)
            return "Every kept conversation has been deleted."
        d_from, d_to = _parse_day(date_from), _parse_day(date_to)
        if not d_from and not d_to:
            return "Say which days to forget, or everything."
        d_from, d_to = d_from or date.min, d_to or date.max
        if d_from > d_to:
            d_from, d_to = d_to, d_from
        n = 0
        for d in days():
            if d_from <= d <= d_to:
                try:
                    (DIR / f"{d}.jsonl").unlink()
                    n += 1
                except OSError:
                    pass
        eps = _read_jsonl(DIR / EPISODES)
        kept = [e for e in eps if not (d_from <= (_parse_day(e.get("date")) or date.min) <= d_to)]
        if len(kept) != len(eps):
            _rewrite(DIR / EPISODES, kept)
    return f"Deleted the conversations of {n} day(s)." if n or len(kept) != len(eps) \
        else "Nothing was kept for those days."


def stats() -> dict:
    ds = days()
    return {"enabled": enabled(), "days_kept": settings().get("days", 90),
            "days_with_conversations": len(ds),
            "first": str(ds[0]) if ds else "", "summaries": len(episodes())}
