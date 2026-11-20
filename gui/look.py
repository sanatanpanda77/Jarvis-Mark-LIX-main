"""gui/look.py — Mark 58: how the interface looks, and the pieces that draw it.

  * LOOK: the user's appearance choices (memory/config_manager "ui_look"),
    read once and changed live from the Appearance panel;
  * HudRoot: the window's ground — one cached picture behind everything:
    light, a pattern, guide circles round the core, and the lines that tie
    each panel to the core;
  * Dock / DockButton: the round buttons along the bottom;
  * AppearanceOverlay: the panel where all of this is chosen.

Everything here is paint only: no panel, button or signal elsewhere changes
because of it."""
from __future__ import annotations

import math
import random

from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
    QPolygonF, QRadialGradient,
)
from PyQt6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider,
    QVBoxLayout, QWidget,
)

from gui.style import blend, px_font, with_alpha
from gui.theme import C, qcol

try:
    from memory.config_manager import UI_LOOK_DEFAULTS, get_ui_look, save_ui_look
except Exception:                                    # pragma: no cover
    UI_LOOK_DEFAULTS = {}
    get_ui_look = lambda: {}                         # noqa: E731
    save_ui_look = lambda _d: None                   # noqa: E731


class _Look(dict):
    """The appearance choices. A dict, read everywhere at paint time, so a
    change shows on the next frame without anything being rebuilt."""

    def __init__(self):
        super().__init__()
        self._listeners = []
        self.reload()

    def reload(self) -> None:
        try:
            self.update(get_ui_look())
        except Exception:
            self.update(UI_LOOK_DEFAULTS)

    def set(self, key: str, value) -> None:
        self[key] = value
        try:
            save_ui_look(dict(self))
        except Exception as e:                       # cosmetic: never fatal
            print(f"[Look] not saved: {e}")
        for fn in list(self._listeners):
            try:
                fn(key)
            except Exception as e:
                print(f"[Look] listener failed: {e}")

    def on_change(self, fn) -> None:
        self._listeners.append(fn)

    # read helpers
    @property
    def hud(self) -> bool:
        return self.get("panels", "hud") != "classic"

    @property
    def motion(self) -> float:
        return {"full": 1.0, "calm": 0.4, "off": 0.0}.get(self.get("motion", "full"), 1.0)

    @property
    def glow(self) -> float:
        return max(0.0, min(100, int(self.get("glow", 70)))) / 70.0

    @property
    def glass(self) -> float:
        return max(0.0, min(100, int(self.get("glass", 55)))) / 55.0

    def module_on(self, name: str) -> bool:
        return bool((self.get("modules") or {}).get(name, True))


LOOK = _Look()


