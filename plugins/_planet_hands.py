"""
_planet_hands.py — Planet Watch's live view moved by hand: the same shared
camera and hand tracker as gesture control and the Holo Desk (_hands.py).

    point (the hand open or pointing)   a cursor moves over the view: the
                                        country under it lights up, a quake,
                                        a satellite or the ISS shows its card
    pinch and move                      take hold of the planet: the globe
                                        turns under the hand, the map slides;
                                        let go while moving and it spins on
    pinch with both hands               pull apart / together: closer / further,
                                        about the point between the hands
    a quick pinch (a tap)               what a click does: select the country
                                        (outline, flag, card), a satellite (its
                                        orbit), a quake; on the sea: let go
    pinch and hold still                drop a pin; a second pin draws the
                                        great-circle route with its distance
                                        and flight time; a third starts again
    a fist, held still for a second     clear the pins and the selection
    two fingers (index + middle), held  a ring menu at the hand: every switch of
                                        the view (layers, style, home, close) and
                                        what fits the moment (go to the selected
                                        country, follow the selected satellite,
                                        a slow spin, clear), and closer / further
                                        about that spot. Hold on an item (a ring
                                        fills), pinch, or close the hand on it.
                                        A fist in the middle or the hand far away
                                        closes it
    point (only the index out), held    zoom in towards that spot, again every
                                        second while it is held

The model is told the pins and what is selected (core/situation through the
view), so "find flights between these two" or "tell me about this country"
need no names said.

HOW IT FITS
    The tracker runs on its own thread; nothing here touches Qt from it. The
    view asks for the newest hands on its own (GUI) thread, every frame it
    paints (tick), and the hands are carried forward by their own speed to that
    moment - a 60 Hz screen moves smoothly between 30 Hz camera frames. The
    camera picture is made into a small preview once per camera frame, on the
    tracker's thread, never per repaint.
    While on, gesture control's pinch and swipes are this view's (its open palm
    still stops the assistant talking), and a held input lock (Face Lock) makes
    the hands nobody's.

SMOOTH (measured on a real recording, tools/test_planet_hands.py + the bench)
    The first version moved the globe by the cursor's step each frame, with a
    fixed smoothing: the hand's jitter shook the globe (1.8 px a frame with
    the hand held still) and the steps added up unevenly. Now:
      - each camera frame goes through a One Euro filter of its own (steady
        when the hand is still, no lag when it moves) - once per camera frame,
        never again on a repeated one (that would read as "stopped");
      - between camera frames the cursor is carried by the filtered speed;
      - a grab keeps the very spot of the planet that was grabbed under the
        cursor (no steps to add up, no drift), and a fling leaves with the
        filtered speed.

THE NUMBERS
    Thresholds are in screen fractions or hand sizes, never pixels of one
    screen. Taps, holds and throws use the same rules as the Holo Desk.
"""
from __future__ import annotations

import math
import time
from typing import Optional

import numpy as np

from core.qt.QtCore import QPointF, QRectF, Qt, QTimer
from core.qt.QtGui import QColor, QFont, QFontMetricsF, QGuiApplication, QImage, QPainter, QPen

try:
    from plugins import _hands as H
    from plugins import _planet_view as pv
except ImportError:  # loaded outside the package
    import _hands as H  # type: ignore
    import _planet_view as pv  # type: ignore

NAME = "planet_watch"
# The part of the camera picture that reaches the whole view: hands do not
# have to go to the picture's edges (where the tracker loses them) to reach
# the view's edges.
REACH = 0.62
FOLLOW = 40.0            # 1/s: the last step to where the hand is, between camera frames
# One Euro filter on the aim point, in picture fractions: the cut-off at rest
# (Hz) and how fast it opens with speed. Tuned on the real recording (bench).
SMOOTH_MIN_CUTOFF = 0.9
SMOOTH_BETA = 8.0
PREDICT_MAX = 0.06       # s a cursor is carried ahead of its last camera frame
TAP_TIME = 0.35          # s: a pinch shorter than this that did not move is a tap
STILL = 0.018            # of the view's diagonal: moved less than this is "still"
HOLD_PIN = 0.8           # s of a still pinch: a pin
FIST_HOLD = 1.0          # s of a fist held still: clear (fists are common: 700 frames in 8 minutes)
FLING_MIN = 0.25         # of the view's diagonal a second: slower is a put-down, not a fling
THROW_CAP = 3500.0       # px/s
GHOST_SPEED = 0.5        # picture widths/s: a pinch born on a hand this fast is motion blur
SEEN_FOR = 0.6           # s a hand stays on screen after it was last seen
HINT_SECONDS = 9.0
MENU_HOLD = 0.5          # s of two fingers: the menu opens
MENU_RADIUS = 118.0      # px at scale 1: the ring of the menu
MENU_IDLE = 8.0          # s without a choice: the menu closes
MENU_AWAY = 2.3          # of the radius: a hand this far from the middle closes it
POINT_HOLD = 0.8         # s of a still pointing finger: zoom in there
POINT_AGAIN = 1.0        # s between zooms while it is still held
MENU_DWELL = 0.8         # s pointing at one item: it is chosen (a ring fills)
MENU_QUIET = 0.8         # s after the menu: no tap, pin, clear or menu (the hand is still settling)
MENU_ZOOM = 4.0          # closer / further from the menu: this factor about where it was opened
NOT_A_PINCH = ("fist", "thumbs_up", "thumbs_down")   # thumb on the index, but not a pinch
PINCH_FRAMES = 2         # camera frames running before a pinch counts
RELEASE_FRAMES = 3       # camera frames open before a held pinch lets go (measured: the tracker
                         # reads a turning pinch as open for a frame or two, 24 times in 8 min)
