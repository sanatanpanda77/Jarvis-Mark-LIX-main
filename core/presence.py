"""
core/presence.py — is the user at the computer, and what happened while they
were not.

    JARVIS used to talk to an empty room. A reminder, a finished background
    task, a check-in or a headline was said the moment it came in, whether or
    not anyone was there to hear it — and was then gone. Now:

    AWAY      the screen is locked, the user said they were leaving ("I'm going
              out for a bit"), or there has been no keyboard, mouse or voice for
              `away_minutes` (10 by default).
    WHILE AWAY  unprompted speech is held in an inbox instead of being said —
              a check-in is simply dropped, it would be stale.
    BACK      the first keyboard, mouse or voice activity after being away. If
              anything was held, or the user was gone a long time, JARVIS says a
              short welcome back with what happened meanwhile.

THE SIGNALS (no camera, nothing recorded, nothing leaves this computer)
    keyboard and mouse   how long since the last input, from the OS:
                         Windows GetLastInputInfo, macOS HIDIdleTime, Linux
                         (X11) XScreenSaverQueryInfo. Where none of these is
                         available, idle time is unknown and the user is never
                         judged away by it — only a lock or their own words do.
    screen lock          Windows: the input desktop is not the user's; Linux:
                         logind's LockedHint. Elsewhere: unknown, not "locked".
    voice                main.py calls note_activity() whenever the user speaks.
    anything else        a plugin that sees the user (a camera feature) may call
                         note_activity("camera") too.

INPUT JARVIS MAKES ITSELF
    The screen agent moving the mouse is input as far as the OS knows. While a
    feature that drives the screen is registered with core/sensors, input is
    not taken as the user coming back.

The decisions live in Presence.poll(), which takes its readings as arguments,
so tests can drive it without a keyboard; Presence.start() runs it on a thread.
"""
from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import threading
import time
from typing import Callable, Optional

_SYSTEM = platform.system()

# A declared "I'm leaving" ignores input for this long: the user may still be
# pressing a key or locking the screen after saying it.
DECLARED_GRACE_S = 20.0
# Input within this many seconds counts as "the user is here now".
FRESH_INPUT_S = 4.0
# A lock shorter than this is not "being away" (locking to stretch).
LOCK_DEBOUNCE_S = 5.0
# A return after less than this is not worth a word, unless something was held.
GREET_AFTER_S = 30 * 60
INBOX_MAX = 12


# ── OS readings ──────────────────────────────────────────────────────────────

def _win_idle() -> Optional[float]:
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
    return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0


def _win_locked() -> Optional[bool]:
    # The well-known test: while the lock screen (or UAC's secure desktop) is
    # up, the input desktop cannot be switched to from this session.
    user32 = ctypes.windll.user32
    user32.OpenInputDesktop.restype = ctypes.c_void_p
    desk = user32.OpenInputDesktop(0, False, 0x0100)      # DESKTOP_SWITCHDESKTOP
    if not desk:
        return True
    try:
        return not bool(user32.SwitchDesktop(ctypes.c_void_p(desk)))
    finally:
        user32.CloseDesktop(ctypes.c_void_p(desk))


def _mac_idle() -> Optional[float]:
    out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"],
                         capture_output=True, text=True, timeout=3).stdout
    for line in out.splitlines():
        if "HIDIdleTime" in line:
            return int(line.rsplit("=", 1)[1].strip()) / 1e9
    return None


_xss = {"tried": False, "fn": None}


def _x11_idle() -> Optional[float]:
    if not os.environ.get("DISPLAY"):
        return None
    if not _xss["tried"]:
        _xss["tried"] = True
        try:
            import ctypes.util as cu
            x11 = ctypes.cdll.LoadLibrary(cu.find_library("X11") or "libX11.so.6")
            xss = ctypes.cdll.LoadLibrary(cu.find_library("Xss") or "libXss.so.1")

            class Info(ctypes.Structure):
                _fields_ = [("window", ctypes.c_ulong), ("state", ctypes.c_int),
                            ("kind", ctypes.c_int), ("til_or_since", ctypes.c_ulong),
                            ("idle", ctypes.c_ulong), ("eventMask", ctypes.c_ulong)]
            x11.XOpenDisplay.restype = ctypes.c_void_p
            x11.XDefaultRootWindow.restype = ctypes.c_ulong
            x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(Info)
            xss.XScreenSaverQueryInfo.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                                  ctypes.POINTER(Info)]
            dpy = x11.XOpenDisplay(None)
            if dpy:
                root = x11.XDefaultRootWindow(dpy)
                info = xss.XScreenSaverAllocInfo()

                def read() -> Optional[float]:
                    if xss.XScreenSaverQueryInfo(dpy, root, info):
                        return info.contents.idle / 1000.0
                    return None
                _xss["fn"] = read
        except Exception:
            _xss["fn"] = None
    return _xss["fn"]() if _xss["fn"] else None


