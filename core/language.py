"""
core/language.py — which language to answer in, for any language, offline.

Speech arrives with the language it was heard in. Typed words, a phone message
and a plugin's request arrived with nothing, and the model then took the
language from memory or settings: typed English questions were answered in
Turkish 8 times in 102 turns, an English "summarize <link>" was summarized in
Turkish (bench 2026-10-25). With the language NAMED in the request, 0.

    written(text)        the language `text` is written in, "" when unsure
    note(code, words)    a language the user just WROTE in (typed, phone)
    heard(code, p, text) a language speech was recognised in -> the language
                         to answer this turn in, and whether to recognise it
                         again in that language (see "THE CONVERSATION'S
                         LANGUAGE" below)
    current()            the conversation's language
    reply_language(words) written(words), else the conversation's, else the
                         interface language the user chose
    name(code)           "German", "Japanese" ... for a prompt

THE CONVERSATION'S LANGUAGE (2026-10-27)
    One language per conversation, kept between sessions, changed only on
    real evidence. Measured on the subscription's speech server (bench
    lang_flip.py, 105 recognitions, 5 languages, 3 noise levels): Whisper
    names the wrong language for 36 of them - one-word replies ("Ja" ->
    English, "Evet" -> English "Have it"), short commands with a brand name
    ("Spiel Spotify" -> English 0.91, "Ouvre Spotify" -> English 0.97), and
    sentences with loanwords ("Check mal meine E-Mails" -> English "Check my
    minor emails") - while a real change of language ("Can you answer me in
    English from now on?") was named at 0.98-0.998 with many words. The old
    rule switched on 0.65 and 12 characters: a German speaker was answered in
    English after "Spiel Spotify" (members, 2026-10-27).
    Now a sentence in another language counts only when it is sure
    (_SURE_P) AND long enough (_SURE_WORDS); it is answered in that language,
    but the conversation moves to it only when the next one is in it too (a
    German speaker naming an English song gets that one answer in English,
    and "Lauter bitte" after it is German again). Anything less is recognised
    again in the conversation's language - the words come out right as well
    ("Check mal meine e-mails", "Evet"). Replayed on the same 105: wrong
    reply language 34 -> 0.
    Before any sure sentence (a new install) the conversation's language is
    the interface's, held loosely: never forced on the user's words, and
    their first sentence of a few words (_LOOSE_P, _FIRST_WORDS) in another
    language takes over at once.

The identifier is py3langid (97 languages, deterministic, under a millisecond
a message, ~0.5 s and ~75 MB to load - once, see warm()). Below _MIN it
abstains: on short real messages every wrong guess ("ok", "merci", "open
Spotify", "summarize") scored 0.19 or less, every right one 0.30 or more;
and the first language must be 1.3 times as likely as the second (34
sentences, 26 languages: 32 named right, 0 wrong, only near neighbours held).
"""
from __future__ import annotations

import re
import threading

_MIN = 0.30
_MARGIN = 1.3          # the first language this many times as likely as the second
_SURE_P = 0.95         # speech in another language counts from this probability...
_SURE_WORDS = 3        # ...and this many words (every measured flip had fewer, or less)
_AGAIN_P = 0.90        # the next sentence in that language, however short, moves the conversation
_FIRST_P = 0.80        # with nothing sure yet, a clear sentence sets the language at once
_FIRST_WORDS = 2
_LOOSE_P = 0.50        # ...and over the interface's language, which nobody has said yet
_WRITE_WORDS = 3       # typed words in another language move the conversation from this many
_LINKS = re.compile(r"(?:https?://|www\.)\S+|\S+@\S+")
_lock = threading.Lock()
_ident = None
_failed = False
_current = ""
_sure = False          # the conversation's language was confirmed by the user's own words
_pending = ""          # another language heard surely once; the next sentence decides
_loaded = False


def _identifier():
    global _ident, _failed
    if _ident is None and not _failed:
        with _lock:
            if _ident is None and not _failed:
                try:
                    from py3langid.langid import MODEL_FILE, LanguageIdentifier
                    _ident = LanguageIdentifier.from_model_file(MODEL_FILE, norm_probs=True)
                except Exception as e:
                    _failed = True
                    print(f"[Language] the language of typed text cannot be told here: {type(e).__name__}")
    return _ident


def warm() -> None:
    """Load the identifier now (a background thread at start), not on the
    first typed message."""
    _identifier()


def ready() -> bool:
    """The identifier is loaded (or cannot be): asking now does not wait."""
    return _ident is not None or _failed


def written(text: str) -> str:
    """The ISO 639-1 code of the language `text` is written in, "" when unsure.
    Links and addresses are not words of any language and are left out."""
    words = _LINKS.sub(" ", str(text or ""))
    if not re.search(r"[^\W\d_]", words):
        return ""                                    # no letters: numbers, emoji, a link
    ident = _identifier()
    if ident is None:
        return ""
    try:
        ranked = ident.rank(words)[:2]
    except Exception:
        return ""
    if not ranked:
        return ""
    (code, prob), runner = ranked[0], (ranked[1][1] if len(ranked) > 1 else 0.0)
    # Sure, and clearly ahead: near neighbours (Indonesian 0.46 / Malay 0.53)
    # are not named - "Malay" for Indonesian words got Malaysian lines.
    return str(code) if prob >= _MIN and prob >= _MARGIN * runner else ""


