"""gui/intro.py — the opening: the HUD powering up, then handing over to itself.

WHAT IT SHOWS, IN ORDER (about 3 s; any click or key skips it)
    0.00  dark; a line of light opens across the middle of the screen
    0.20  the core ignites; three rings draw themselves round it
    0.55  the assistant's name resolves letter by letter, a rule under it
    0.70  the system check: the assistant's subsystems and this machine,
          each line filling and turning ONLINE (no vendor or model names)
    1.95  "ALL SYSTEMS ONLINE"; a shock ring leaves the core
    2.25  the window behind starts its own build (`cue`)
    2.30  everything zooms through and fades, and the HUD is underneath

WHY IT DOES NOT STUTTER
    The old start-up was the full HUD — hundreds of primitives a frame, in
    Python — building itself while the assistant loaded in another thread.
    Python runs one thread at a time, so every frame waited its turn behind
    the loading and the motion hitched.

      * A frame here is a few dozen primitives. The backdrop (vignette and
        grid) is drawn once per size into a pixmap and blitted.
      * The HUD's own timer is held while this covers it (window.py), so
        the expensive scene is not painted underneath for nothing.
      * Motion is measured against the clock, not counted in frames: a late
        frame lands where it should be instead of slowing the whole sequence.
      * While it runs, Python hands the interpreter between threads every
        1 ms instead of 5 ms, so the painting thread waits less for its turn.

    Colours come from the C palette and fonts from gui.style, as everywhere.
"""
from __future__ import annotations

import math
import os
import platform
import sys
import time

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QLinearGradient, QPainter, QPen, QPixmap,
    QRadialGradient,
)
from PyQt6.QtWidgets import QWidget

from gui.style import px_font, with_alpha
from gui.theme import C, qcol

# The timeline below is written in "animation seconds"; SPEED plays it faster
# without retuning every beat. 1.25 makes the whole opening 2.4 s on the clock.
SPEED = 1.25
DURATION = 3.0          # to the end of the fade
CUE_AT = 2.25           # the fade into the HUD is about to begin
_FADE_FROM = 2.30
VOICE_AT = 0.30         # the greeting starts as the core ignites, and ends
                        # about when "ALL SYSTEMS ONLINE" comes up
_VOICE_LATEST = 1.40    # not ready by then: leave it to the conversation


def _ease(x: float) -> float:
    """Smoothstep on 0..1, clamped."""
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def _out(x: float) -> float:
    """Ease-out cubic on 0..1, clamped."""
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


def _span(t: float, start: float, dur: float) -> float:
    return _ease((t - start) / dur) if dur > 0 else float(t >= start)


def system_lines() -> list[tuple[str, str]]:
    """(label, value) for the system check.

    The assistant's own subsystems in its own words — never a vendor or a model
    name: this is the suit powering up, not a settings page. The one concrete
    line is the machine it is running on, read in a millisecond."""
    try:
        import psutil
        ram = psutil.virtual_memory().total / 2 ** 30
        host = (f"{platform.system()} {platform.release()} · "
                f"{os.cpu_count()} THREADS · {ram:.0f} GB").upper()
    except Exception:
        host = f"{platform.system()} {platform.release()}".upper()
    return [
        ("POWER CORE", "STABLE"),
        ("HOST", host),
        ("AUDIO ARRAY", "CALIBRATED"),
        ("NEURAL LINK", "SYNCHRONISED"),
        ("VOICE MATRIX", "TUNED"),
        ("PROTOCOLS", "LOADED"),
    ]