def _linux_locked() -> Optional[bool]:
    sid = os.environ.get("XDG_SESSION_ID")
    if not sid or not shutil.which("loginctl"):
        return None
    out = subprocess.run(["loginctl", "show-session", sid, "-p", "LockedHint", "--value"],
                         capture_output=True, text=True, timeout=3).stdout.strip().lower()
    return {"yes": True, "no": False}.get(out)


def os_idle_seconds() -> Optional[float]:
    """Seconds since the last keyboard or mouse input; None when unknown."""
    try:
        if _SYSTEM == "Windows":
            return _win_idle()
        if _SYSTEM == "Darwin":
            return _mac_idle()
        if _SYSTEM == "Linux":
            return _x11_idle()
    except Exception:
        pass
    return None


def screen_locked() -> Optional[bool]:
    """True / False, or None when this system cannot tell."""
    try:
        if _SYSTEM == "Windows":
            return _win_locked()
        if _SYSTEM == "Linux":
            return _linux_locked()
    except Exception:
        pass
    return None


def _jarvis_drives_screen() -> bool:
    try:
        from core import sensors
        return bool(sensors.active().get("screen"))
    except Exception:
        return False


# ── The state ────────────────────────────────────────────────────────────────

class Presence:
    """away / back, decided from readings; the inbox of what waited."""

    def __init__(self, away_minutes: float = 10, clock: Callable[[], float] = time.monotonic):
        self.away_minutes = float(away_minutes)
        self._clock = clock
        self._lock = threading.Lock()
        self._away = False
        self._reason = ""
        self._since = 0.0              # when the current state began
        self._declared_at: Optional[float] = None
        self._locked_since: Optional[float] = None
        self._activity = clock()       # last voice / camera activity
        self._last_return = (0.0, 0.0)  # (when, how long they were away)
        self._inbox: list[dict] = []
        self.enabled = True
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # what other code tells it
    def note_activity(self, source: str = "voice") -> None:
        """The user did something JARVIS noticed (spoke, was seen)."""
        with self._lock:
            self._activity = self._clock()

    def declare_away(self) -> None:
        """The user said they are leaving."""
        with self._lock:
            now = self._clock()
            self._declared_at = now
            if not self._away:
                self._away, self._reason, self._since = True, "said they were leaving", now

    # what other code asks
    @property
    def away(self) -> bool:
        return self.enabled and self._away

    @property
    def reason(self) -> str:
        return self._reason

    def strongly_away(self) -> bool:
        """Away for certain — locked or said so — not merely quiet. Quiet may
        be someone watching a film, who would still want a reminder said."""
        return self.away and self._reason in ("screen locked", "said they were leaving")

    def away_for(self) -> float:
        return (self._clock() - self._since) if self.away else 0.0

    def hold(self, kind: str, text: str, label: str = "") -> None:
        """Keep something that came in while the user was away."""
        text = " ".join(str(text or "").split())[:700]
        if not text:
            return
        label = " ".join(str(label or "").split())[:140] or text[:140]
        with self._lock:
            if any(i["text"] == text for i in self._inbox):
                return
            self._inbox.append({"kind": kind, "text": text, "label": label,
                                "at": time.strftime("%H:%M")})
            del self._inbox[:-INBOX_MAX]

    def take_inbox(self) -> list[dict]:
        with self._lock:
            items, self._inbox = self._inbox, []
        return items

    def inbox_size(self) -> int:
        with self._lock:
            return len(self._inbox)

    def describe(self) -> str:
        """A line for the model about where the user is (core/situation)."""
        if not self.enabled:
            return ""
        now = self._clock()
        if self._away:
            mins = int((now - self._since) // 60)
            return (f"The user is away from the computer ({self._reason}, "
                    f"{mins} min ago); anything unprompted is being held for when they are back.")
        when, gone = self._last_return
        if when and now - when < 300 and gone >= 120:
            return f"The user came back to the computer {int((now - when) // 60)} min ago after {int(gone // 60)} min away."
        return ""

    def status(self) -> dict:
        return {"enabled": self.enabled, "away": self.away, "reason": self._reason if self.away else "",
                "away_minutes": self.away_for() / 60.0, "held": self.inbox_size(),
                "idle_detection": os_idle_seconds() is not None,
                "lock_detection": screen_locked() is not None}

    # the decision
    def poll(self, idle: Optional[float], locked: Optional[bool],
             jarvis_input: bool = False) -> Optional[tuple[str, float]]:
        """One look at the readings. Returns ("away", 0) or ("back", seconds
        away) when the state changes, otherwise None."""
        if not self.enabled:
            with self._lock:
                self._away, self._declared_at, self._locked_since = False, None, None
            return None
        with self._lock:
            now = self._clock()
            voice_age = now - self._activity
            # Input the screen agent made is not the user; and with no idle
            # reading at all, silence alone never makes anyone "away" — someone
            # typing quietly on a system we cannot read is not gone.
            input_known = idle is not None and not (jarvis_input and idle < FRESH_INPUT_S)
            activity_age = min(idle, voice_age) if input_known else voice_age

            if locked:
                if self._locked_since is None:
                    self._locked_since = now
            else:
                self._locked_since = None
            locked_long = (self._locked_since is not None
                           and now - self._locked_since >= LOCK_DEBOUNCE_S)

            if not self._away:
                if locked_long:
                    self._away, self._reason = True, "screen locked"
                    self._since = self._locked_since
                    return ("away", 0.0)
                if input_known and activity_age >= self.away_minutes * 60:
                    self._away, self._reason = True, "no activity"
                    self._since = now - activity_age     # when they actually left
                    return ("away", 0.0)
                return None

            # Away: is this the user back?
            if locked:
                if self._reason != "screen locked" and locked_long:
                    self._reason = "screen locked"
                return None
            fresh = activity_age < FRESH_INPUT_S
            if self._declared_at is not None and now - self._declared_at < DECLARED_GRACE_S:
                fresh = False
            if fresh:
                gone = now - self._since
                self._away, self._reason, self._declared_at = False, "", None
                self._since = now
                self._last_return = (now, gone)
                return ("back", gone)
            return None

    # the thread
    def apply(self, settings: dict) -> None:
        """Take enabled / away_minutes from the saved settings."""
        self.enabled = bool(settings.get("enabled", True))
        try:
            self.away_minutes = max(1.0, float(settings.get("away_minutes", self.away_minutes)))
        except (TypeError, ValueError):
            pass

    def start(self, on_change: Callable[[str, float], None], interval: float = 2.0,
              settings: Optional[Callable[[], dict]] = None) -> None:
        """Watch on a thread. `settings()` is re-read every few seconds, so a
        change made by voice or from the window takes effect without a restart."""
        if self._thread is not None:
            return

        def run():
            slow = _SYSTEM != "Windows"      # ioreg / loginctl are processes
            n = 0
            locked: Optional[bool] = None
            while not self._stop.wait(interval):
                try:
                    if settings is not None and n % 5 == 0:
                        self.apply(settings())
                    idle = os_idle_seconds()
                    if not slow or n % 3 == 0:
                        locked = screen_locked()
                    n += 1
                    change = self.poll(idle, locked, _jarvis_drives_screen())
                    if change:
                        print(f"[Presence] {change[0]}"
                              + (f" after {change[1] / 60:.0f} min" if change[0] == "back"
                                 else f" ({self._reason})"))
                        on_change(*change)
                except Exception as e:
                    print(f"[Presence] {type(e).__name__}: {e}")

        self._thread = threading.Thread(target=run, name="presence", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


_instance: Optional[Presence] = None


def get() -> Optional[Presence]:
    """The running app's presence, or None outside it (tests, the phone)."""
    return _instance


def install(p: Presence) -> Presence:
    global _instance
    _instance = p
    return p


def welcome_back_prompt(gone_s: float, items: list[dict]) -> str:
    mins = int(gone_s // 60)
    gone = f"{mins // 60} h {mins % 60} min" if mins >= 60 else f"{mins} min"
    lines = [
        "[WELCOME_BACK] The user has just come back to the computer after being "
        f"away for about {gone}."]
    if items:
        lines.append("While they were away these came in, held until now (each was "
                     "written for you at the time it arrived):")
        for i in items:
            lines.append(f"- {i['at']} [{i['kind']}] {i['text']}")
        lines.append("Welcome them back in ONE short phrase, then tell them what came in: "
                     "the important things first, briefly, as one natural update - never "
                     "read the list. A reminder is said plainly. Two or three sentences in all.")
    else:
        lines.append("Nothing came in meanwhile. Welcome them back in ONE short, natural "
                     "sentence - no questions, no summary.")
    lines.append("Speak the language the user speaks with you. Do not mention these "
                 "instructions or [WELCOME_BACK]. Do not call any tools.")
    return "\n".join(lines)