LOST_GRACE = 0.6         # s a lost hand keeps what it holds (blur, a new number; measured 22 drops at 0.35)
ADOPT_DIST = 0.2         # of the diagonal: a new hand this close to a lost one is that hand
ZOOM_MIN_GAP = 0.12      # of the diagonal: the closest two hands count as (no explosion)
ZOOM_FOLLOW = 18.0       # 1/s: the span's approach to where the hands put it
ZOOM_RATE = 12.0         # the span changes by at most this factor a second

# Words on the screen: English here; the view's provider gives them in the
# user's language (the same once-per-language translation as its buttons).
HINTS = {"hint_point": "Point: look around", "hint_turn": "Pinch and move: turn the planet",
         "hint_zoom": "Pinch with both hands: zoom", "hint_tap": "Quick pinch: select",
         "hint_pin": "Pinch and hold: drop a pin", "hint_clear": "Fist held: clear",
         "hint_menu": "Two fingers held: menu - hold on an item to choose it"}


def _gestures_captured(on: bool) -> None:
    """While the view is moved by hand, gesture control leaves the pinch and
    the swipes alone - and gives them back only if the Holo Desk is not
    holding them too."""
    try:
        from plugins import gesture_control
    except Exception:
        return
    try:
        if on:
            gesture_control.captured.set()
            return
        try:
            from plugins import holo_desk
            if holo_desk.is_open():
                return
        except Exception:
            pass
        gesture_control.captured.clear()
    except Exception:
        pass


def _locked() -> bool:
    try:
        from core import inputlock
        return bool(inputlock.held())
    except Exception:
        return False


def _gc_km(a: tuple, b: tuple) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    c = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)
    return 6371.0 * math.acos(max(-1.0, min(1.0, c)))


