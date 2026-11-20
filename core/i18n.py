"""
core/i18n.py — interface sentences for plugins, under the names they use.

    from core.i18n import t
    t("confirm.send_email", to="Ana")      -> "Send an email to Ana"

Jarvis's own interface is in English. Plugins written for Torvis (the same
plugin file runs in both) ask for their sentences by key; the keys they use
are in locales/en.json, and a key that is not there comes back as itself —
visible, so a missing sentence is reported rather than hidden.

current() is the user's chosen interface language (config `ui_language`),
English when none was chosen. Plugins use it to answer in that language.
"""
from __future__ import annotations

import json
import string
from functools import lru_cache
from pathlib import Path

SOURCE = "en"
_LOCALES = Path(__file__).resolve().parent.parent / "locales"


class _Holes(dict):
    """format_map that leaves `{unknown}` as it is instead of raising."""
    def __missing__(self, key):
        return "{" + key + "}"


@lru_cache(maxsize=None)
def _strings(tag: str) -> dict:
    try:
        data = json.loads((_LOCALES / f"{tag}.json").read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if isinstance(v, str) and not k.startswith("_")}
    except (OSError, ValueError):
        return {}


def source_strings() -> dict[str, str]:
    return _strings(SOURCE)


def current() -> str:
    """The interface language the user chose, as a tag ("en", "de"...)."""
    try:
        from memory.config_manager import load_api_keys
        tag = str(load_api_keys().get("ui_language") or "").strip()
    except Exception:
        tag = ""
    return tag.split("-")[0].lower() or SOURCE


def english_name(tag: str | None = None) -> str:
    """The language's English name ("de" -> "German")."""
    tag = (tag or current()).strip() or SOURCE
    try:
        from babel import Locale
        return Locale.parse(tag.replace("-", "_")).get_display_name("en").split(" (")[0]
    except Exception:
        return {"en": "English", "de": "German", "es": "Spanish", "fr": "French",
                "it": "Italian", "nl": "Dutch", "pl": "Polish", "pt": "Portuguese",
                "tr": "Turkish"}.get(tag.split("-")[0].lower(), tag)


def lookup(key: str, tag: str | None = None) -> str | None:
    tag = (tag or current()).lower()
    return _strings(tag).get(key) or source_strings().get(key)


def t(key: str, /, **values) -> str:
    """The sentence for `key`; English when the language has none; the key
    itself when nobody has it."""
    text = lookup(key) or key
    if values:
        try:
            text = string.Formatter().vformat(text, (), _Holes(values))
        except Exception:
            pass
    return text


def phrase(text: str, tag: str | None = None) -> str:
    """An English phrase in the interface language. Jarvis has no catalog of
    free phrases, so it stays English (always readable)."""
    return text