# ── the ground behind everything ─────────────────────────────────────────────
class HudRoot(QWidget):
    """The window's central widget. Paints one cached picture; the panels on
    it are transparent, so it shows through them. Animated parts live in the
    HUD canvas, which copies its piece of this picture under itself."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pm: QPixmap | None = None
        self._key = None
        self._lines: list[list[QPointF]] = []
        self._core: tuple[float, float, float] | None = None

    def set_geometry_hints(self, lines, core) -> None:
        lines = [list(l) for l in (lines or [])]
        key = (tuple(tuple((round(q.x()), round(q.y())) for q in l) for l in lines),
               tuple(round(v) for v in core) if core else None)
        if key != getattr(self, "_hint_key", None):
            self._hint_key = key
            self._lines, self._core = lines, core
            self._key = None
            self.update()

    def picture(self) -> QPixmap:
        W, H = max(1, self.width()), max(1, self.height())
        dpr = self.devicePixelRatioF() or 1.0
        key = (W, H, dpr, C.PRI, C.BG, LOOK.get("backdrop"), LOOK.get("glow"),
               LOOK.hud, getattr(self, "_hint_key", None))
        if self._pm is None or self._key != key:
            self._pm = _render_ground(W, H, dpr, self._lines if LOOK.hud else [],
                                      self._core if LOOK.hud else None)
            self._key = key
        return self._pm

    def paintEvent(self, e):
        p = QPainter(self)
        if not p.isActive():
            return
        pm = self.picture()
        r = QRectF(e.rect())
        dpr = pm.devicePixelRatio()
        p.drawPixmap(r, pm, QRectF(r.x() * dpr, r.y() * dpr, r.width() * dpr, r.height() * dpr))
        p.end()


def _render_ground(W: int, H: int, dpr: float, lines, core) -> QPixmap:
    pm = QPixmap(int(W * dpr), int(H * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(qcol(C.BG))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = LOOK.glow
    ccx, ccy = (core[0], core[1]) if core else (W / 2, H * 0.45)

    # light: a wide soft glow where the core is
    lg = QRadialGradient(ccx, ccy, max(W, H) * 0.72)
    lg.setColorAt(0.0, blend(C.PRI, min(0.16, 0.085 * g)))
    lg.setColorAt(0.45, blend(C.PRI, min(0.06, 0.030 * g)))
    lg.setColorAt(1.0, qcol(C.BG))
    p.fillRect(QRectF(0, 0, W, H), QBrush(lg))

    kind = LOOK.get("backdrop", "grid")
    if kind == "grid":
        step = 32
        x = (W / 2) % step
        i = 0
        while x < W:
            major = round((x - W / 2) / step) % 4 == 0
            p.setPen(QPen(with_alpha(C.PRI, 0.055 if major else 0.026), 1))
            p.drawLine(QLineF(x, 0, x, H))
            x += step
            i += 1
        y = (H / 2) % step
        while y < H:
            major = round((y - H / 2) / step) % 4 == 0
            p.setPen(QPen(with_alpha(C.PRI, 0.055 if major else 0.026), 1))
            p.drawLine(QLineF(0, y, W, y))
            y += step
        p.setPen(QPen(with_alpha(C.PRI, 0.22), 1))
        x0, y0 = (W / 2) % (step * 4), (H / 2) % (step * 4)
        x = x0
        while x < W:
            y = y0
            while y < H:
                p.drawLine(QLineF(x - 3, y, x + 3, y))
                p.drawLine(QLineF(x, y - 3, x, y + 3))
                y += step * 4
            x += step * 4
    elif kind == "hex":
        s = 20.0
        h = s * math.sqrt(3)
        p.setPen(QPen(with_alpha(C.PRI, 0.045), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        row = 0
        y = -h
        while y < H + h:
            x = -s * 1.5 + (s * 1.5 if row % 2 else 0)
            while x < W + s * 3:
                p.drawPolygon(QPolygonF([QPointF(x + s * math.cos(math.radians(a)),
                                                 y + s * math.sin(math.radians(a)))
                                         for a in range(0, 360, 60)]))
                x += s * 3
            y += h / 2
            row += 1
    elif kind == "stars":
        rnd = random.Random(58)
        p.setPen(Qt.PenStyle.NoPen)
        for _ in range(int(W * H / 2200)):
            x, y = rnd.uniform(0, W), rnd.uniform(0, H)
            a = rnd.uniform(0.08, 0.55)
            r = rnd.uniform(0.5, 1.4)
            c = C.WHITE if rnd.random() < 0.35 else C.PRI
            p.setBrush(QBrush(with_alpha(c, a)))
            p.drawEllipse(QPointF(x, y), r, r)

    # vignette: the pattern fades out toward the edges
    vg = QRadialGradient(ccx, ccy, max(W, H) * 0.75)
    vg.setColorAt(0.0, with_alpha(C.BG, 0.0))
    vg.setColorAt(0.55, with_alpha(C.BG, 0.25))
    vg.setColorAt(1.0, with_alpha(C.BG, 0.90))
    p.fillRect(QRectF(0, 0, W, H), QBrush(vg))

    if core:
        cx, cy, r = core
        # guide circles: the core's rings carried out across the screen
        for k, a, dash in ((1.32, 0.10, (2, 6)), (1.62, 0.06, (1, 9)), (2.05, 0.04, (1, 14))):
            pen = QPen(with_alpha(C.PRI, a), 1)
            pen.setDashPattern(list(dash))
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), r * k, r * k)
        # cross-hair rules through the core, fading out
        for sgn in (-1, 1):
            hg = QLinearGradient(cx + sgn * r * 1.15, 0, cx + sgn * r * 2.3, 0)
            hg.setColorAt(0.0, with_alpha(C.PRI, 0.16))
            hg.setColorAt(1.0, with_alpha(C.PRI, 0.0))
            p.setPen(QPen(QBrush(hg), 1))
            p.drawLine(QLineF(cx + sgn * r * 1.15, cy, cx + sgn * r * 2.3, cy))

    # the lines from each panel to the core
    for line in lines:
        if len(line) < 2:
            continue
        path = QPainterPath(line[0])
        for q in line[1:]:
            path.lineTo(q)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(with_alpha(C.PRI, 0.07 * max(0.4, g)), 4))
        p.drawPath(path)
        p.setPen(QPen(with_alpha(C.PRI, 0.36), 1))
        p.drawPath(path)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(qcol(C.PRI)))
        p.drawEllipse(line[0], 2.6, 2.6)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(with_alpha(C.PRI, 0.8), 1.2))
        p.drawEllipse(line[-1], 3.2, 3.2)
        # a short tick across every bend
        for q in line[1:-1]:
            p.setPen(QPen(with_alpha(C.PRI, 0.55), 1))
            p.drawEllipse(q, 1.4, 1.4)

    # the screen's edges: rulers on both sides and brackets in the corners
    p.setPen(QPen(with_alpha(C.PRI, 0.16), 1))
    y = H * 0.22
    i = 0
    while y < H * 0.78:
        ln = 7 if i % 5 == 0 else 3
        p.drawLine(QLineF(2, y, 2 + ln, y))
        p.drawLine(QLineF(W - 2, y, W - 2 - ln, y))
        y += 9
        i += 1
    p.end()
    return pm


def connector(node: QPointF, side: int, cx: float, cy: float, R: float) -> list[QPointF]:
    """The path from a panel's node to the core's outer ring: along the
    panel's header line, then a 45° run, then a short straight into the
    ring. side = -1 for a panel on the left, +1 on the right."""
    dy = max(-0.82 * R, min(0.82 * R, node.y() - cy))
    ex = cx + side * math.sqrt(max(0.0, R * R - dy * dy))
    ey = cy + dy
    p2 = QPointF(ex + side * 22, ey)
    run = abs(node.y() - ey)
    p1x = p2.x() + side * run
    if side < 0:
        p1x = max(node.x() + 6, p1x)
    else:
        p1x = min(node.x() - 6, p1x)
    return [QPointF(node), QPointF(p1x, node.y()), p2, QPointF(ex, ey)]


# ── the dock ─────────────────────────────────────────────────────────────────
class DockButton(QPushButton):
    """A round button with a word under it."""

    def __init__(self, kind: str, label: str, parent=None):
        super().__init__(parent)
        self._kind, self._label = kind, label
        self.setFixedSize(66, 52)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet("QPushButton { background: transparent; border: none; }")

    def text(self) -> str:                               # noqa: D401
        return self._label

    def enterEvent(self, e):
        super().enterEvent(e)
        self.update()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self.update()

    def paintEvent(self, _):
        from gui.widgets import paint_icon
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W = self.width()
        hot = self.underMouse()
        on = self.isChecked() or self.isDown()
        col = qcol(C.PRI)
        cx, cy, r = W / 2, 18.0, 15.5
        g = QRadialGradient(cx, cy, r + 6)
        g.setColorAt(0.0, with_alpha(C.PRI, 0.30 if on else 0.16 if hot else 0.06))
        g.setColorAt(1.0, with_alpha(C.PRI, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawEllipse(QPointF(cx, cy), r + 6, r + 6)
        p.setBrush(QBrush(with_alpha(C.BG, 0.6)))
        p.setPen(QPen(with_alpha(C.PRI, 0.95 if (on or hot) else 0.42), 1.2))
        p.drawEllipse(QPointF(cx, cy), r, r)
        # two lit arcs on the ring
        pen = QPen(with_alpha(C.PRI, 1.0 if (on or hot) else 0.75), 2.2)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        p.setPen(pen)
        rr = QRectF(cx - r - 3, cy - r - 3, (r + 3) * 2, (r + 3) * 2)
        for st in (35, 215):
            p.drawArc(rr, st * 16, 40 * 16)
        paint_icon(p, self._kind, QRectF(cx - 8, cy - 8, 16, 16),
                   qcol(C.WHITE) if (on or hot) else col)
        p.setFont(px_font(8, QFont.Weight.DemiBold, spacing=1.4))
        p.setPen(QPen(qcol(C.WHITE) if (on or hot) else with_alpha(C.PRI, 0.75)))
        p.drawText(QRectF(0, 37, W, 14), Qt.AlignmentFlag.AlignCenter, self._label)
        p.end()


class Dock(QWidget):
    """The window's foot: status on the left, the round buttons in the
    middle on a raised plate, the shortcuts on the right. Keeps
    StatusStrip's set_items / set_right, so code that fed the old footer
    feeds this one."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(62)
        self._items: list[tuple[str, str]] = []
        self._right = ""
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 8, 0, 2)
        self._row.setSpacing(2)
        self._row.addStretch(1)
        self._buttons: list[DockButton] = []

    def add_button(self, b: DockButton) -> DockButton:
        self._row.insertWidget(self._row.count(), b)
        self._buttons.append(b)
        return b

    def finish(self) -> None:
        self._row.addStretch(1)

    def set_items(self, items) -> None:
        items = [(str(a), str(b)) for a, b in items]
        if items != self._items:
            self._items = items
            self.update()

    def set_right(self, text: str) -> None:
        self._right = str(text or "")
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        if self._buttons:
            x0 = min(b.x() for b in self._buttons) - 18
            x1 = max(b.x() + b.width() for b in self._buttons) + 18
        else:
            x0 = x1 = W / 2
        base = 16.0
        # the plate the buttons sit on
        plate = QPolygonF([QPointF(x0 - 20, base), QPointF(x0, 3), QPointF(x1, 3),
                           QPointF(x1 + 20, base), QPointF(x1 + 20, H), QPointF(x0 - 20, H)])
        pg = QLinearGradient(0, 0, 0, H)
        pg.setColorAt(0.0, with_alpha(C.PRI, 0.10))
        pg.setColorAt(1.0, with_alpha(C.PRI, 0.02))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(pg))
        p.drawPolygon(plate)
        # the rule along the top, rising over the plate
        lg = QLinearGradient(0, 0, W, 0)
        lg.setColorAt(0.0, with_alpha(C.PRI, 0.05))
        lg.setColorAt(0.5, with_alpha(C.PRI, 0.55))
        lg.setColorAt(1.0, with_alpha(C.PRI, 0.05))
        p.setPen(QPen(QBrush(lg), 1))
        p.drawPolyline(QPolygonF([QPointF(0, base), QPointF(x0 - 20, base), QPointF(x0, 3),
                                  QPointF(x1, 3), QPointF(x1 + 20, base), QPointF(W, base)]))
        p.setPen(QPen(qcol(C.PRI), 2))
        p.drawLine(QLineF(x0 + 8, 3, x0 + 48, 3))
        p.drawLine(QLineF(x1 - 48, 3, x1 - 8, 3))
        # status on the left, shortcuts on the right, in the band under the rule
        p.setFont(px_font(9, QFont.Weight.DemiBold, spacing=1.3))
        fm = p.fontMetrics()
        ty = base + (H - base) / 2
        x = 16.0
        for label, colour in self._items:
            w = fm.horizontalAdvance(label)
            if x + w + 12 > x0 - 30:
                break
            c = getattr(C, colour, None) if not colour.startswith("#") else colour
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(qcol(c or C.PRI)))
            p.drawEllipse(QPointF(x + 3, ty), 2.6, 2.6)
            p.setPen(QPen(qcol(C.TEXT_MED)))
            p.drawText(QRectF(x + 11, ty - 8, w + 4, 16), Qt.AlignmentFlag.AlignVCenter, label)
            x += w + 30
        room = W - (x1 + 40) - 16
        if room > 40:
            p.setPen(QPen(qcol(C.TEXT_DIM)))
            p.drawText(QRectF(x1 + 40, ty - 8, room, 16),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       fm.elidedText(self._right, Qt.TextElideMode.ElideLeft, int(room)))
        p.end()