class GlobeHands:
    """Hands → the PlanetWidget's own moves (_pan_pixels, _set_span_anchored,
    _point_at, _click). One per view; `start` / `stop` on the GUI thread."""

    def __init__(self, view):
        self.view = view
        self.on = False
        self.cursors: dict = {}           # hand id → {"x","y","seen","pinch","hist",...}
        self.grip: Optional[dict] = None  # one hand holding the planet
        self.zoom: Optional[dict] = None  # two hands
        self.fist_since: Optional[float] = None
        self.menu: Optional[dict] = None  # {"x","y","items","hot","hand","opened","touched"}
        self.pins: list = []              # [{"lat","lon","name"}], two at most
        self.preview: Optional[QImage] = None
        self.show_preview = True
        self.started = 0.0
        self.words = dict(HINTS)
        self.rate = [0, 0.0]               # frames painted while a hand was in view, and seconds
        self.quiet_until = 0.0
        self._dt = 1 / 60
        self._path: list = []
        self._path_for = None
        self.poll = QTimer(view)          # wakes the view when a hand comes into sight
        self.poll.setInterval(120)
        self.poll.timeout.connect(self._poll)

    # ── switching on and off (GUI thread) ───────────────────────────────────

    def start(self, show_preview: bool = True) -> tuple[bool, str]:
        if self.on:
            return True, ""
        if _locked():
            return False, "the assistant is locked"
        if not H.SERVICE.acquire(NAME):
            return False, H.SERVICE.error or "the hand models are missing"
        self.on, self.started, self.show_preview = True, time.monotonic(), show_preview
        self.cursors.clear()
        self.grip = self.zoom = None
        H.SERVICE.subscribe(self._on_camera)
        _gestures_captured(True)
        self.poll.start()
        self.view.hands_ctl = self
        try:
            self.view._submit("hands:words", getattr(self.view.provider, "labels", lambda *a: None),
                              dict(HINTS), "hand_hints")
        except Exception:
            pass
        self.view._kick()
        return True, ""

    def _log_rate(self) -> None:
        n, secs = self.rate
        if n > 30 and secs > 0:
            print(f"[PlanetHands] {n / secs:.0f} frames a second while hands were in view "
                  f"({secs:.0f} s); camera {getattr(H.SERVICE, 'fps', 0.0):.0f} a second", flush=True)
        self.rate = [0, 0.0]

    def stop(self) -> None:
        if not self.on:
            return
        self._log_rate()
        self.on = False
        self.poll.stop()
        H.SERVICE.unsubscribe(self._on_camera)
        H.SERVICE.release(NAME)
        _gestures_captured(False)
        self.cursors.clear()
        self.grip = self.zoom = None
        self.menu = None
        self.preview = None
        if getattr(self.view, "hands_ctl", None) is self:
            self.view.hands_ctl = None
        self.view.update()

    def camera_problem(self) -> str:
        return H.SERVICE.error or ""

    # ── the tracker's thread: only the preview picture ──────────────────────

    def _on_camera(self, hands, frame, t) -> None:
        if not self.show_preview or frame is None:
            return
        cv2 = H.cv2
        h, w = frame.shape[:2]
        pw = 320
        small = cv2.resize(frame, (pw, max(1, int(h * pw / w))), interpolation=cv2.INTER_AREA)
        small = (small * 0.8).astype(small.dtype)
        H.draw_hands(small, hands)
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        self.preview = QImage(rgb.data, rgb.shape[1], rgb.shape[0], 3 * rgb.shape[1],
                              QImage.Format.Format_RGB888).copy()     # one assignment: atomic

    # ── the GUI thread ──────────────────────────────────────────────────────

    def on_data(self, key: str, value) -> None:
        if key == "hands:words" and isinstance(value, dict):
            self.words = {k: str(value.get(k) or v) for k, v in HINTS.items()}
        elif key.startswith("hands:pin:") and value:
            # "the place, the country": the first and last of what the geocoder says
            parts = [x.strip() for x in str(value).split(",") if x.strip()]
            name = parts[0] if len(parts) < 2 else f"{parts[0]}, {parts[-1]}"
            for pin in self.pins:
                if pin.get("key") == key:
                    pin["name"] = name

    def _poll(self) -> None:
        try:
            hands, _f, t = H.SERVICE.latest()
            if hands or self.preview is not None:
                self.view.update()            # the view's own pacing takes it from here
        except Exception:
            pass

    def wants_frames(self) -> bool:
        now = time.monotonic()
        return self.on and (bool(self.grip or self.zoom or self.menu)
                            or any(now - c["seen"] < SEEN_FOR for c in self.cursors.values())
                            or now - self.started < HINT_SECONDS)

    def _to_view(self, ax: float, ay: float) -> tuple[float, float]:
        w, h = self.view.width(), self.view.height()
        return ((ax - 0.5) / REACH + 0.5) * w, ((ay - 0.5) / REACH + 0.5) * h

    def tick(self, now: float, dt: float) -> None:
        """Read the hands and move the view. Called from the view's paint."""
        if not self.on:
            return
        hands, _frame, t = H.SERVICE.latest()
        if _locked():
            hands = []
        if hands:
            self.rate[0] += 1
            self.rate[1] += dt
        self._dt = dt
        diag = math.hypot(self.view.width(), self.view.height())
        live = set()
        for hd in hands:
            ax, ay = hd.aim
            ft = getattr(hd, "frame_t", None) or t or now
            c = self.cursors.get(hd.id)
            if c is None:
                c = self._adopt(hd.id, self._to_view(ax, ay), now, diag)
            if c is None:
                sx, sy = self._to_view(ax, ay)
                c = self.cursors[hd.id] = {"x": sx, "y": sy, "pinch": False, "pinch_since": None,
                                           "start": None, "moved": 0.0, "pinned": False, "ghost": False,
                                           "pin_frames": 0, "ft": None, "seen": now}
                self._reseed(c, ax, ay)
            elif hd.gap > 0:
                self._reseed(c, ax, ay)               # found again: the filter starts over, the state stays
            new_frame = c["ft"] != ft
            if new_frame:                             # a new camera frame: filter it, once
                fp = c["filter"](np.array([ax, ay]), ft)
                if c["ft"] is not None and ft > c["ft"]:
                    c["fv"] = 0.5 * c["fv"] + 0.5 * (fp - c["fp"]) / (ft - c["ft"])
                c["fp"], c["ft"] = fp, ft
            lead = max(0.0, min(PREDICT_MAX, now - c["ft"]))
            sx, sy = self._to_view(*(c["fp"] + c["fv"] * lead))
            k = 1.0 - math.exp(-FOLLOW * max(1e-3, dt))
            nx, ny = c["x"] + (sx - c["x"]) * k, c["y"] + (sy - c["y"]) * k
            if hd.pose != c.get("pose"):
                c["pose_since"], c["pose_at"] = now, (nx, ny)
            c.update(x=nx, y=ny, seen=now, pose=hd.pose, size=hd.size, side=hd.side)
            # A pinch counts when the hand really pinches: not a fist or a thumb
            # whose tip happens to touch the index (measured: 327 frames of
            # "fist + pinching" in a real session), and in two camera frames
            # running. Once on it lasts while the tracker says pinching, whatever
            # the hand's shape - closing the hand a little must not drop the planet.
            if new_frame:
                real = hd.pinching and hd.pose not in NOT_A_PINCH
                c["pin_frames"] = c["pin_frames"] + 1 if real else 0
                c["off_frames"] = 0 if hd.pinching else c.get("off_frames", 0) + 1
            was = c["pinch"]
            c["pinch"] = (c.get("off_frames", 0) < RELEASE_FRAMES) if was else c["pin_frames"] >= PINCH_FRAMES
            if c["pinch"] and not was:
                c["pinch_since"], c["start"], c["moved"], c["pinned"] = now, (nx, ny), 0.0, False
                c["ghost"] = float(np.hypot(*hd.velocity)) > GHOST_SPEED or now < self.quiet_until
            elif not c["pinch"] and was:
                self._released(hd.id, c, now, diag)
            if c["pinch"] and c["start"] is not None:
                c["moved"] = max(c["moved"], math.hypot(nx - c["start"][0], ny - c["start"][1]) / diag)
            if c["ghost"] and float(np.hypot(*hd.velocity)) < 0.3 and now >= self.quiet_until:
                c["ghost"] = False
            live.add(hd.id)
        for hid in [h for h in self.cursors if h not in live]:
            c = self.cursors[hid]
            # Lost for a moment (a blur, the tracker giving the hand a new number):
            # what it holds is kept for LOST_GRACE, then let go without a fling.
            if c.get("pinch") and now - c["seen"] > LOST_GRACE:
                c["pinch"], c["start"] = False, None
                if self.grip and self.grip["hand"] == hid:
                    self.grip = None
            if now - c["seen"] > SEEN_FOR:
                del self.cursors[hid]
        self._act(now, diag, live)

    @staticmethod
    def _reseed(c: dict, ax: float, ay: float) -> None:
        c["filter"] = H.OneEuro(SMOOTH_MIN_CUTOFF, SMOOTH_BETA)
        c["fp"], c["fv"], c["ft"] = np.array([ax, ay]), np.zeros(2), None

    def _adopt(self, hid, at: tuple, now: float, diag: float) -> Optional[dict]:
        """A hand the tracker numbers anew, where a hand was lost a moment ago,
        is that hand: its grab, zoom or menu go on (measured: 76 numbers for one
        person's two hands in 8 minutes - each new one dropped the planet)."""
        best, best_d = None, ADOPT_DIST * diag
        for old, c in self.cursors.items():
            if c["seen"] >= now or now - c["seen"] > LOST_GRACE:
                continue
            d = math.hypot(c["x"] - at[0], c["y"] - at[1])
            if d < best_d:
                best, best_d = old, d
        if best is None:
            return None
        c = self.cursors.pop(best)
        self.cursors[hid] = c
        for holder in (self.grip, self.menu):
            if holder and holder.get("hand") == best:
                holder["hand"] = hid
        if self.zoom:
            self.zoom["hands"] = [hid if h == best else h for h in self.zoom.get("hands", [])]
        return c

    def _act(self, now: float, diag: float, live: set) -> None:
        v = self.view
        if self._menu(now, diag, live):
            return
        holding = {hid: c for hid, c in self.cursors.items()
                   if c.get("pinch") and not c["ghost"] and (hid in live or now - c["seen"] <= LOST_GRACE)}
        # two hands: zoom about the point between them
        if len(holding) >= 2:
            ids = (self.zoom or {}).get("hands") or list(holding)[:2]
            if not all(h in holding for h in ids):
                ids = list(holding)[:2]
            a, b = holding[ids[0]], holding[ids[1]]
            mid = ((a["x"] + b["x"]) / 2, (a["y"] + b["y"]) / 2)
            # hands that come together never make the distance (and the span) explode
            d = max(ZOOM_MIN_GAP * diag, math.hypot(a["x"] - b["x"], a["y"] - b["y"]))
            if self.zoom is None or self.zoom.get("hands") != ids:
                v._stop_motion()
                self.zoom = {"hands": ids, "d0": d, "span0": v.span, "geo": v._screen_to_geo(*mid),
                             "now": v.span}
                self.grip = None
            z = self.zoom
            target = z["span0"] * z["d0"] / d
            # the span follows smoothly, in steps of ratio (log), never faster than ZOOM_RATE
            k = 1.0 - math.exp(-ZOOM_FOLLOW * self._dt)
            step = (math.log(target) - math.log(z["now"])) * k
            limit = math.log(ZOOM_RATE) * self._dt
            z["now"] = math.exp(math.log(z["now"]) + max(-limit, min(limit, step)))
            if z["geo"] is None:
                v._set_span_anchored(z["now"], None, None)
            else:
                v._set_span_anchored(z["now"], mid, z["geo"])
            v._changed()
            for c in holding.values():
                c["start"] = None                    # a zoom is never a tap or a pin
            return
        if self.zoom is not None:
            self.zoom = None
            for c in holding.values():               # the hand still holding goes on turning from here
                c["start"], c["moved"] = None, 1.0
        # one hand: hold the planet once the pinch moved; hold still: a pin
        if holding:
            hid, c = next(iter(holding.items()))
            if self.grip is None:
                # The planet is taken where the hand IS, the moment the pinch is
                # real: waiting for the hand to move first, then catching up with
                # where it started, made every grab begin with a jump (~33 px).
                # A tap or a pin is still told by how far the hand went (moved).
                v._stop_motion()
                v.follow_until = 0.0
                self.grip = {"hand": hid, "geo": v._screen_to_geo(c["x"], c["y"]), "last": (c["x"], c["y"])}
            if self.grip["hand"] == hid and hid in live:     # lost for a moment: the planet waits
                self._hold(c["x"], c["y"])
            if (c["start"] is not None and not c["pinned"] and c["moved"] <= STILL
                    and now - c["pinch_since"] >= HOLD_PIN and now >= self.quiet_until):
                c["pinned"] = True
                self._pin(c["x"], c["y"])
        else:
            # pointing: the same hover as the mouse
            seen = [c for hid, c in self.cursors.items() if hid in live]
            if seen:
                c = max(seen, key=lambda c: c["seen"])
                v._point_at(QPointF(c["x"], c["y"]))
                self._dwell(c, now, diag)
        # a fist held still clears (not right after the menu: that fist closed it)
        fist = next((c for hid, c in self.cursors.items() if hid in live and c.get("pose") == "fist"), None)
        if fist is not None and not holding and now >= self.quiet_until:
            at = fist.get("pose_at") or (fist["x"], fist["y"])
            if math.hypot(fist["x"] - at[0], fist["y"] - at[1]) / diag > STILL * 2:
                fist["pose_since"], fist["pose_at"] = now, (fist["x"], fist["y"])
            if now - fist["pose_since"] >= FIST_HOLD:
                fist["pose_since"] = now + 3600       # once per fist
                self.pins.clear()
                v.clear_selection()

    def _dwell(self, c: dict, now: float, diag: float) -> None:
        """A pointing finger held still zooms in there - again every second."""
        if c.get("pose") != "point" or c.get("pose_since") is None or now < self.quiet_until:
            c["dwelt"] = None
            return
        px, py = c.get("pose_at") or (c["x"], c["y"])
        if math.hypot(c["x"] - px, c["y"] - py) / diag > STILL * 1.5:
            c["pose_since"], c["pose_at"], c["dwelt"] = now, (c["x"], c["y"]), None     # moved: start over
            return
        due = c["pose_since"] + POINT_HOLD if not c.get("dwelt") else c["dwelt"] + POINT_AGAIN
        if now >= due:
            c["dwelt"] = now
            self.view._zoom(0.5, QPointF(c["x"], c["y"]))

    def _menu_items(self, x: float, y: float) -> list:
        """The menu: closer / further about the spot it was opened on (one
        step each way, a factor of 4 - a few steps reach the streets), then
        everything the view itself offers (PlanetWidget.hand_menu)."""
        v = self.view
        here = QPointF(x, y)
        label = lambda key: v.labels.get(key) or key
        return ([{"key": "zin", "glyph": "+", "label": label("zin"), "on": False,
                  "run": lambda: v._zoom(1 / MENU_ZOOM, here)},
                 {"key": "zout", "glyph": "−", "label": label("zout"), "on": False,
                  "run": lambda: v._zoom(MENU_ZOOM, here)}]
                + v.hand_menu())

    def _choose(self, now: float) -> None:
        m = self.menu
        item = m["items"][m["hot"]]
        self.menu = None
        self.quiet_until = now + MENU_QUIET
        try:
            item["run"]()
        except Exception as e:
            print(f"[PlanetHands] menu: {item.get('key')}: {type(e).__name__}: {e}")

    def _menu(self, now: float, diag: float, live: set) -> bool:
        """The ring menu: opened by two fingers held where the hand is. The
        item the hand points at is chosen by holding there (MENU_DWELL, a ring
        fills), by a pinch, or by closing the hand on it. A fist in the
        middle, the hand far away or a long pause close it. True while open."""
        v = self.view
        s = max(0.72, min(1.35, v.height() / 900.0))
        if self.menu is None:
            if now < self.quiet_until:
                return False
            for hid, c in self.cursors.items():
                if (hid in live and c.get("pose") == "two" and not c.get("pinch") and self.grip is None
                        and self.zoom is None and now - (c.get("pose_since") or now) >= MENU_HOLD):
                    r = MENU_RADIUS * s
                    x = max(r + 30 * s, min(v.width() - r - 90 * s, c["x"]))
                    y = max(r + 30 * s, min(v.height() - r - 30 * s, c["y"]))
                    self.menu = {"x": x, "y": y, "items": self._menu_items(c["x"], c["y"]), "hot": None,
                                 "hand": hid, "opened": now, "touched": now, "r": r, "hot_since": now}
                    c["pose_since"] = now + 3600                # once per hold
                    c["menu_pinch"] = bool(c.get("pinch"))
                    break
            return self.menu is not None
        m = self.menu
        if v.hover is not None or v.hover_country is not None:
            v.hover, v.hover_country = None, None          # no card under the menu
        c = self.cursors.get(m["hand"])
        if c is None or m["hand"] not in live:
            hands = [(hid, cc) for hid, cc in self.cursors.items() if hid in live]
            if not hands:
                if now - m["touched"] > MENU_IDLE:
                    self.menu = None
                return True
            m["hand"], c = hands[0]
        dx, dy = c["x"] - m["x"], c["y"] - m["y"]
        dist = math.hypot(dx, dy)
        if dist > m["r"] * MENU_AWAY or now - m["touched"] > MENU_IDLE:
            self.menu = None
            self.quiet_until = now + MENU_QUIET
            return True
        n = len(m["items"])
        hot = None
        if dist > m["r"] * 0.42 and n:
            ang = (math.degrees(math.atan2(dy, dx)) + 90.0) % 360.0       # 0 at the top, clockwise
            hot = int(round(ang / (360.0 / n))) % n
        if hot != m["hot"]:
            m["hot"], m["touched"], m["hot_since"] = hot, now, now
        closed = c.get("pose") == "fist" or (c.get("pinch") and not c.get("menu_pinch"))
        if c.get("pinch"):
            c["menu_pinch"] = True
            c["start"] = None                                # the menu's pinch: no tap, no pin later
        else:
            c["menu_pinch"] = False
        if hot is not None and (closed or now - m["hot_since"] >= MENU_DWELL):
            self._choose(now)
        elif hot is None and c.get("pose") == "fist":
            self.menu = None                                 # a fist in the middle: nothing, closed
            self.quiet_until = now + MENU_QUIET
        return True

    def _hold(self, x: float, y: float) -> None:
        """Move the view so the grabbed spot of the planet is under (x, y). On
        the globe a pan is a rotation, so two steps get it exact; a spot that
        has turned round the limb is let go of and the one now under the hand
        taken instead (the hand keeps turning the globe as far as it likes)."""
        v, g = self.view, self.grip
        lx, ly = g["last"]
        g["last"] = (x, y)
        if g["geo"] is None:
            v._pan_pixels(x - lx, y - ly)
            g["geo"] = v._screen_to_geo(x, y)
        else:
            for _ in range(2):
                q = v._projector()(*g["geo"])
                if q is None:
                    break
                v._pan_pixels(x - q[0], y - q[1])
            q = v._projector()(*g["geo"])
            if q is None or math.hypot(q[0] - x, q[1] - y) > 3.0:
                g["geo"] = v._screen_to_geo(x, y)      # past the limb or a pole: take the spot under the hand
        v._changed()

    def _released(self, hid, c, now: float, diag: float) -> None:
        v = self.view
        held = now - (c["pinch_since"] or now)
        tapped = (c["start"] is not None and not c["pinned"] and not c["ghost"]
                  and held <= TAP_TIME and c["moved"] <= STILL)
        if self.grip is not None and self.grip["hand"] == hid and c["moved"] > STILL:
            self.grip = None
            # the fling leaves with the hand's filtered speed - a hand that had
            # stopped before opening its fingers puts the planet down instead
            vx = float(c["fv"][0]) * v.width() / REACH
            vy = float(c["fv"][1]) * v.height() / REACH
            sp = math.hypot(vx, vy)
            if sp > FLING_MIN * math.hypot(v.width(), v.height()):
                if sp > THROW_CAP:
                    vx, vy = vx * THROW_CAP / sp, vy * THROW_CAP / sp
                v.vel = (vx, vy)
                v._kick()
        else:
            if self.grip is not None and self.grip["hand"] == hid:
                self.grip = None
            if tapped:
                v._click(QPointF(c["x"], c["y"]))
        c["start"] = None

    def _pin(self, x: float, y: float) -> None:
        geo = self.view._screen_to_geo(x, y)
        if geo is None:
            return
        if len(self.pins) >= 2:
            self.pins.clear()
        key = f"hands:pin:{len(self.pins)}:{geo[0]:.3f},{geo[1]:.3f}"
        self.pins.append({"lat": geo[0], "lon": geo[1], "name": "", "key": key})
        name_of = getattr(self.view.provider, "place_name", None)
        if callable(name_of):
            self.view._submit(key, name_of, geo[0], geo[1], self.view.span)
        self.view.update()

    def route(self) -> Optional[dict]:
        if len(self.pins) < 2:
            return None
        a, b = self.pins[0], self.pins[1]
        fn = getattr(self.view.provider, "route_facts", None)
        km = _gc_km((a["lat"], a["lon"]), (b["lat"], b["lon"]))
        return fn(km) if callable(fn) else {"km": km, "distance": f"{km:,.0f} km", "flight": ""}

    def situation(self) -> str:
        if not self.on:
            return ""
        parts = ["moved by the user's hands"]
        names = [p["name"] or f"{p['lat']:.2f}, {p['lon']:.2f}" for p in self.pins]
        if len(names) == 1:
            parts.append(f"pin at {names[0]}")
        elif len(names) == 2:
            r = self.route() or {}
            parts.append(f"route pinned from {names[0]} to {names[1]}: {r.get('distance', '')}"
                         + (f", direct flight about {r['flight']}" if r.get("flight") else ""))
        return "; ".join(parts)

    # ── drawing (inside the view's paint) ───────────────────────────────────

    def paint(self, p: QPainter, acc: tuple, s: float) -> None:
        if not self.on:
            return
        now = time.monotonic()
        self._paint_pins(p, acc, s)
        for hid, c in self.cursors.items():
            if now - c["seen"] > SEEN_FOR:
                continue
            fade = max(0.0, 1.0 - (now - c["seen"]) / SEEN_FOR)
            x, y = c["x"], c["y"]
            r = 15 * s
            pinch = c.get("pinch") and not c.get("ghost")
            p.setPen(QPen(QColor(*acc, int(230 * fade)), 2.2))
            p.setBrush(QColor(*acc, int(120 * fade)) if pinch else Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(x, y), r * (0.7 if pinch else 1.0), r * (0.7 if pinch else 1.0))
            p.drawEllipse(QPointF(x, y), 2.2, 2.2)
            if (c.get("pose") == "fist" and not c.get("pinch") and not self.menu and c.get("pose_since")
                    and now >= self.quiet_until and (self.pins or self.view.sel_country or self.view.sat_sel)):
                k = min(1.0, (now - c["pose_since"]) / FIST_HOLD)
                if 0.2 < k < 1.0:
                    p.setPen(QPen(QColor("#ff9b8a"), 2.6))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawArc(QRectF(x - r * 1.6, y - r * 1.6, r * 3.2, r * 3.2), 90 * 16, int(-360 * 16 * k))
            if (pinch and c.get("start") is not None and not c.get("pinned") and self.grip is None
                    and self.zoom is None and c.get("pinch_since")):
                k = min(1.0, (now - c["pinch_since"]) / HOLD_PIN)
                if k > 0.25:                       # a hold in progress: the pin is coming
                    p.setPen(QPen(QColor("#eaf6ff"), 2.6))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawArc(QRectF(x - r * 1.6, y - r * 1.6, r * 3.2, r * 3.2), 90 * 16, int(-360 * 16 * k))
        if self.zoom is not None:
            pts = [c for c in self.cursors.values() if c.get("pinch")][:2]
            if len(pts) == 2:
                p.setPen(QPen(QColor(*acc, 150), 1.4, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(pts[0]["x"], pts[0]["y"]), QPointF(pts[1]["x"], pts[1]["y"]))
        self._paint_menu(p, acc, s, now)
        self._paint_preview(p, acc, s)
        if now - self.started < HINT_SECONDS:
            self._paint_hint(p, acc, s, 1.0 - max(0.0, now - self.started - HINT_SECONDS + 1.5) / 1.5)

    def _paint_pins(self, p: QPainter, acc: tuple, s: float) -> None:
        if not self.pins:
            return
        v = self.view
        proj = v._projector()
        if len(self.pins) == 2:
            a, b = self.pins
            if self._path_for != (a["key"], b["key"]):
                self._path = pv.great_circle((a["lat"], a["lon"]), (b["lat"], b["lon"]), 160)
                self._path_for = (a["key"], b["key"])
            path = self._path
            run: list = []
            p.setPen(QPen(QColor("#ffd36b"), 2.4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            last = None
            for la, lo in path:
                q = proj(la, lo)
                if q is None or (last is not None and abs(q[0] - last[0]) > v.width() * 0.5):
                    if len(run) > 1:
                        p.drawPolyline(run)
                    run = [] if q is None else [QPointF(*q)]
                else:
                    run.append(QPointF(*q))
                last = q
            if len(run) > 1:
                p.drawPolyline(run)
            mid = proj(*path[len(path) // 2])
            r = self.route()
            if mid and r:
                font = QFont(QGuiApplication.font())
                font.setPixelSize(int(17 * s))
                font.setBold(True)
                p.setFont(font)
                text = r["distance"] + (f"   ✈ {r['flight']}" if r.get("flight") else "")
                fm = QFontMetricsF(font)
                box = QRectF(mid[0] - fm.horizontalAdvance(text) / 2 - 10, mid[1] - 34 * s,
                             fm.horizontalAdvance(text) + 20, 26 * s)
                p.setPen(QPen(QColor("#ffd36b"), 1.2))
                p.setBrush(QColor(3, 8, 14, 225))
                p.drawRoundedRect(box, 6, 6)
                p.setPen(QColor("#fff3d1"))
                p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        small = QFont(QGuiApplication.font())
        small.setPixelSize(int(14 * s))
        p.setFont(small)
        for pin in self.pins:
            q = proj(pin["lat"], pin["lon"])
            if q is None:
                continue
            x, y = q
            p.setPen(QPen(QColor("#03070b"), 1.5))
            p.setBrush(QColor("#ffd36b"))
            p.drawEllipse(QPointF(x, y - 14 * s), 7 * s, 7 * s)
            p.drawLine(QPointF(x, y - 7 * s), QPointF(x, y))
            if pin["name"]:
                p.setPen(QColor("#fff3d1"))
                p.drawText(QPointF(x + 10 * s, y - 12 * s), pin["name"][:28])

    def _paint_menu(self, p: QPainter, acc: tuple, s: float, now: float) -> None:
        m = self.menu
        if not m:
            return
        grow = min(1.0, (now - m["opened"]) / 0.15)
        r = m["r"] * (0.6 + 0.4 * grow)
        cx, cy = m["x"], m["y"]
        n = len(m["items"])
        p.setPen(QPen(QColor(*acc, 90), 1.2))
        p.setBrush(QColor(3, 8, 14, int(200 * grow)))
        p.drawEllipse(QPointF(cx, cy), r + 34 * s, r + 34 * s)
        font = QFont(QGuiApplication.font())
        font.setPixelSize(int(19 * s))
        for i, item in enumerate(m["items"]):
            ang = math.radians(i * 360.0 / max(1, n) - 90.0)
            x, y = cx + math.cos(ang) * r, cy + math.sin(ang) * r
            hot = i == m["hot"]
            rr = (27 if hot else 21) * s
            p.setPen(QPen(QColor(*acc, 230 if hot else 140), 2.0 if hot else 1.2))
            p.setBrush(QColor(*acc, 170) if item["on"] else QColor(8, 20, 30, 235))
            p.drawEllipse(QPointF(x, y), rr, rr)
            p.setFont(font)
            p.setPen(QColor("#03070b") if item["on"] else QColor("#eaf6ff"))
            p.drawText(QRectF(x - rr, y - rr, 2 * rr, 2 * rr), Qt.AlignmentFlag.AlignCenter, item["glyph"])
            if hot:
                k = min(1.0, (now - m["hot_since"]) / MENU_DWELL)
                p.setPen(QPen(QColor("#eaf6ff"), 3.0))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawArc(QRectF(x - rr - 6 * s, y - rr - 6 * s, 2 * rr + 12 * s, 2 * rr + 12 * s),
                          90 * 16, int(-360 * 16 * k))
        text = m["items"][m["hot"]]["label"] if m["hot"] is not None else self.view.labels.get("menu", "Menu")
        small = QFont(QGuiApplication.font())
        small.setPixelSize(int(14 * s))
        p.setFont(small)
        p.setPen(QColor("#eaf6ff"))
        p.drawText(QRectF(cx - r * 0.62, cy - r * 0.5, r * 1.24, r), Qt.AlignmentFlag.AlignCenter
                   | Qt.TextFlag.TextWordWrap, text)

    def _paint_preview(self, p: QPainter, acc: tuple, s: float) -> None:
        img = self.preview
        if img is None or not self.show_preview:
            return
        v = self.view
        w = 250 * s
        h = w * img.height() / max(1, img.width())
        box = QRectF(v.width() / 2 - w / 2, v.height() - h - 22 * s, w, h)
        p.drawImage(box, img)
        p.setPen(QPen(QColor(*acc, 180), 1.4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(box, 6, 6)

    def _paint_hint(self, p: QPainter, acc: tuple, s: float, alpha: float) -> None:
        lines = [self.words[k] for k in HINTS]
        font = QFont(QGuiApplication.font())
        font.setPixelSize(int(15 * s))
        fm = QFontMetricsF(font)
        wd = max(fm.horizontalAdvance(t) for t in lines) + 28 * s
        ht = (fm.height() + 6 * s) * len(lines) + 18 * s
        box = QRectF(24 * s, self.view.height() / 2 - ht / 2, wd, ht)
        before = p.opacity()
        p.setOpacity(before * max(0.0, min(1.0, alpha)))
        p.setPen(QPen(QColor(*acc, 140), 1.2))
        p.setBrush(QColor(3, 8, 14, 215))
        p.drawRoundedRect(box, 8, 8)
        p.setFont(font)
        p.setPen(QColor("#dbe9f3"))
        y = box.top() + 9 * s
        for t in lines:
            p.drawText(QRectF(box.left() + 14 * s, y, wd, fm.height() + 6 * s), Qt.AlignmentFlag.AlignVCenter, t)
            y += fm.height() + 6 * s
        p.setOpacity(before)