class IntroOverlay(QWidget):
    """Covers its parent, plays once, then removes itself.

    `cue` fires when the window behind should begin its own build; `finished`
    when the overlay is gone. Both fire exactly once, also when skipped."""

    cue = pyqtSignal()
    finished = pyqtSignal()

    def __init__(self, parent: QWidget, title: str = "J.A.R.V.I.S",
                 subtitle: str = "") -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        # Opaque from first frame to last: Qt then never paints the window
        # underneath, which was most of a frame's cost. The fade shows a
        # picture of the HUD taken at the start instead (see start()).
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._title = (title or "J.A.R.V.I.S").upper()
        if not subtitle:
            try:
                from gui.theme import APP_VERSION
                subtitle = str(APP_VERSION)
            except Exception:
                subtitle = ""
        self._subtitle = subtitle.upper()
        self._rows = system_lines()
        self._t0 = None
        self._cued = False
        self._done = False
        self._backdrop: QPixmap | None = None
        self._backdrop_key = None
        self._switch = None
        self._snapshot: QPixmap | None = None
        self.before_snapshot = None      # callable, run just before the photograph
        self.voice_hook = None           # callable -> bool: start the greeting
        self._voiced = False
        # Adaptive quality: frame times of the first moments decide whether the
        # heavier layers are drawn at all (see _judge_speed).
        self._frame_times: list = []
        self._lite = False
        self._tmr = QTimer(self)
        self._tmr.setTimerType(Qt.TimerType.PreciseTimer)
        self._tmr.timeout.connect(self._tick)
        self.setGeometry(parent.rect())

    # ── control ──────────────────────────────────────────────────────────────
    def start(self) -> None:
        self.setGeometry(self.parentWidget().rect())
        if callable(self.before_snapshot):
            # The window puts itself in the state the fade should land on.
            try:
                self.before_snapshot()
            except Exception as e:
                print(f"[HUD] opening: before_snapshot failed ({e})")
        self._take_snapshot()
        self.raise_()
        self.show()
        self.setFocus()
        try:
            self._switch = sys.getswitchinterval()
            sys.setswitchinterval(0.001)
        except Exception:
            self._switch = None
        self._t0 = time.perf_counter()
        self._tmr.start(16)

    def _take_snapshot(self) -> None:
        """A picture of the finished HUD to fade into.

        Taken while the screen is still dark, where the one expensive frame it
        costs cannot be seen. Fading over the LIVE window instead meant
        painting the whole HUD under a translucent cover on every frame of the
        fade: a measured 34-40 ms a frame, a visible stutter at the very end."""
        try:
            self.hide()
            self._snapshot = self.parentWidget().grab()
        except Exception as e:
            print(f"[HUD] opening: no snapshot ({e})")
            self._snapshot = None
        finally:
            self.show()

    def skip(self) -> None:
        if self._t0 is not None and not self._done:
            # Jump to the fade rather than cutting: a skip should still feel
            # like the system handing over, just sooner.
            now = (time.perf_counter() - self._t0) * SPEED
            if now < _FADE_FROM:
                self._t0 -= (_FADE_FROM - now) / SPEED

    def _judge_speed(self) -> None:
        """After a dozen frames: is this machine keeping up?

        The opening is cheap on any recent computer (~4 ms a frame at 1080p),
        but not every computer is recent, and a 4K screen on integrated
        graphics, a busy CPU or a remote desktop can each make it hitch. A
        hitching opening looks broken, so it degrades instead: first the full-
        window backdrop and the tick ring go (most of the per-frame cost), and
        if even that is not enough it fades straight into the HUD."""
        times = self._frame_times
        gaps = sorted(b - a for a, b in zip(times, times[1:]))
        if not gaps:
            return
        median = gaps[len(gaps) // 2]
        if median > 0.045:
            print(f"[HUD] opening: {median * 1000:.0f} ms a frame - skipping to the HUD")
            self.skip()
        elif median > 0.026:
            print(f"[HUD] opening: {median * 1000:.0f} ms a frame - lighter drawing")
            self._lite = True

    def _tick(self) -> None:
        now = time.perf_counter()
        t = (now - self._t0) * SPEED
        if len(self._frame_times) < 13:
            self._frame_times.append(now)
            if len(self._frame_times) == 13:
                self._judge_speed()
        if not self._voiced and VOICE_AT <= t <= _VOICE_LATEST and callable(self.voice_hook):
            # The greeting, if this start came with one (ui.JarvisUI). The hook
            # starts it on its own thread and returns at once.
            try:
                self._voiced = bool(self.voice_hook())
            except Exception as e:
                print(f"[HUD] opening: greeting not played ({e})")
                self._voiced = True
        if not self._cued and t >= CUE_AT:
            self._cued = True
            self.cue.emit()
        if t >= DURATION:
            self._finish()
            return
        self.update()

    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        self._tmr.stop()
        if not self._cued:
            self._cued = True
            self.cue.emit()
        if self._switch is not None:
            # Give the HUD's own build, which starts at the cue, the same
            # advantage for a few seconds more before going back to normal.
            prev = self._switch
            QTimer.singleShot(3500, lambda: sys.setswitchinterval(prev))
        self.hide()
        self.finished.emit()
        self.deleteLater()

    def mousePressEvent(self, e) -> None:
        self.skip()

    def keyPressEvent(self, e) -> None:
        # Modifier keys never skip: bringing the window to the front sends a
        # bare Alt (window._force_foreground), and it was skipping the opening
        # — and the greeting with it — every time.
        if e.key() in (Qt.Key.Key_Alt, Qt.Key.Key_Control, Qt.Key.Key_Shift,
                       Qt.Key.Key_Meta, Qt.Key.Key_AltGr):
            return
        self.skip()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._backdrop = None
        if self._snapshot is not None and self._snapshot.size() != self.size():
            self._snapshot = None           # wrong size now: the fade shows the
                                            # intro's own backdrop going dark

    # ── painting ─────────────────────────────────────────────────────────────
    def _get_backdrop(self, W: int, H: int) -> QPixmap:
        dpr = self.devicePixelRatioF()
        key = (W, H, dpr, C.BG, C.PRI)
        if self._backdrop is not None and self._backdrop_key == key:
            return self._backdrop
        # At the screen's own pixel density: a logical-size pixmap on a scaled
        # display is resampled on every single frame it is drawn.
        pm = QPixmap(max(1, int(W * dpr)), max(1, int(H * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(qcol(C.BG))
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = W / 2, H / 2
        g = QRadialGradient(cx, cy, max(W, H) * 0.7)
        g.setColorAt(0.0, with_alpha(C.PRI, 0.10))
        g.setColorAt(0.45, with_alpha(C.PRI, 0.03))
        g.setColorAt(1.0, with_alpha(C.BG, 0.0))
        p.fillRect(QRectF(0, 0, W, H), QBrush(g))
        # a fine grid, brightest at the centre
        step = 34
        for x in range(int(cx % step), W, step):
            k = 1 - min(1.0, abs(x - cx) / (W * 0.55))
            p.setPen(QPen(with_alpha(C.PRI, 0.05 * k), 1))
            p.drawLine(x, 0, x, H)
        for y in range(int(cy % step), H, step):
            k = 1 - min(1.0, abs(y - cy) / (H * 0.55))
            p.setPen(QPen(with_alpha(C.PRI, 0.05 * k), 1))
            p.drawLine(0, y, W, y)
        p.end()
        self._backdrop, self._backdrop_key = pm, key
        return pm

    def paintEvent(self, _e) -> None:
        if self._t0 is None:
            # Shown before the first tick: dark, so the HUD never flashes first.
            p = QPainter(self)
            p.fillRect(self.rect(), QColor(0, 0, 0))
            p.end()
            return
        t = (time.perf_counter() - self._t0) * SPEED
        W, H = self.width(), self.height()
        # Layout from the size, top to bottom, so nothing meets anything else
        # at any window size: rings, then the name under them, then the
        # system check under that.
        size = max(26.0, min(64.0, W * 0.042))
        cx, cy = W / 2.0, H * 0.40
        R = min(W, H) * 0.19
        title_y = cy + R * 1.30 + size * 0.6
        pri, white, acc, green = qcol(C.PRI), qcol(C.WHITE), qcol(C.ACC), qcol(C.GREEN)

        fade = 1.0 - _span(t, _FADE_FROM, DURATION - _FADE_FROM)
        zoom = 1.0 + 0.35 * _out((t - _FADE_FROM) / (DURATION - _FADE_FROM)) if t > _FADE_FROM else 1.0

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if fade < 1.0:
            # Underneath: what the window will look like when this is gone.
            if self._snapshot is not None:
                p.drawPixmap(0, 0, self._snapshot)
            else:
                p.fillRect(QRectF(0, 0, W, H), qcol(C.BG))
        p.setOpacity(fade)
        if self._lite:
            p.fillRect(QRectF(0, 0, W, H), qcol(C.BG))
        else:
            p.drawPixmap(0, 0, self._get_backdrop(W, H))
        # the backdrop comes up out of black
        black = 1.0 - _span(t, 0.0, 0.45)
        if black > 0:
            p.fillRect(QRectF(0, 0, W, H), with_alpha(QColor(0, 0, 0), black))

        # ── the line of light across the middle ──────────────────────────────
        a = _span(t, 0.0, 0.45)
        if a > 0 and t < 1.6:
            half = W * 0.42 * _out(t / 0.5)
            k = (1 - _span(t, 0.9, 0.7))
            lg = QLinearGradient(cx - half, 0, cx + half, 0)
            lg.setColorAt(0.0, with_alpha(pri, 0.0))
            lg.setColorAt(0.5, with_alpha(white, 0.9 * k))
            lg.setColorAt(1.0, with_alpha(pri, 0.0))
            p.setPen(QPen(QBrush(lg), 1.6))
            p.drawLine(QPointF(cx - half, cy), QPointF(cx + half, cy))

        p.save()
        p.translate(cx, cy)
        p.scale(zoom, zoom)

        # ── the core ─────────────────────────────────────────────────────────
        ig = _out((t - 0.2) / 0.5)
        if ig > 0:
            pulse = 1.0 + 0.06 * math.sin(t * 7.0)
            cr = R * 0.22 * ig * pulse
            g = QRadialGradient(0, 0, cr * 2.6)
            g.setColorAt(0.0, with_alpha(white, 0.95))
            g.setColorAt(0.18, with_alpha(pri, 0.85))
            g.setColorAt(0.5, with_alpha(pri, 0.18))
            g.setColorAt(1.0, with_alpha(pri, 0.0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(g))
            p.drawEllipse(QPointF(0, 0), cr * 2.6, cr * 2.6)

        # ── three rings drawing themselves ───────────────────────────────────
        p.setBrush(Qt.BrushStyle.NoBrush)
        spin = t * 18.0
        for i, (rad, start, speed, width, alpha) in enumerate((
                (1.00, 0.25, 1.0, 2.0, 0.9),
                (0.82, 0.35, -1.6, 1.2, 0.6),
                (0.64, 0.45, 2.4, 3.0, 0.75))):
            prog = _out((t - start) / 0.65)
            if prog <= 0:
                continue
            pen = QPen(with_alpha(acc if i == 2 else pri, alpha), width)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            p.setPen(pen)
            r = R * rad
            rect = QRectF(-r, -r, 2 * r, 2 * r)
            base = 90 + spin * speed
            if i == 2:
                # broken ring: three arcs
                for j in range(3):
                    p.drawArc(rect, int((base + j * 120) * 16), int(-80 * prog * 16))
            else:
                p.drawArc(rect, int(base * 16), int(-360 * prog * 16))

        # ── tick ring ────────────────────────────────────────────────────────
        tk = 0.0 if self._lite else _span(t, 0.35, 0.8)
        if tk > 0:
            n = int(120 * tk)
            r0, r1 = R * 1.10, R * 1.16
            p.setPen(QPen(with_alpha(pri, 0.45), 1))
            for i in range(n):
                ang = math.radians(i * 3 - spin * 0.4)
                long_ = i % 10 == 0
                ra = r0 - (R * 0.04 if long_ else 0)
                c, s = math.cos(ang), math.sin(ang)
                p.drawLine(QPointF(c * ra, s * ra), QPointF(c * r1, s * r1))

        # ── the shock ring leaving the core ──────────────────────────────────
        if 1.95 <= t <= 2.75:
            k = (t - 1.95) / 0.8
            rr = R * (0.3 + 3.2 * _out(k))
            p.setPen(QPen(with_alpha(white, 0.55 * (1 - k)), 2.5 * (1 - k) + 0.5))
            p.drawEllipse(QPointF(0, 0), rr, rr)
        p.restore()

        # ── the name ─────────────────────────────────────────────────────────
        if t > 0.55:
            p.setFont(px_font(size, QFont.Weight.DemiBold, spacing=size * 0.32))
            fm = p.fontMetrics()
            total = fm.horizontalAdvance(self._title)
            x = cx - total / 2
            for i, ch in enumerate(self._title):
                k = _out((t - 0.55 - i * 0.045) / 0.35)
                w = fm.horizontalAdvance(ch)
                if k > 0:
                    p.setPen(QPen(with_alpha(white, k)))
                    p.drawText(QRectF(x, title_y - size * 0.7 + (1 - k) * 10, w + 4, size * 1.4),
                               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, ch)
                x += w
            rule = _out((t - 0.75) / 0.6)
            if rule > 0:
                half = total * 0.5 * rule
                lg = QLinearGradient(cx - half, 0, cx + half, 0)
                lg.setColorAt(0.0, with_alpha(pri, 0.0))
                lg.setColorAt(0.5, with_alpha(pri, 0.9))
                lg.setColorAt(1.0, with_alpha(pri, 0.0))
                p.setPen(QPen(QBrush(lg), 1.4))
                yy = title_y + size * 0.62
                p.drawLine(QPointF(cx - half, yy), QPointF(cx + half, yy))
            sub = _span(t, 0.95, 0.4)
            if sub > 0:
                online = t >= 1.95
                text = (f"{self._subtitle}   ·   ALL SYSTEMS ONLINE" if online
                        else f"{self._subtitle}   ·   INITIALISING")
                p.setFont(px_font(max(11.0, size * 0.24), QFont.Weight.DemiBold,
                                  spacing=size * 0.08))
                p.setPen(QPen(with_alpha(green if online else pri, 0.9 * sub)))
                p.drawText(QRectF(0, title_y + size * 0.78, W, size * 0.6),
                           Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, text)

        # ── the system check, lower left ─────────────────────────────────────
        if t > 0.7 and self._rows:
            fs = max(10.0, min(13.0, W * 0.0085))
            p.setFont(px_font(fs, QFont.Weight.DemiBold, mono=True))
            fm = p.fontMetrics()
            x0 = max(28.0, W * 0.045)
            line_h = fs * 2.1
            below_title = title_y + size * 1.4 + line_h
            y0 = max(H - 36 - line_h * len(self._rows), below_title)
            bar_w = max(70.0, W * 0.07)
            for i, (label, value) in enumerate(self._rows):
                st = 0.7 + i * 0.16
                k = _span(t, st, 0.25)
                if k <= 0:
                    continue
                fill = _out((t - st) / 0.45)
                y = y0 + i * line_h
                p.setPen(QPen(with_alpha(pri, 0.75 * k)))
                p.drawText(QPointF(x0, y), f"{label:<12}")
                bx = x0 + fm.horizontalAdvance("M" * 13)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(with_alpha(pri, 0.15 * k))
                p.drawRect(QRectF(bx, y - fs * 0.55, bar_w, 3))
                p.setBrush(with_alpha(green if fill >= 1 else pri, 0.9 * k))
                p.drawRect(QRectF(bx, y - fs * 0.55, bar_w * fill, 3))
                p.setPen(QPen(with_alpha(white if fill >= 1 else pri, 0.85 * k)))
                status = "ONLINE" if fill >= 1 else f"{int(fill * 100):3d}%"
                p.drawText(QPointF(bx + bar_w + 12, y), f"{status:<7} {value[:40]}")
        p.end()
