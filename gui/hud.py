"""gui/hud.py — The centre of the HUD: the avatar or the reactor core, the state line and the waveform.

Split out of ui.py in Mark LVII; behaviour unchanged."""
from __future__ import annotations

import json
import math
import os
import platform
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

from PyQt6.QtCore import (
    QEasingCurve, QLineF, QMimeData, QObject, QParallelAnimationGroup, QPointF,
    QPoint, QPropertyAnimation, QRect, QRectF, QSize, QSizeF, Qt, QTimer,
    QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QDragEnterEvent, QDropEvent, QFont,
    QFontDatabase, QKeySequence, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap, QPolygonF, QRadialGradient, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QGraphicsScene, QGraphicsView, QGraphicsPixmapItem,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget, QProgressBar, QSlider,
)

from gui.theme import (
    C,
    HoloAvatar,
    qcol,
)

from gui.style import blend, chamfer, px_font, with_alpha

try:
    from gui.reactor import Reactor3D
except Exception:      # pragma: no cover — the classic core still draws
    Reactor3D = None


class HudCanvas(QWidget):
    def __init__(self, face_path: str, assistant_name: str = "J.A.R.V.I.S", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.muted    = False
        self.speaking = False
        self.state    = "INITIALISING"
        self._assistant_name = assistant_name

        # The holographic head that fills the HUD. If it could not be imported
        # we fall back to the old glowing core so the panel is never empty.
        self._avatar = None
        if HoloAvatar is not None:
            try:
                self._avatar = HoloAvatar()
            except Exception:
                self._avatar = None

        # The reactor core in three dimensions (gui/reactor.py). The flat ring
        # stack it replaced is kept whole as _paint_core_classic, and it is
        # what draws if the model could not be built.
        self._reactor = None
        if Reactor3D is not None:
            try:
                self._reactor = Reactor3D()
            except Exception:
                self._reactor = None

        # Which centrepiece to draw. Read once here and changed live by the
        # settings toggle; the avatar object is kept either way so switching
        # back is instant and costs no reload.
        try:
            from memory.config_manager import get_hud_style
            self.hud_style = get_hud_style()
        except Exception:
            self.hud_style = "face"
        self._core_phase = 0.0

        self._tick       = 0
        self._scale      = 1.0
        self._tgt_scale  = 1.0
        self._halo       = 55.0
        self._tgt_halo   = 55.0
        self._last_t     = time.time()
        self._step_t     = time.time()
        self._blink      = True
        self._blink_tick = 0

        # Rescaled-face cache: the smooth rescale is expensive, so we keep the
        # last result and only rebuild it when the (quantised) size changes.

        # Static grid-dot layer, pre-rendered once per size/theme into a pixmap
        # so paintEvent blits it in one call instead of thousands of drawPoint()s.
        self._grid_cache: QPixmap | None = None
        self._grid_key = None
        # Repaint throttle counter (idle frames drop to ~20 Hz — see _step()).
        self._paint_tick = 0

        # Live audio reactivity: _live_amp is written from the audio threads
        # (0.0–1.0), _amp_disp is the smoothed value the paint code reads.
        self._live_amp  = 0.0
        self._amp_disp  = 0.0
        # (frames, start_time, hop) posted by the playback thread — see
        # push_visemes(). None means "no schedule; use the plain level".
        self._visemes = None
        self._vis_i = None        # first schedule frame not yet handed to the mouth
        self._base_scale = 1.0    # slow "breathing" target; amp is added per-frame
        self._base_halo  = 55.0

        # Mark 58 HUD: motion phase (scaled by the user's motion choice), the
        # start-up build, the lines to the panels and the cursor's pull.
        self._mphase = 0.0
        self._boot_t0 = None
        self._lines: list = []
        self._par = (0.0, 0.0)
        rnd = random.Random(5858)
        self._motes = [(rnd.random(), rnd.random(), rnd.uniform(0.4, 1.4),
                        rnd.uniform(0.7, 1.9), rnd.uniform(0, 6.3), rnd.random() < 0.3)
                       for _ in range(42)]

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(16)

    def glance(self, dx: float, dy: float, hold: float = 1.1) -> None:
        """Ask the avatar to look somewhere for a moment (see HoloAvatar.glance)."""
        try:
            if self._avatar is not None:
                self._avatar.glance(dx, dy, hold)
        except Exception:
            pass

    def push_visemes(self, frames, hop: float, at: float) -> None:
        """Thread-safe: hand over a schedule of (level, openness, width) frames.

        The playback thread writes up to 200 ms of audio in one go, so a single
        averaged level would only move the mouth five times a second — enough to
        flap, nowhere near enough to articulate. It instead posts the whole
        slice's worth of 20 ms frames here and `_step()` plays them out against
        the wall clock, in step with the audio going to the speakers.

        `at` is the wall-clock time this batch will *begin to sound*, which the
        caller tracks as a playback cursor. It is not the time of the call, and
        the difference is the whole point: `stream.write` returns once the buffer
        accepts the samples, so consecutive batches are handed over far faster
        than they play. Anchoring each one to "now" made every batch start while
        its predecessor was still sounding, so each schedule replaced the last
        after a couple of frames and the mouth only ever played the opening
        instant of every 200 ms — the reason it did not match the words.

        Successive batches are therefore *appended* into one continuous
        timeline, not swapped in. A paragraph is one schedule; the mouth stops
        falling into a gap at every chunk boundary and having to climb back out.
        """
        try:
            if not frames:
                return
            hop = max(1e-3, float(hop))
            at = float(at)
            new = list(frames)
            cur = self._visemes
            if cur is not None:
                old, t0, ohop = cur
                if abs(ohop - hop) < 1e-6:
                    # Where in the existing timeline does this batch land?
                    i = int(round((at - t0) / hop))
                    if 0 <= i <= len(old) + 1:
                        # Continues (or slightly overlaps) what is already
                        # queued: extend rather than restart. Drop whatever has
                        # already been played so the list cannot grow without
                        # bound over a long reply.
                        merged = old[:i] + new
                        played = int((time.time() - t0) / hop) - 2
                        if played > 60:
                            merged = merged[played:]
                            t0 += played * hop
                            if self._vis_i is not None:
                                self._vis_i = max(0, self._vis_i - played)
                        self._visemes = (merged, t0, hop)
                        return
            self._visemes = (new, at, hop)
            self._vis_i = None
        except Exception:
            pass

    def set_audio_level(self, level: float) -> None:
        """Thread-safe entry point for the audio threads. Stores the louder of
        the incoming level and the current value so brief gaps between chunks
        don't make the waveform stutter; _step() decays it back down."""
        try:
            lv = float(level)
        except (TypeError, ValueError):
            return
        if lv < 0.0:
            lv = 0.0
        elif lv > 1.0:
            lv = 1.0
        if lv > self._live_amp:
            self._live_amp = lv

    def _make_grid(self, W: int, H: int) -> QPixmap:
        """Pre-render the static backdrop — atmosphere, workshop floor, bokeh
        and the screen frame — once per size and theme, so paintEvent blits one
        pixmap instead of redrawing a scene every frame.

        The floor is a perspective grid receding to a horizon with two rows of
        lights along it, after the workshop the HUD is modelled on; the bokeh
        are out-of-focus lights, mostly cool with a few warm. The layout is
        seeded, so it is the same scene on every launch."""
        W, H = max(1, W), max(1, H)
        pm = QPixmap(W, H)
        pm.fill(qcol(C.BG))
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx = W / 2

        g = QRadialGradient(cx, H * 0.42, max(W, H) * 0.62)
        g.setColorAt(0.0, blend(C.PRI, 0.10))
        g.setColorAt(0.55, blend(C.PRI, 0.035))
        g.setColorAt(1.0, qcol(C.BG))
        p.fillRect(QRectF(0, 0, W, H), QBrush(g))

        hy, vy = H * 0.70, H * 0.52
        p.save()
        p.setClipRect(QRectF(0, hy, W, H - hy))
        for i in range(-14, 15):
            x = cx + i * W * 0.11
            p.setPen(QPen(blend(C.PRI, 0.10 * (1 - abs(i) / 16)), 1))
            p.drawLine(QLineF(cx + (x - cx) * 0.05, vy, x, H))
        y, step = hy, 5.0
        while y < H:
            k = (y - hy) / max(1.0, H - hy)
            p.setPen(QPen(blend(C.PRI, 0.03 + 0.09 * k), 1))
            p.drawLine(QLineF(0, y, W, y))
            y += step
            step *= 1.28
        p.setPen(Qt.PenStyle.NoPen)
        for side in (-1, 1):
            for j in range(3, 12):
                t = j / 11
                x = cx + side * (W * 0.16 + t * t * W * 0.26)
                yy = hy + (H - hy) * t * t
                rr = (0.6 + 1.4 * t) * 3
                gg = QRadialGradient(x, yy, rr)
                gg.setColorAt(0.0, with_alpha(C.WHITE, 0.30 * (0.3 + t)))
                gg.setColorAt(0.35, with_alpha(C.PRI, 0.12 * (0.3 + t)))
                gg.setColorAt(1.0, with_alpha(C.PRI, 0.0))
                p.setBrush(QBrush(gg))
                p.drawEllipse(QPointF(x, yy), rr, rr)
        p.restore()

        lg = QLinearGradient(0, 0, W, 0)
        lg.setColorAt(0.0, with_alpha(C.PRI, 0.0))
        lg.setColorAt(0.5, with_alpha(C.PRI, 0.35))
        lg.setColorAt(1.0, with_alpha(C.PRI, 0.0))
        p.setPen(QPen(QBrush(lg), 1))
        p.drawLine(QLineF(0, hy, W, hy))

        rnd = random.Random(57)
        p.setPen(Qt.PenStyle.NoPen)
        for _ in range(int(W * H / 9000)):
            x, yy = rnd.uniform(0, W), rnd.uniform(0, H * 0.72)
            rr = rnd.uniform(0.8, 3.2) * 3
            warm = rnd.random() < 0.12
            c = C.ACC2 if warm else (C.WHITE if rnd.random() < 0.3 else C.PRI)
            a = rnd.uniform(0.08, 0.40)
            gg = QRadialGradient(x, yy, rr)
            gg.setColorAt(0.0, with_alpha(c, a))
            gg.setColorAt(1.0, with_alpha(c, 0.0))
            p.setBrush(QBrush(gg))
            p.drawEllipse(QPointF(x, yy), rr, rr)

        # the screen's edge: a thin chamfered frame with lit corners
        m, cut, arm = 6, 16, 46
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(blend(C.PRI, 0.20), 1))
        p.drawPolygon(chamfer(QRectF(m, m, W - 2 * m, H - 2 * m), cut, ("tl", "tr", "br", "bl")))
        p.setPen(QPen(blend(C.PRI, 0.9), 1.8))
        for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
            x0 = m if sx > 0 else W - m
            y0 = m if sy > 0 else H - m
            p.drawLine(QLineF(x0, y0 + sy * (cut + arm), x0, y0 + sy * cut))
            p.drawLine(QLineF(x0, y0 + sy * cut, x0 + sx * cut, y0))
        p.setFont(px_font(10, QFont.Weight.DemiBold, spacing=2.2))
        p.setPen(QPen(blend(C.PRI, 0.55)))
        p.drawText(QRectF(m + 24, m + 8, 260, 16), Qt.AlignmentFlag.AlignVCenter, "VOICE INTERFACE")
        p.end()
        return pm

    def _step(self):
        self._tick += 1
        now = time.time()

        # ── Live audio reactivity ────────────────────────────────────────────
        # A viseme schedule, if one is playing, gives both the level and the
        # mouth shape for this exact instant; otherwise fall back to the peak
        # level the audio threads pushed in.
        v_open = v_wide = v_level = None
        v_seq = None
        sched = self._visemes
        if sched is not None:
            frames, t0, hop = sched
            i = int((now - t0) / hop)
            if 0 <= i < len(frames):
                # Hand over *every* frame since the last tick, not just the one
                # under the cursor. This timer runs at 60 Hz but the paint is
                # throttled and the machine may be busy, so a tick can span two
                # or three 20 ms frames — and a consonant closure is only two
                # frames long. Sampling one and discarding the rest is how the
                # closures between words went missing.
                j = self._vis_i if self._vis_i is not None else i
                v_seq = frames[max(0, j):i + 1]
                self._vis_i = max(j, i + 1)
                v_level, v_open, v_wide = frames[i]
                if v_seq:
                    peak = max(f[0] for f in v_seq)
                    if peak > self._live_amp:
                        self._live_amp = peak
            elif i >= len(frames):
                self._visemes = None        # schedule spent
                self._vis_i = None

        # Audio threads push peaks into _live_amp; decay it toward silence so
        # gaps between chunks fade out instead of freezing, then smooth it.
        self._live_amp *= 0.86
        self._amp_disp += (self._live_amp - self._amp_disp) * 0.45
        amp = self._amp_disp

        # The avatar animates off the very same smoothed level the waveform
        # uses — one audio source, so the mouth can never drift out of sync.
        dt = now - self._step_t
        self._step_t = now
        # Integrated, not derived from absolute time: multiplying wall-clock by
        # a rate that changes with state jumps the rings the instant JARVIS
        # starts talking. Same lesson the head's sway taught.
        self._core_phase += min(0.10, max(0.0, dt))
        try:
            from gui.look import LOOK
            self._mphase += min(0.10, max(0.0, dt)) * LOOK.motion
            if LOOK.get("parallax", True) and LOOK.motion > 0 and self.isVisible():
                from PyQt6.QtGui import QCursor
                win = self.window()
                c = win.mapFromGlobal(QCursor.pos())
                tx = max(-1.0, min(1.0, (c.x() - win.width() / 2) / max(1, win.width() / 2)))
                ty = max(-1.0, min(1.0, (c.y() - win.height() / 2) / max(1, win.height() / 2)))
            else:
                tx = ty = 0.0
            px, py = self._par
            self._par = (px + (tx - px) * 0.06, py + (ty - py) * 0.06)
        except Exception:
            pass

        if self._avatar is not None and self.hud_style == "face":
            self._avatar.step(dt, amp, speaking=self.speaking,
                              muted=self.muted, state=self.state,
                              v_open=v_open, v_wide=v_wide or 0.0,
                              v_level=v_level, v_seq=v_seq,
                              v_hop=(sched[2] if sched is not None else 0.02))
        else:
            if self._reactor is not None:
                self._reactor.step(dt, self.speaking,
                                   self.state in ("THINKING", "PROCESSING"), self.muted)
            # Fallback core: slow "breathing" base target, lifted by the level.
            if now - self._last_t > (0.12 if self.speaking else 0.5):
                if self.speaking:
                    self._base_scale = 1.03
                    self._base_halo  = 122.0
                elif self.muted:
                    self._base_scale = random.uniform(0.998, 1.002)
                    self._base_halo  = random.uniform(15, 28)
                else:
                    self._base_scale = random.uniform(1.001, 1.008)
                    self._base_halo  = random.uniform(48, 68)
                self._last_t = now

            if self.muted:
                self._tgt_scale, self._tgt_halo = self._base_scale, self._base_halo
            elif self.speaking:
                self._tgt_scale = self._base_scale + amp * 0.13
                self._tgt_halo  = self._base_halo  + amp * 95.0
            else:
                self._tgt_scale = self._base_scale + amp * 0.06
                self._tgt_halo  = self._base_halo  + amp * 75.0

            sp = 0.38 if self.speaking else (0.30 if amp > 0.02 else 0.15)
            self._scale += (self._tgt_scale - self._scale) * sp
            self._halo  += (self._tgt_halo  - self._halo)  * sp

        self._blink_tick += 1
        if self._blink_tick >= 38:
            self._blink = not self._blink
            self._blink_tick = 0
            _blinked = True
        else:
            _blinked = False

        # Repaint throttling — advancing the animation state above is cheap at
        # 60 Hz, but the paint is heavy. Active (speaking, audio, thinking) runs
        # at ~30 Hz, which is the frame rate animation has used for talking
        # characters forever and is indistinguishable here; idle drops to ~20 Hz
        # so a sleeping HUD stops pinning a CPU core. The visuals stay smooth
        # either way because the animation state keeps stepping at 60 Hz.
        self._paint_tick = (self._paint_tick + 1) % 6
        active = (self.speaking or amp > 0.02
                  or self.state in ("THINKING", "PROCESSING"))
        if _blinked or (self._paint_tick % 2 == 0 if active
                        else self._paint_tick % 3 == 0):
            # Nothing is on screen when the window is hidden or minimised, so
            # rendering the avatar into it is pure waste — and this app is meant
            # to sit running all day. The animation state above keeps stepping,
            # so it picks up mid-motion instead of snapping when you come back.
            if self._on_screen():
                self.update()

    def _on_screen(self) -> bool:
        """True only when this canvas can actually be seen by the user."""
        try:
            if not self.isVisible():
                return False
            win = self.window()
            return not (win.isMinimized() or win.isHidden())
        except Exception:
            return True      # never let a visibility check stop the HUD drawing

    # ── reactor core ─────────────────────────────────────────────────────────
    # The centrepiece for anyone who did not want a face looking back at them.
    # Built from the same budget as the head — software QPainter, no GPU — and
    # from the same principle: everything on it means something. The rings turn
    # at a rate the state sets, the spectrum ring is the real audio level, and
    # the core brightens with the voice. Nothing here is decoration that moves
    # for its own sake, which is what made the old glowing orb feel dead.

    def _core_colours(self):
        if self.muted:
            return qcol(C.MUTED_C), qcol(C.MUTED_C)
        if self.speaking:
            return qcol(C.PRI), qcol(C.ACC)
        if self.state in ("THINKING", "PROCESSING"):
            return qcol(C.PRI), qcol(C.ACC2)
        if self.state == "LISTENING":
            return qcol(C.PRI), qcol(C.GREEN)
        return qcol(C.PRI), qcol(C.PRI_DIM)

    def _paint_core(self, p: QPainter, cx: float, cy: float, r: float,
                    W: float = 0.0, H: float = 0.0):
        """Draw the reactor core centred on (cx, cy) with core radius r.

        The 3-D model when it was built, the flat ring stack otherwise. Going
        back to the flat one for good is this method calling
        _paint_core_classic, which is kept whole for exactly that."""
        if self._reactor is None:
            return self._paint_core_classic(p, cx, cy, r, W, H)
        main, acc = self._core_colours()
        amp = self._amp_disp
        live = (self.speaking or amp > 0.04) and not self.muted
        self._reactor.paint(p, cx, cy, r, main, acc, qcol(C.WHITE), amp, live,
                            self._assistant_name or "")

    def set_readouts(self, cpu: float, mem: float, net: str) -> None:
        """The computer's state for the arcs around the core (the window's
        metrics timer, every 2 s)."""
        self._ro = (max(0.0, min(100.0, float(cpu))), max(0.0, min(100.0, float(mem))), str(net or ""))

    # ── Mark 58 HUD helpers ──────────────────────────────────────────────────
    def set_connectors(self, lines) -> None:
        """The lines from the panels to the core, in this widget's space, for
        the pulses that run along them."""
        self._lines = [list(l) for l in (lines or [])]

    def start_boot(self) -> None:
        from gui.look import LOOK
        if LOOK.get("boot", True) and LOOK.motion > 0:
            self._boot_t0 = time.time()

    def _boot(self, start: float, dur: float = 0.38) -> float:
        """How far one layer of the core has built itself, 0..1, smoothed."""
        if self._boot_t0 is None:
            return 1.0
        b = (time.time() - self._boot_t0) / 2.4
        if b >= 1.6:
            self._boot_t0 = None
            return 1.0
        x = max(0.0, min(1.0, (b - start) / dur))
        return x * x * (3 - 2 * x)

    def _band(self):
        W, H = self.width(), self.height()
        cx, cy = W / 2, H / 2
        fw = min(W, H)
        sy = min(cy + fw * 0.38, H - 74.0)
        band_t = 30.0
        band_h = max(60.0, sy - 14.0 - band_t)
        return W, H, cx, sy, band_t, band_h

    def core_geometry(self):
        """(cx, cy, r) of the centrepiece, in this widget's space."""
        W, H, cx, sy, band_t, band_h = self._band()
        from gui.look import LOOK
        if self.hud_style == "deck":
            k = 2.3 if LOOK.hud else 2.12
            r = min(W * (0.33 if LOOK.hud else 0.36), band_h / k)
            return cx, band_t + band_h / 2.0, r
        fw = min(W, H)
        if self.hud_style == "face" and self._avatar is not None:
            r_head = min(fw * 0.355, band_h / (self._avatar.SPAN + 0.08))
            head_cy = band_t + (band_h - self._avatar.SPAN * r_head) / 2.0 + r_head
            return cx, head_cy, r_head * 1.05
        if self._reactor is not None:
            ext = Reactor3D.TOP + Reactor3D.BOTTOM
            r = min(W * 0.25, band_h / ext)
            core_cy = band_t + (band_h - ext * r) / 2.0 + Reactor3D.TOP * r
            # the rings reach about 1.2 core radii out to the sides
            return cx, core_cy, r * 1.2
        r = min(W * 0.46, band_h / 2.0)
        return cx, band_t + band_h / 2.0, r

    def _paint_deck(self, p: QPainter, cx0: float, cy0: float, r: float) -> None:
        """Mark 58's centrepiece, from the outside in: a bearing scale, a
        segmented ring with a running highlight, the computer's own CPU and
        memory as arcs (the assistant's name and the release written in the
        gaps), a radar sweep, two tilted orbits, three broken rings, a
        hexagon, 60 voice bars on the real audio level and a breathing core.

        At start-up it builds itself from the core outward; with parallax on,
        the inner layers lean toward the cursor more than the outer ones,
        which reads as depth. All flat 2-D, a few hundred primitives."""
        from gui.look import LOOK
        t = self._mphase * 2.08
        amp = self._amp_disp
        gk = LOOK.glow
        if self.muted:
            main = acc = qcol(C.MUTED_C)
        elif self.speaking:
            main, acc = qcol(C.PRI), qcol(C.AMB)
        elif self.state in ("THINKING", "PROCESSING"):
            main, acc = qcol(C.PRI), qcol(C.ACC2)
        elif self.state == "LISTENING":
            main, acc = qcol(C.PRI), qcol(C.GREEN)
        else:
            main, acc = qcol(C.PRI), qcol(C.PRI)
        cpu, mem, net = getattr(self, "_ro", (0.0, 0.0, ""))
        par_x, par_y = self._par

        def at(depth):
            return cx0 + par_x * depth * 12.0, cy0 + par_y * depth * 9.0

        def arc(c, rad, width, start_deg, span_deg, col, alpha, cap=Qt.PenCapStyle.FlatCap):
            pen = QPen(with_alpha(col, max(0.0, min(1.0, alpha))), width)
            pen.setCapStyle(cap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(QRectF(c[0] - rad, c[1] - rad, rad * 2, rad * 2),
                      int(start_deg * 16), int(span_deg * 16))

        def arc_text(c, text, rad, centre_deg, size, col, bottom=False):
            p.setFont(px_font(size, QFont.Weight.DemiBold, spacing=0))
            fm = p.fontMetrics()
            step = math.degrees((fm.horizontalAdvance("M") * 1.05) / max(1.0, rad))
            n = len(text)
            p.setPen(QPen(col))
            for i, ch in enumerate(text):
                off = (i - (n - 1) / 2) * step
                ang = centre_deg - off if bottom else centre_deg + off
                ar = math.radians(ang)
                p.save()
                p.translate(c[0] + math.cos(ar) * rad, c[1] + math.sin(ar) * rad)
                p.rotate(ang - 90 if bottom else ang + 90)
                p.drawText(QRectF(-8, -8, 16, 16), Qt.AlignmentFlag.AlignCenter, ch)
                p.restore()

        # light behind it
        b = self._boot(0.0, 0.3)
        c = at(1.0)
        glow = QRadialGradient(c[0], c[1], r * 1.1)
        glow.setColorAt(0.0, with_alpha(acc if self.speaking else main, min(0.4, 0.16 * gk) * b))
        glow.setColorAt(0.6, with_alpha(main, 0.04 * gk * b))
        glow.setColorAt(1.0, with_alpha(main, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow))
        p.drawEllipse(QPointF(c[0], c[1]), r * 1.1, r * 1.1)

        # 1 · bearing scale (drawn round as it builds)
        b = self._boot(0.62, 0.5)
        if b > 0:
            c = at(0.0)
            p.setPen(QPen(with_alpha(main, 0.35), 1))
            for i in range(int(180 * b)):
                deg = i * 2
                a = math.radians(deg - 90)
                long_ = deg % 10 == 0
                r0 = r * (0.965 if long_ else 0.98)
                p.drawLine(QLineF(c[0] + math.cos(a) * r0, c[1] + math.sin(a) * r0,
                                  c[0] + math.cos(a) * r, c[1] + math.sin(a) * r))
            if r > 150 and b >= 1.0:
                p.setFont(px_font(8, mono=True))
                p.setPen(QPen(with_alpha(main, 0.55)))
                for deg in range(0, 360, 30):
                    a = math.radians(deg - 90)
                    tx, ty = c[0] + math.cos(a) * r * 1.055, c[1] + math.sin(a) * r * 1.055
                    p.drawText(QRectF(tx - 16, ty - 7, 32, 14), Qt.AlignmentFlag.AlignCenter, f"{deg:03d}")
            # four reticle brackets just outside, turning slowly
            arc_r = r * 1.0 + 6
            for k in range(4):
                st = 45 + k * 90 + t * 2.5
                arc(c, arc_r, 2.2, st - 9, 18, main, 0.85 * b)

        # 2 · segmented ring with a running highlight
        b = self._boot(0.52)
        if b > 0:
            c = at(0.2)
            seg_r = r * 0.90 * (0.75 + 0.25 * b)
            head = (t * 22.0) % 360.0
            for i in range(36):
                st = i * 10 - t * 6
                d = ((head - (st % 360.0)) % 360.0)
                hot = max(0.0, 1.0 - d / 90.0)
                arc(c, seg_r, max(2.0, r * 0.012), st, 7.0, main,
                    (0.35 + (0.25 if i % 3 == 0 else 0) + 0.55 * hot) * b)

        # 3 · the readings: CPU left, memory right; name and release between
        b = self._boot(0.44)
        if b > 0:
            c = at(0.35)
            rr, wd = r * 0.80, max(5.0, r * 0.045)
            for side, value, label in ((-1, cpu, "CPU"), (1, mem, "MEM")):
                centre = 180.0 if side < 0 else 0.0
                start = centre - 55.0 if side < 0 else centre + 55.0
                span = 110.0 if side < 0 else -110.0
                arc(c, rr, wd, start, span, main, 0.10 * b)
                hot = qcol(C.AMB) if value >= 85 else main
                arc(c, rr, wd, start, span * value / 100.0 * b, hot, 0.9 * b)
                arc(c, rr, wd * 2.4, start, span * value / 100.0 * b, hot, 0.10 * b)
                if r > 120 and b >= 1.0:
                    inner_x = c[0] + side * (rr - wd - r * 0.035)
                    al = Qt.AlignmentFlag.AlignRight if side > 0 else Qt.AlignmentFlag.AlignLeft
                    bx = inner_x - 90 if side > 0 else inner_x
                    p.setFont(px_font(9, QFont.Weight.DemiBold, spacing=1.6))
                    p.setPen(QPen(with_alpha(main, 0.75)))
                    p.drawText(QRectF(bx, c[1] - 24, 90, 14), al | Qt.AlignmentFlag.AlignVCenter, label)
                    p.setFont(px_font(14, QFont.Weight.DemiBold, mono=True))
                    p.setPen(QPen(hot if value >= 85 else qcol(C.WHITE)))
                    p.drawText(QRectF(bx, c[1] - 9, 90, 20), al | Qt.AlignmentFlag.AlignVCenter, f"{value:.0f}%")
            if r > 140 and b >= 1.0:
                fs = max(8.0, min(11.0, r * 0.034))
                name = str(self._assistant_name or "").upper()[:18]
                arc_text(c, " ".join(name), rr, -90.0, fs, with_alpha(C.WHITE, 0.85))
                arc_text(c, "MARK  LIX", rr, 90.0, fs * 0.85, with_alpha(main, 0.6), bottom=True)
            if net and r > 120 and b >= 1.0:
                p.setFont(px_font(9, mono=True))
                p.setPen(QPen(with_alpha(main, 0.65)))
                p.drawText(QRectF(c[0] - 80, c[1] + r * 0.62, 160, 14), Qt.AlignmentFlag.AlignCenter, f"NET  {net}")

        # 4 · radar sweep
        b = self._boot(0.36)
        if b > 0:
            c = at(0.5)
            sweep_r = r * 0.70 * (0.7 + 0.3 * b)
            ang = (t * 40.0) % 360.0
            cg = QConicalGradient(c[0], c[1], -ang)
            cg.setColorAt(0.0, with_alpha(main, 0.22 * b))
            cg.setColorAt(0.12, with_alpha(main, 0.0))
            cg.setColorAt(1.0, with_alpha(main, 0.0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(cg))
            p.drawEllipse(QPointF(c[0], c[1]), sweep_r, sweep_r)
            arc(c, sweep_r, 1.0, 0.0, 360.0, main, 0.25 * b)

        # 5 · two tilted orbits, a light running on each
        b = self._boot(0.30)
        if b > 0:
            c = at(0.75)
            for k, (tilt, rx, ry, sp) in enumerate(((24.0, 0.68, 0.20, 0.9), (-38.0, 0.62, 0.17, -1.3))):
                p.save()
                p.translate(c[0], c[1])
                p.rotate(tilt + t * (2.0 if k == 0 else -1.6))
                pen = QPen(with_alpha(main, 0.30 * b), 1)
                pen.setDashPattern([3, 4])
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(0, 0), r * rx * b, r * ry * b)
                a = t * sp
                dx, dy = math.cos(a) * r * rx * b, math.sin(a) * r * ry * b
                dg = QRadialGradient(dx, dy, 7)
                dg.setColorAt(0.0, with_alpha(C.WHITE, 0.95 * b))
                dg.setColorAt(0.4, with_alpha(acc, 0.6 * b))
                dg.setColorAt(1.0, with_alpha(acc, 0.0))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(dg))
                p.drawEllipse(QPointF(dx, dy), 7, 7)
                p.restore()

        # 6 · three broken rings
        b = self._boot(0.22)
        if b > 0:
            c = at(0.6)
            for k, (rad, sp, n, alpha) in enumerate(((0.60, 18, 3, 0.7), (0.52, -26, 5, 0.5), (0.45, 34, 2, 0.8))):
                gap = 360.0 / n
                for j in range(n):
                    arc(c, r * rad * (0.7 + 0.3 * b), 1.6 if k != 2 else 3.0, j * gap + t * sp,
                        gap * 0.62 * b, acc if k == 2 else main, alpha * b)

        # 7 · hexagon frame
        b = self._boot(0.14)
        if b > 0:
            c = at(0.9)
            hr = r * 0.36 * b
            rot = t * 0.25 + amp * 0.4
            hexa = QPolygonF([QPointF(c[0] + math.cos(rot + i * math.pi / 3) * hr,
                                      c[1] + math.sin(rot + i * math.pi / 3) * hr) for i in range(6)])
            p.setPen(QPen(with_alpha(main, 0.55 * b), 1.3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPolygon(hexa)
            p.setPen(QPen(with_alpha(main, 0.25 * b), 1))
            for i in range(6):
                a = rot + i * math.pi / 3
                p.drawLine(QLineF(c[0] + math.cos(a) * hr, c[1] + math.sin(a) * hr,
                                  c[0] + math.cos(a) * hr * 1.18, c[1] + math.sin(a) * hr * 1.18))

        # 8 · voice bars
        b = self._boot(0.06)
        if b > 0:
            c = at(1.0)
            inner = r * 0.17
            lw = max(2.0, r * 0.011)
            for i in range(60):
                if i / 60 > b:
                    break
                a = i / 60 * math.tau - math.pi / 2
                idle = 0.10 + 0.06 * math.sin(t * 2.0 + i * 0.9)
                live = amp * (0.55 + 0.45 * abs(math.sin(t * 6.0 + i * 0.7)))
                h = r * 0.15 * max(idle, min(1.0, live * 1.7))
                pen = QPen(with_alpha(acc, 0.95 if amp > 0.04 else 0.55), lw)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                p.drawLine(QLineF(c[0] + math.cos(a) * inner, c[1] + math.sin(a) * inner,
                                  c[0] + math.cos(a) * (inner + h), c[1] + math.sin(a) * (inner + h)))

        # 9 · the core: lights first
        b = self._boot(0.0, 0.25)
        c = at(1.1)
        breathe = 1.0 + 0.05 * math.sin(t * 2.2) + 0.25 * amp
        cr = r * 0.13 * breathe * (0.3 + 0.7 * b)
        core = QRadialGradient(c[0], c[1], cr)
        core.setColorAt(0.0, with_alpha(C.WHITE, 0.97))
        core.setColorAt(0.35, with_alpha(acc, min(1.0, 0.75 * max(0.6, gk))))
        core.setColorAt(1.0, with_alpha(acc, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(core))
        p.drawEllipse(QPointF(c[0], c[1]), cr, cr)
        arc(c, r * 0.135 * (0.3 + 0.7 * b), 1.4, 0.0, 360.0, acc, 0.8 * b)

    def _paint_core_classic(self, p: QPainter, cx: float, cy: float, r: float,
                            W: float = 0.0, H: float = 0.0):
        """Draw the reactor at (cx, cy) with outer radius r, using the whole
        canvas (W x H) for the marks that frame it."""
        main, acc = self._core_colours()
        bg = qcol(C.BG)
        amp = self._amp_disp
        t = self._core_phase
        live = (self.speaking or amp > 0.04) and not self.muted

        def blend(col: QColor, a: float) -> QColor:
            """Pre-mix onto the background instead of asking Qt to composite.
            The raster engine's opaque path is several times faster than its
            translucent one, and everything here is a line or an arc."""
            k = max(0.0, min(1.0, a))
            return QColor(int(bg.red()   + (col.red()   - bg.red())   * k),
                          int(bg.green() + (col.green() - bg.green()) * k),
                          int(bg.blue()  + (col.blue()  - bg.blue())  * k))

        p.setBrush(Qt.BrushStyle.NoBrush)

        # 1. The atmosphere. One radial gradient doing what a stack of discs did
        #    badly: a wide, soft body of light that gives the thing presence
        #    before any detail is read. This single element decides whether the
        #    HUD looks vast or looks small, so it is drawn first and drawn big.
        # Concentrated rather than spread: a gradient reaching the outer rim
        # washes the whole disc a flat dim blue and reads as fog. Ending it at
        # two thirds leaves it a body of light with somewhere to fall off to,
        # which is what makes it look lit rather than tinted.
        lift = 1.0 + 0.55 * amp + (0.18 if self.speaking else 0.0)
        p.setPen(Qt.PenStyle.NoPen)
        for gr, a0, a1 in ((r * 0.70, 0.30, 0.0), (r * 0.34, 0.34, 0.0)):
            g = QRadialGradient(cx, cy, gr)
            g.setColorAt(0.00, blend(main, min(0.95, a0 * lift)))
            g.setColorAt(0.45, blend(main, min(0.95, a0 * lift * 0.52)))
            g.setColorAt(0.78, blend(main, min(0.95, a0 * lift * 0.18)))
            g.setColorAt(1.00, blend(main, a1))
            p.setBrush(QBrush(g))
            p.drawEllipse(QRectF(cx - gr, cy - gr, gr * 2, gr * 2))
        p.setBrush(Qt.BrushStyle.NoBrush)

        # 2. Frame marks at the corners of the whole canvas, not of the circle.
        #    They are what set the scale: the eye reads the reactor as filling
        #    the room rather than sitting in the middle of it.
        if W > 40 and H > 40:
            m, arm = min(W, H) * 0.035, min(W, H) * 0.055
            p.setPen(QPen(blend(main, 0.45), 1.4))
            for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
                x = cx + sx * (W / 2 - m)
                y = cy + sy * (H / 2 - m)
                p.drawLine(QLineF(x, y, x - sx * arm, y))
                p.drawLine(QLineF(x, y, x, y - sy * arm))

        # 3. Crosshair across the full canvas, broken around the core so it
        #    frames the reactor rather than crossing it.
        p.setPen(QPen(blend(main, 0.16), 1))
        gap = r * 0.62
        if W > 40:
            p.drawLine(QLineF(cx - W / 2, cy, cx - gap, cy))
            p.drawLine(QLineF(cx + gap, cy, cx + W / 2, cy))
        if H > 40:
            p.drawLine(QLineF(cx, cy - H / 2, cx, cy - gap))
            p.drawLine(QLineF(cx, cy + gap, cx, cy + H / 2))

        # 4. Two thin outer circles. Sparse on purpose — a dense ring reads as a
        #    grey band at this size, and restraint is what made the original
        #    look expensive.
        for rr, a in ((1.00, 0.34), (0.93, 0.16)):
            rad = r * rr
            p.setPen(QPen(blend(main, a), 1))
            p.drawEllipse(QRectF(cx - rad, cy - rad, rad * 2, rad * 2))

        # 5. Long, sparse graduations: 24 majors reaching well in from the rim,
        #    with shorter minors between them.
        major, minor = [], []
        for i in range(72):
            a = math.radians(i * 5.0)
            ca, sa = math.cos(a), math.sin(a)
            if i % 3 == 0:
                major.append(QLineF(cx + ca * r * 0.885, cy + sa * r * 0.885,
                                    cx + ca * r * 0.985, cy + sa * r * 0.985))
            else:
                minor.append(QLineF(cx + ca * r * 0.945, cy + sa * r * 0.945,
                                    cx + ca * r * 0.985, cy + sa * r * 0.985))
        p.setPen(QPen(blend(main, 0.42), 1.3))
        p.drawLines(major)
        p.setPen(QPen(blend(main, 0.18), 1))
        p.drawLines(minor)

        # 6. Sweeping arcs. Long spans, not dashes — the original's grandeur
        #    came from a few big strokes. Speed is the state: idle drifts,
        #    thinking hurries, speaking runs.
        rate = 1.0 + (1.9 if self.state in ("THINKING", "PROCESSING") else 0.0) \
                   + (1.2 if self.speaking else 0.0)
        for k, (rr, span, count, dirn, col, a, wid) in enumerate((
                (0.955, 118, 2, +1, acc,  0.75, 2.0),
                (0.845, 82,  3, -1, main, 0.38, 1.3),
                (0.760, 150, 1, +1, acc,  0.45, 1.6),
                (0.660, 64,  4, -1, main, 0.26, 1.1),
                (0.545, 128, 2, +1, main, 0.30, 1.2))):
            rad = r * rr
            p.setPen(QPen(blend(col, a), wid))
            box = QRectF(cx - rad, cy - rad, rad * 2, rad * 2)
            base = (t * rate * (9 + k * 6) * dirn) % 360.0
            for sgm in range(count):
                p.drawArc(box, int((base + sgm * (360.0 / count)) * 16),
                          int(span * 16))

        # 7. The voice, as a ring of graduations that grow with it. Kept out at
        #    a wide radius so it never crowds the middle.
        n = 60
        ring = r * 0.415
        spikes = []
        for i in range(n):
            a = math.radians(i * (360.0 / n))
            ca, sa = math.cos(a), math.sin(a)
            wob = 0.5 + 0.5 * math.sin(t * 2.3 + i * 0.42)
            idle = 0.018 + 0.012 * math.sin(t * 1.2 + i * 0.7)
            h = r * (idle + (amp * 0.20 * wob if live else 0.0))
            spikes.append(QLineF(cx + ca * ring, cy + sa * ring,
                                 cx + ca * (ring + h), cy + sa * (ring + h)))
        p.setPen(QPen(blend(acc if live else main, 0.25 + 0.5 * amp), 1.6))
        p.drawLines(spikes)

        # 8. The inner ring the name sits in.
        inner = r * 0.355
        p.setPen(QPen(blend(acc, 0.30 + 0.45 * amp), 1.5))
        p.drawEllipse(QRectF(cx - inner, cy - inner, inner * 2, inner * 2))

        # 9. The name, sized from the string rather than from the radius alone:
        #    "J.A.R.V.I.S" and a name someone renamed to "MAX" are very
        #    different widths, and a fixed fraction of r spills one of them past
        #    the ring it is supposed to sit inside.
        name = self._assistant_name or ""
        if name:
            space = max(1.0, r * 0.018)
            fsz = max(8, int(min(r * 0.105,
                                 (inner * 1.75) / max(1, len(name)) * 1.6 - space)))
            f = px_font(fsz * 1.33, QFont.Weight.Bold)
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, space)
            p.setFont(f)
            p.setPen(QPen(blend(qcol(C.WHITE), 0.6 + 0.4 * min(1.0, amp * 2)), 1))
            p.drawText(QRectF(cx - r, cy - fsz, r * 2, fsz * 2),
                       Qt.AlignmentFlag.AlignCenter, name)

    def _ground(self, p: QPainter, W: int, H: int) -> bool:
        """Copy this widget's piece of the window's ground under it, so the
        centre sits on the same picture as the panels. False if there is no
        such ground (an older window), and the old backdrop is drawn."""
        try:
            from gui.look import HudRoot
            root = self.window().centralWidget()
            if not isinstance(root, HudRoot):
                return False
            pm = root.picture()
            off = self.mapTo(root, QPoint(0, 0))
            d = pm.devicePixelRatio()
            p.drawPixmap(QRectF(0, 0, W, H), pm, QRectF(off.x() * d, off.y() * d, W * d, H * d))
            return True
        except Exception:
            return False

    def _paint_atmosphere(self, p: QPainter, W: int, H: int) -> None:
        """What moves behind the core: drifting motes, a slow scan band and
        pulses running in along the lines from the panels."""
        from gui.look import LOOK
        t = self._mphase
        p.setPen(Qt.PenStyle.NoPen)
        for (x0, y0, sp, sz, ph, white) in self._motes:
            y = ((y0 - t * sp * 0.018) % 1.0) * H
            x = x0 * W + math.sin(t * 0.3 + ph) * 8
            a = 0.10 + 0.22 * (0.5 + 0.5 * math.sin(t * 1.3 + ph))
            p.setBrush(QBrush(with_alpha(C.WHITE if white else C.PRI, a)))
            p.drawEllipse(QPointF(x, y), sz, sz)
        for i, line in enumerate(self._lines):
            if len(line) < 2 or LOOK.motion <= 0:
                continue
            segs = [(line[k], line[k + 1]) for k in range(len(line) - 1)]
            lens = [math.hypot(b.x() - a.x(), b.y() - a.y()) for a, b in segs]
            total = sum(lens) or 1.0
            f = ((t * 0.32 + i * 0.37) % 1.0)
            d = f * total
            for (a, b), ln in zip(segs, lens):
                if d <= ln:
                    k = d / max(1e-6, ln)
                    x, y = a.x() + (b.x() - a.x()) * k, a.y() + (b.y() - a.y()) * k
                    fade = math.sin(f * math.pi)
                    g = QRadialGradient(x, y, 8)
                    g.setColorAt(0.0, with_alpha(C.WHITE, 0.9 * fade))
                    g.setColorAt(0.35, with_alpha(C.PRI, 0.5 * fade))
                    g.setColorAt(1.0, with_alpha(C.PRI, 0.0))
                    p.setBrush(QBrush(g))
                    p.drawEllipse(QPointF(x, y), 8, 8)
                    break
                d -= ln

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():      # device not ready (e.g. 0-size during layout) — skip cleanly
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        from gui.look import LOOK
        W, H, cx, sy, band_t, band_h = self._band()
        hud = LOOK.hud and self._ground(p, W, H)
        if not hud:
            # The backdrop is one cached pixmap, rebuilt only when the size or
            # the theme changes — so live re-theming still repaints it.
            key = (W, H, C.PRI, C.BG, C.WHITE)
            if self._grid_cache is None or self._grid_key != key:
                self._grid_cache = self._make_grid(W, H)
                self._grid_key = key
            p.drawPixmap(0, 0, self._grid_cache)
        else:
            self._paint_atmosphere(p, W, H)
        fw = min(W, H)

        if self.hud_style == "deck":
            gx, gy, r = self.core_geometry()
            self._paint_deck(p, gx, gy, r)
        elif self._avatar is not None and self.hud_style == "face":
            r_head = min(fw * 0.355, band_h / (self._avatar.SPAN + 0.08))
            head_cy = band_t + (band_h - self._avatar.SPAN * r_head) / 2.0 + r_head
            if self.muted:
                main = acc = qcol(C.MUTED_C)
            else:
                main = qcol(C.PRI)
                if self.speaking:
                    acc = qcol(C.ACC)
                elif self.state in ("THINKING", "PROCESSING"):
                    acc = qcol(C.ACC2)
                elif self.state == "LISTENING":
                    acc = qcol(C.GREEN)
                else:
                    acc = qcol(C.PRI)
            self._avatar.paint(p, cx, head_cy, r_head, main, acc, qcol(C.BG))
        elif self._reactor is not None:
            ext = Reactor3D.TOP + Reactor3D.BOTTOM
            r = min(W * 0.25, band_h / ext)
            core_cy = band_t + (band_h - ext * r) / 2.0 + Reactor3D.TOP * r
            self._paint_core(p, cx, core_cy, r, W, band_h)
        else:
            r = min(W * 0.46, band_h / 2.0)
            self._paint_core_classic(p, cx, band_t + band_h / 2.0, r, W, band_h)

        # state line
        if self.muted:
            txt, col = "MUTED", qcol(C.MUTED_C)
        elif self.speaking:
            txt, col = "SPEAKING", qcol(C.AMB if self.hud_style == "deck" else C.ACC)
        elif self.state in ("THINKING", "PROCESSING"):
            txt, col = self.state, qcol(C.ACC2)
        elif self.state == "LISTENING":
            txt, col = "LISTENING", qcol(C.GREEN)
        else:
            txt, col = str(self.state), qcol(C.PRI)
        p.setFont(px_font(13, QFont.Weight.DemiBold, spacing=5))
        tw = p.fontMetrics().horizontalAdvance(txt)
        p.setPen(QPen(col))
        p.drawText(QRectF(0, sy, W, 22), Qt.AlignmentFlag.AlignCenter, txt)
        amp = self._amp_disp
        if hud:
            # brackets round the word, and an oscilloscope line under it
            bx = tw / 2 + 18
            p.setPen(QPen(with_alpha(col, 0.8), 1.4))
            for sg in (-1, 1):
                x = cx + sg * bx
                p.drawLine(QLineF(x, sy + 2, x, sy + 20))
                p.drawLine(QLineF(x, sy + 2, x - sg * 6, sy + 2))
                p.drawLine(QLineF(x, sy + 20, x - sg * 6, sy + 20))
                lg = QLinearGradient(x + sg * 8, 0, x + sg * 130, 0)
                lg.setColorAt(0.0, with_alpha(col, 0.6))
                lg.setColorAt(1.0, with_alpha(col, 0.0))
                p.setPen(QPen(QBrush(lg), 1))
                p.drawLine(QLineF(x + sg * 8, sy + 11, x + sg * 130, sy + 11))
                p.setPen(QPen(with_alpha(col, 0.8), 1.4))
            wy = sy + 50
            span = min(W * 0.62, 460.0)
            N = 120
            t = self._mphase * 2.08
            path = QPainterPath()
            for i in range(N + 1):
                u = i / N
                env = math.sin(u * math.pi) ** 1.5
                x = cx - span / 2 + u * span
                if self.muted:
                    yv = 0.0
                else:
                    wave = (math.sin(u * 23 + t * 7.0) * 0.55 + math.sin(u * 41 - t * 11.0) * 0.30
                            + math.sin(u * 9 + t * 3.0) * 0.15)
                    yv = env * (1.5 + amp * 26.0) * wave
                (path.moveTo if i == 0 else path.lineTo)(QPointF(x, wy - yv))
            base = col if (amp > 0.05 or self.muted) else qcol(C.PRI)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(with_alpha(base, 0.12), 5))
            p.drawPath(path)
            p.setPen(QPen(with_alpha(base, 0.9), 1.5))
            p.drawPath(path)
            p.setPen(QPen(with_alpha(base, 0.25), 1))
            p.drawLine(QLineF(cx - span / 2 - 20, wy, cx - span / 2 - 4, wy))
            p.drawLine(QLineF(cx + span / 2 + 4, wy, cx + span / 2 + 20, wy))
        else:
            pulse = 0.85 if (self._blink or self.speaking or self.muted) else 0.40
            rule = min(110.0, W * 0.12)
            for sgn in (-1, 1):
                x0 = cx + sgn * (tw / 2 + 14)
                lg = QLinearGradient(x0, 0, x0 + sgn * rule, 0)
                lg.setColorAt(0.0, with_alpha(col, pulse))
                lg.setColorAt(1.0, with_alpha(col, 0.0))
                p.setPen(QPen(QBrush(lg), 1))
                p.drawLine(QLineF(x0, sy + 11, x0 + sgn * rule, sy + 11))

            # waveform — the real audio level, mirrored below its baseline
            wy = sy + 50
            N = 48
            span = min(W * 0.60, 420.0)
            bw = span / N
            mid = (N - 1) / 2.0
            wx0 = cx - span / 2
            for i in range(N):
                if self.muted:
                    hgt, c = 1.5, qcol(C.MUTED_C)
                else:
                    env = (1.0 - abs(i - mid) / mid) ** 0.7
                    shimmer = 0.55 + 0.45 * math.sin(self._tick * 0.18 + i * 0.7)
                    idle = 2.0 + 1.5 * math.sin(self._tick * 0.09 + i * 0.6)
                    hgt = max(1.5, min(24.0, idle + amp * 22.0 * env * shimmer))
                    c = (blend(col, 0.35 + 0.65 * env) if amp > 0.05
                         else blend(C.PRI, 0.28 + 0.2 * env))
                x = wx0 + i * bw
                w = max(1.5, bw - 2.0)
                p.fillRect(QRectF(x, wy - hgt, w, hgt), c)
                p.fillRect(QRectF(x, wy + 1.5, w, hgt * 0.45), blend(c, 0.30))

        p.end()   # end deterministically so the backing store never flushes an active painter