def plausible(words: str, code: str, top: int = 5) -> bool:
    """Could `words` be in language `code`? Among the identifier's `top`
    candidates for them, even when it is unsure: Turkish is 3rd for
    "listeme 2 kilo domates ekle", Spanish 19th - a Spanish reply to it is not
    plausible. True when there is nothing to judge by."""
    code = str(code or "").split("-")[0].lower()
    text = _LINKS.sub(" ", str(words or ""))
    if not code or not re.search(r"[^\W\d_]", text):
        return True
    ident = _identifier()
    if ident is None:
        return True
    try:
        return code in [c for c, _ in ident.rank(text)[:top]]
    except Exception:
        return True


def _word_count(text: str) -> int:
    return len(re.findall(r"[^\W\d_]+", _LINKS.sub(" ", str(text or ""))))


def _load() -> None:
    """The conversation's language as the last session left it."""
    global _loaded, _current, _sure
    if _loaded:
        return
    _loaded = True
    try:
        from memory.config_manager import load_api_keys
        saved = str(load_api_keys().get("conversation_language") or "").strip().lower()
    except Exception:
        saved = ""
    if saved and not _current:
        _current, _sure = saved, True


def _set(code: str, sure: bool = True) -> None:
    global _current, _sure, _pending
    code = str(code or "").strip().lower()
    if not code:
        return
    changed = code != _current
    _current, _pending = code, ""
    _sure = _sure or sure if not changed else sure
    if changed and sure:
        try:
            from memory.config_manager import update_config
            update_config(conversation_language=code)
        except Exception:
            pass


def note(code: str, words: int | str | None = None) -> None:
    """The user just WROTE in `code` (typed, a phone message). Writing is
    deliberate, but "ok" or "open Spotify" says little: the conversation
    moves only for _WRITE_WORDS words or more (`words`: a count or the text),
    or when nothing is sure yet."""
    _load()
    code = str(code or "").strip().lower()
    if not code or code == _current:
        return
    n = _word_count(words) if isinstance(words, str) else (_WRITE_WORDS if words is None else int(words))
    if n >= _WRITE_WORDS or not _sure:
        _set(code)


def heard(code: str, probability: float, text: str) -> tuple[str, bool]:
    """Speech was recognised as `code` with `probability` ("text" = its
    words). Returns (the language to answer this turn in, recognise again in
    it?) and moves the conversation's language when the evidence says so -
    see "THE CONVERSATION'S LANGUAGE" above."""
    global _pending, _sure
    _load()
    code = str(code or "").strip().lower()
    p = float(probability or 0.0)
    n = _word_count(text)
    if not _current:
        base = interface()
        if not base:
            if code:
                _set(code, sure=p >= _FIRST_P and n >= _FIRST_WORDS)
            return code, False
        _set(base, sure=False)
    if not code or code == _current:
        _pending = ""
        if code and p >= _FIRST_P and n >= _FIRST_WORDS:
            _sure = True
        return _current, False
    if not _sure:
        # Nothing the user said has confirmed it yet (a new install: the
        # interface's language). A guess is never forced on their words: a
        # Spanish question recognised again as English came out as English
        # ("What time will tomorrow in Madrid?") and held the conversation
        # there (bench 2026-10-27). Their own sentence takes over.
        if p >= _LOOSE_P and n >= _FIRST_WORDS:
            _set(code, sure=p >= _FIRST_P or n >= _SURE_WORDS)
        return code, False
    if p >= _SURE_P and n >= _SURE_WORDS or (_pending == code and p >= _AGAIN_P):
        if _pending == code:
            print(f"[Language] the conversation moves to {code} ({p:.0%}, {n} words, twice)")
            _set(code)
        else:
            _pending = code
            print(f"[Language] this sentence is {code} ({p:.0%}, {n} words); "
                  f"the conversation stays {_current} unless the next one is too")
        return code, False
    print(f"[Language] heard as {code} ({p:.0%}, {n} words) - taken as {_current}, recognised again")
    return _current, True


def current() -> str:
    _load()
    return _current


def reset() -> None:
    """Forget the conversation's language (tests; nothing in the app)."""
    global _current, _sure, _pending, _loaded
    _current, _sure, _pending, _loaded = "", False, "", True


def interface() -> str:
    try:
        from core import i18n
        return str(i18n.current() or "").split("-")[0].lower()
    except Exception:
        return ""


def reply_language(words: str = "") -> str:
    """The language to answer `words` in, "" when it is better not to name one.

    Theirs when it can be told; else the conversation's; else - only when there
    is next to nothing to read ("ok", "summarize <link>") - the interface's.
    Real words the identifier cannot place ("listeme 2 kilo domates ekle": no
    letter only one language has) name nothing: the model reads them better
    than a guess, and the interface's language there was a wrong one."""
    found = written(words) or current()
    if found:
        return found
    return interface() if len(_LINKS.sub(" ", str(words or "")).split()) <= 2 else ""


def name(code: str) -> str:
    """The English name of a language code, for a prompt; the ISO code itself
    (which a model reads as well) when there is no name for it."""
    code = str(code or "").strip()
    if not code:
        return ""
    found = ""
    try:
        from core.i18n import english_name
        found = english_name(code) or ""
        if not found or found.lower() == code.lower():
            from core.speech_languages import english_name as speech_name
            found = speech_name(code) or ""
    except Exception:
        found = ""
    if not found or found.lower() == code.lower():
        return f"the language with ISO code '{code}'"
    return found
