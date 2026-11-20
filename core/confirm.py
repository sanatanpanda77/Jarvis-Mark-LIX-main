"""
core/confirm.py — a confirmation the model cannot forge.

THE PROBLEM WITH THE OLD GATE
    computer_settings guarded shutdown and restart like this:

        confirmed = str(params.get("confirmed", "")).lower()
        if confirmed not in ("yes", "true", "1", "confirm"):
            return "Please confirm by calling again with confirmed=yes."

    `confirmed` is a tool parameter, which means the *model* writes it. Nothing
    stops it from sending confirmed=yes on the first call, and nothing checks
    that a human was ever involved. It is a convention, not a gate — and its
    coverage was two actions, so deleting files and switching off the WiFi the
    assistant is talking over went through with no gate at all.

THE DESIGN HERE
    The confirmation token is issued by the *interface*, never by the model:

      1. An action calls `request(...)` with a callable that does the real work.
      2. This module hands the UI a banner with CONFIRM / CANCEL and returns
         IMMEDIATELY with a sentence for the model to say out loud.
      3. If — and only if — the user presses CONFIRM, the UI calls `resolve()`,
         which runs the stored callable off the Qt thread.

    Nothing blocks. The model keeps talking while the banner is up, so this
    costs no latency at all; in fact it is cheaper than the old gate, which
    burned two tool round trips (reject, then re-call) on every shutdown.

WHAT BELONGS HERE AND WHAT DOES NOT
    Only genuinely irreversible things. Anything that can be reversed should be
    done at once and pushed onto core/undo.py instead — undo is faster than a
    question, and an assistant that asks before every action is one nobody uses.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

# A pending confirmation is abandoned after this long. Chosen to outlast a
# normal "hang on, let me look at the screen" pause without leaving a live
# shutdown button sitting on the HUD for the rest of the day.
TIMEOUT_SECONDS = 90.0


@dataclass
class _Pending:
    key:     str
    title:   str
    detail:  str
    run:     Callable[[], str]
    at:      float
    # Plugins written for Torvis (the same file runs in both) pass these:
    # what to do with the result once it ran, and what to do when it is
    # cancelled, expires or cannot be asked - a caller waiting on the answer
    # (plugins/screen_agent) is released at once.
    report:    Optional[Callable[[str], None]] = None
    on_cancel: Optional[Callable[[], None]] = None


_pending: Optional[_Pending] = None
_lock = threading.Lock()

# Set once at startup by main.py. Signature: (title, detail) -> None for show,
# and () -> None for hide. Both are marshalled onto the Qt thread by the UI.
_show_cb: Optional[Callable[[str, str], None]] = None
_hide_cb: Optional[Callable[[], None]] = None
_log_cb:  Optional[Callable[[str], None]] = None


def bind(show, hide, log=None) -> None:
    """Wire this module to the HUD. Called once from main.py at startup."""
    global _show_cb, _hide_cb, _log_cb
    _show_cb, _hide_cb, _log_cb = show, hide, log


def _log(msg: str) -> None:
    if _log_cb:
        try:
            _log_cb(msg)
        except Exception:
            pass


def _call(fn, *args) -> None:
    if fn is None:
        return
    try:
        fn(*args)
    except Exception as e:
        print(f"[Confirm] callback failed: {type(e).__name__}: {e}")


def request(key: str, title: str, detail: str, run: Callable[[], str],
            report: Optional[Callable[[str], None]] = None,
            on_cancel: Optional[Callable[[], None]] = None,
            always: Optional[Callable[[], None]] = None,
            on_behalf: bool = False) -> str:
    """Park an irreversible action behind the on-screen gate.

    `report(result)` receives what `run` returned once it ran; `on_cancel()`
    is called when it is cancelled, expires or cannot be asked. `always` and
    `on_behalf` are accepted for plugins written for Torvis: here every such
    step is simply asked, as a plain confirmation.

    Returns the sentence the tool should hand back to the model — phrased as an
    instruction so the assistant asks the user out loud in their own language,
    rather than reading an English string verbatim."""
    global _pending

    if _show_cb is None:
        # No interface bound (headless, or a very early call). Refuse rather
        # than silently performing something irreversible.
        _call(on_cancel)
        return (f"I cannot confirm '{title}' right now because the interface is "
                f"not available, so I have not done it.")

    with _lock:
        _pending = _Pending(key=key, title=title, detail=detail,
                            run=run, at=time.monotonic(),
                            report=report, on_cancel=on_cancel)

    try:
        _show_cb(title, detail)
    except Exception as e:
        with _lock:
            _pending = None
        _call(on_cancel)
        return f"Could not ask for confirmation: {e}. Nothing was done."

    _log(f"SYS: Awaiting confirmation — {title}")
    return (
        f"[CONFIRMATION_PENDING] I have put a confirmation on screen for: {title}. "
        f"Say ONE short sentence in the user's own language telling them you need "
        f"them to confirm it on the HUD before you do it. Do not claim it is done."
    )


def resolve(accepted: bool) -> None:
    """Called by the UI when the user presses CONFIRM or CANCEL.

    Runs the stored callable on a worker thread — this is invoked from the Qt
    thread, and shutting the machine down from inside a button handler would
    freeze the interface on its way out."""
    global _pending

    with _lock:
        p, _pending = _pending, None

    if _hide_cb:
        try:
            _hide_cb()
        except Exception:
            pass

    if p is None:
        return

    if time.monotonic() - p.at > TIMEOUT_SECONDS:
        _log(f"SYS: Confirmation expired — {p.title}")
        _call(p.on_cancel)
        return

    if not accepted:
        _log(f"SYS: Cancelled — {p.title}")
        _call(p.on_cancel)
        return

    def _worker():
        try:
            result = p.run() or "Done."
            _log(f"SYS: Confirmed — {p.title}. {result}")
        except Exception as e:
            result = f"It failed: {e}"
            _log(f"ERR: {p.title} failed — {e}")
        _call(p.report, str(result))

    threading.Thread(target=_worker, daemon=True,
                     name=f"confirm-{p.key}").start()


def pending_title() -> str:
    """'' when nothing is waiting. Lets an action avoid stacking two banners."""
    with _lock:
        if _pending is None:
            return ""
        if time.monotonic() - _pending.at > TIMEOUT_SECONDS:
            return ""
        return _pending.title


def withdraw(key: str) -> bool:
    """Take a waiting question of this `key` off the screen without running
    it (its asker stopped). True when there was one."""
    global _pending
    with _lock:
        if _pending is None or _pending.key != key:
            return False
        _pending = None
    if _hide_cb:
        try:
            _hide_cb()
        except Exception:
            pass
    return True


def pending_since() -> Optional[float]:
    """When (time.monotonic) the waiting question was put on screen, or None.
    An answer that is not a press - a gesture - must have begun after it."""
    with _lock:
        if _pending is None or time.monotonic() - _pending.at > TIMEOUT_SECONDS:
            return None
        return _pending.at