# ── the Appearance panel ─────────────────────────────────────────────────────
ACCENTS = (("CYAN", "#00d4ff"), ("ICE", "#8fe3ff"), ("EMERALD", "#00ffa3"),
           ("GOLD", "#ffc233"), ("CRIMSON", "#ff3b5c"), ("VIOLET", "#a879ff"),
           ("WHITE", "#d8e6ee"))


class _Seg(QWidget):
    """A row of equal buttons, one of them chosen."""
    picked = pyqtSignal(str)

    def __init__(self, options, current: str, parent=None):
        super().__init__(parent)
        self._btns = {}
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        for value, label in options:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setFixedHeight(28)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setFont(px_font(10, QFont.Weight.DemiBold, spacing=1.2))
            b.clicked.connect(lambda _c=False, v=value: self.choose(v, emit=True))
            row.addWidget(b, 1)
            self._btns[value] = b
        self.choose(current)

    def choose(self, value: str, emit: bool = False) -> None:
        for v, b in self._btns.items():
            on = v == value
            b.setChecked(on)
            b.setStyleSheet(
                f"QPushButton {{ background: {C.PRI_GHO if on else 'transparent'};"
                f" color: {C.WHITE if on else C.TEXT_DIM};"
                f" border: 1px solid {C.PRI if on else C.BORDER_A}; border-radius: 0px; }}"
                f"QPushButton:hover {{ color: {C.WHITE}; border-color: {C.PRI}; }}")
        if emit:
            self.picked.emit(value)


class _Swatch(QPushButton):
    def __init__(self, name: str, hex_: str, parent=None):
        super().__init__(parent)
        self.hex, self.name = hex_, name
        self.setFixedSize(30, 30)
        self.setToolTip(name.title())
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet("QPushButton { background: transparent; border: none; }")
        self.on = False

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(self.hex)
        g = QRadialGradient(15, 15, 13)
        g.setColorAt(0.0, c)
        g.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), 60))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawEllipse(QPointF(15, 15), 9, 9)
        if self.on or self.underMouse():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(C.WHITE) if self.on else c, 1.5))
            p.drawEllipse(QPointF(15, 15), 13, 13)
        p.end()


class AppearanceOverlay(QFrame):
    """Every choice about the look, applied as it is made and saved."""
    look_changed = pyqtSignal(str)        # a LOOK key
    centre_picked = pyqtSignal(str)       # hud_style
    accent_picked = pyqtSignal(str)       # "#rrggbb"
    custom_colour = pyqtSignal()          # open the colour wheel
    closed = pyqtSignal()

    def __init__(self, centre: str, accent: str, parent=None):
        super().__init__(parent)
        self.setObjectName("AppearanceOverlay")
        self.setStyleSheet(f"#AppearanceOverlay {{ background: {C.BG}; border: 1px solid {C.BORDER_B};"
                           f" border-radius: 0px; }}")
        self.setFixedWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(10)

        head = QHBoxLayout()
        t = QLabel("APPEARANCE")
        t.setFont(px_font(14, QFont.Weight.DemiBold, spacing=4))
        t.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        head.addWidget(t)
        head.addStretch()
        x = QPushButton("DONE")
        x.setFixedSize(80, 28)
        x.setCursor(Qt.CursorShape.PointingHandCursor)
        x.setFont(px_font(10, QFont.Weight.DemiBold, spacing=1.4))
        x.setStyleSheet(f"QPushButton {{ color: {C.BG}; background: {C.PRI}; border: none; }}"
                        f"QPushButton:hover {{ background: {C.WHITE}; }}")
        x.clicked.connect(self._close)
        head.addWidget(x)
        lay.addLayout(head)
        sub = QLabel("Changes show at once and are kept.")
        sub.setFont(px_font(10))
        sub.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        lay.addWidget(sub)

        # Everything below the title scrolls, so the panel fits any window.
        from PyQt6.QtWidgets import QScrollArea
        from gui.style import scroll_css
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 8, 0)
        bl.setSpacing(10)
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)
        rowi = [0]

        def row(label: str, w: QWidget):
            lb = QLabel(label)
            lb.setFont(px_font(10, QFont.Weight.DemiBold, spacing=1.8))
            lb.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            grid.addWidget(lb, rowi[0], 0, Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(w, rowi[0], 1)
            rowi[0] += 1

        self._centre = _Seg((("deck", "ARC CORE"), ("core", "REACTOR"), ("face", "FACE")), centre)
        self._centre.picked.connect(self.centre_picked.emit)
        row("CENTRE", self._centre)

        sw_box = QWidget()
        sw = QHBoxLayout(sw_box)
        sw.setContentsMargins(0, 0, 0, 0)
        sw.setSpacing(2)
        self._swatches = []
        for name, hx in ACCENTS:
            s = _Swatch(name, hx)
            s.clicked.connect(lambda _c=False, h=hx: self._pick_accent(h))
            sw.addWidget(s)
            self._swatches.append(s)
        more = QPushButton("CUSTOM…")
        more.setFixedHeight(26)
        more.setCursor(Qt.CursorShape.PointingHandCursor)
        more.setFont(px_font(9, QFont.Weight.DemiBold, spacing=1.2))
        more.setStyleSheet(f"QPushButton {{ color: {C.TEXT_DIM}; background: transparent;"
                           f" border: 1px solid {C.BORDER_A}; padding: 0 8px; }}"
                           f"QPushButton:hover {{ color: {C.WHITE}; border-color: {C.PRI}; }}")
        more.clicked.connect(self.custom_colour.emit)
        sw.addStretch()
        sw.addWidget(more)
        row("COLOUR", sw_box)
        self._mark_accent(accent)

        def seg(key, options, label):
            s = _Seg(options, LOOK.get(key))
            s.picked.connect(lambda v, k=key: self._set(k, v))
            row(label, s)
            return s

        seg("panels", (("hud", "HUD"), ("classic", "CLASSIC")), "PANELS")
        seg("backdrop", (("grid", "GRID"), ("hex", "HEX"), ("stars", "STARS"), ("plain", "PLAIN")),
            "BACKGROUND")
        seg("motion", (("full", "FULL"), ("calm", "CALM"), ("off", "STILL")), "MOTION")

        def slider(key, label):
            s = QSlider(Qt.Orientation.Horizontal)
            s.setRange(0, 100)
            s.setValue(int(LOOK.get(key, 50)))
            s.setStyleSheet(
                f"QSlider::groove:horizontal {{ height: 2px; background: {C.BORDER_A}; }}"
                f"QSlider::sub-page:horizontal {{ background: {C.PRI}; }}"
                f"QSlider::handle:horizontal {{ width: 12px; height: 12px; margin: -6px 0;"
                f" background: {C.BG}; border: 2px solid {C.PRI}; border-radius: 7px; }}")
            s.valueChanged.connect(lambda v, k=key: self._set(k, int(v), save=False))
            s.sliderReleased.connect(lambda k=key, w=s: self._set(k, int(w.value())))
            row(label, s)
            return s

        slider("glow", "GLOW")
        slider("glass", "PANEL GLASS")

        def toggles(pairs, label, getter, setter):
            box = QWidget()
            h = QHBoxLayout(box)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(4)
            for key, text in pairs:
                b = QPushButton(text)
                b.setCheckable(True)
                b.setChecked(getter(key))
                b.setFixedHeight(28)
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                b.setFont(px_font(9, QFont.Weight.DemiBold, spacing=1.0))

                def paint(btn=b):
                    on = btn.isChecked()
                    btn.setStyleSheet(
                        f"QPushButton {{ background: {C.PRI_GHO if on else 'transparent'};"
                        f" color: {C.WHITE if on else C.TEXT_DIM};"
                        f" border: 1px solid {C.PRI if on else C.BORDER_A}; }}"
                        f"QPushButton:hover {{ border-color: {C.PRI}; }}")
                paint()
                b.toggled.connect(lambda on, k=key, fn=paint: (fn(), setter(k, on)))
                h.addWidget(b, 1)
            row(label, box)

        toggles((("parallax", "PARALLAX"), ("scanlines", "SCANLINES"), ("boot", "START-UP")),
                "EFFECTS", lambda k: bool(LOOK.get(k, True)), self._set)
        toggles((("systems", "SYSTEMS"), ("windows", "APPS"), ("network", "NETWORK"),
                 ("status", "STATUS"), ("tasks", "TASKS")),
                "PANELS SHOWN", LOOK.module_on, self._set_module)
        bl.addLayout(grid)

        foot = QHBoxLayout()
        reset = QPushButton("RESET TO DEFAULT")
        reset.setFixedHeight(26)
        reset.setCursor(Qt.CursorShape.PointingHandCursor)
        reset.setFont(px_font(9, QFont.Weight.DemiBold, spacing=1.2))
        reset.setStyleSheet(f"QPushButton {{ color: {C.TEXT_DIM}; background: transparent;"
                            f" border: none; text-align: left; }}"
                            f"QPushButton:hover {{ color: {C.WHITE}; }}")
        reset.clicked.connect(self._reset)
        foot.addWidget(reset)
        foot.addStretch()
        bl.addLayout(foot)
        bl.addStretch()
        sc = QScrollArea()
        sc.setWidget(body)
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.Shape.NoFrame)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sc.setStyleSheet("QScrollArea { background: transparent; border: none; }" + scroll_css())
        sc.viewport().setStyleSheet("background: transparent;")
        lay.addWidget(sc, 1)

    def sizeHint(self):
        from PyQt6.QtCore import QSize
        return QSize(520, 640)

    def _set(self, key: str, value, save: bool = True) -> None:
        if save:
            LOOK.set(key, value)
        else:
            LOOK[key] = value
            for fn in list(LOOK._listeners):
                try:
                    fn(key)
                except Exception:
                    pass
        self.look_changed.emit(key)

    def _set_module(self, name: str, on: bool) -> None:
        mods = dict(LOOK.get("modules") or {})
        mods[name] = bool(on)
        self._set("modules", mods)

    def _pick_accent(self, hx: str) -> None:
        self._mark_accent(hx)
        self.accent_picked.emit(hx)

    def _mark_accent(self, hx: str) -> None:
        for s in self._swatches:
            s.on = s.hex.lower() == str(hx or "").lower()
            s.update()

    def _reset(self) -> None:
        for k, v in UI_LOOK_DEFAULTS.items():
            LOOK[k] = dict(v) if isinstance(v, dict) else v
        LOOK.set("panels", LOOK["panels"])
        self.centre_picked.emit("deck")
        self.accent_picked.emit(ACCENTS[0][1])
        self.look_changed.emit("*")
        self._close(rebuild=True)

    def _close(self, rebuild: bool = False) -> None:
        self.setProperty("reopen", bool(rebuild))
        self.hide()
        self.closed.emit()
        self.deleteLater()
