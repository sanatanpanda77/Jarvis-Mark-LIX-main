"""gui/lock_screen.py — what covers the window while core/inputlock is held.

The whole central widget, on top of everything in it: nothing under it can be
clicked, typed into or tabbed to (its siblings are disabled while it shows and
given back exactly as they were). It shows what the holder sends - a title, a
line and a picture (Face Lock's camera view) - and, for a real lock, the way
back in: the licence key this computer is signed in with.

The holder's words are English and are shown in the interface language
(core/i18n.phrase: shipped from PLUGIN "phrases", or learnt when this install
can translate), with their {placeholders} filled afterwards.
"""
from __future__ import annotations

import threading

from core.qt.QtCore import QEvent, QObject, QRectF, Qt, QTimer, Signal
from core.qt.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from core.qt.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from core import i18n, inputlock
from core.i18n import t
from gui.style import hud_font
from gui.theme import C

_TONE = {"idle": "TEXT", "busy": "ACC2", "ok": "GREEN", "bad": "RED"}


def _filled(english: str, values: dict) -> str:
    text = i18n.phrase(english)
    try:
        return text.format(**values) if values else text
    except (KeyError, IndexError, ValueError):
        return english.format(**values) if values else english


class _Bridge(QObject):
    """core/inputlock calls from any thread; the screen changes on the Qt one."""
    show = Signal(object)
    hide = Signal()
    frame = Signal(bytes)
    relabel = Signal()


class LockScreen(QWidget):
    _RAISE_MS = 400

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("LockScreen")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.hide()
        self._disabled: list[QWidget] = []
        self._state: dict = {}
        self._pixmap: QPixmap | None = None
        self._learning: set[str] = set()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 28, 40, 24)
        lay.setSpacing(10)

        self._badge = QLabel("🔒  " + t("ui.lock.locked"))
        self._badge.setFont(hud_font(9, QFont.Weight.Bold))
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._badge.setStyleSheet(f"color: {C.ACC}; background: transparent; letter-spacing: 4px;")
        lay.addWidget(self._badge)

        self._title = QLabel()
        self._title.setFont(hud_font(17, QFont.Weight.Bold))
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title.setWordWrap(True)
        self._title.setStyleSheet(f"color: {C.PRI}; background: transparent; letter-spacing: 3px;")
        lay.addWidget(self._title)

        self._view = QLabel()
        self._view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._view.setMinimumSize(240, 180)
        self._view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._view.setStyleSheet("background: transparent;")
        lay.addWidget(self._view, 1)

        self._line = QLabel()
        self._line.setFont(hud_font(12, QFont.Weight.Bold))
        self._line.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._line.setWordWrap(True)
        lay.addWidget(self._line)

        # The way back in: a link, then the key's field.
        self._other = QPushButton(t("ui.lock.other_way"))
        self._other.setFlat(True)
        self._other.setCursor(Qt.CursorShape.PointingHandCursor)
        self._other.setFont(hud_font(8))
        self._other.setStyleSheet(f"QPushButton {{ color: {C.TEXT_DIM}; background: transparent; "
                                  f"border: none; text-decoration: underline; }}"
                                  f"QPushButton:hover {{ color: {C.PRI}; }}")
        self._other.clicked.connect(self._open_key)
        lay.addWidget(self._other, 0, Qt.AlignmentFlag.AlignCenter)

        self._key_row = QWidget()
        self._key_row.setStyleSheet("background: transparent;")
        row = QHBoxLayout(self._key_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self._key = QLineEdit()
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.setPlaceholderText(t("ui.lock.licence_hint"))
        self._key.setFixedWidth(300)
        self._key.setFont(hud_font(9))
        self._key.setStyleSheet(f"QLineEdit {{ background: {C.DARK}; color: {C.TEXT}; "
                                f"border: 1px solid {C.BORDER_B}; border-radius: 3px; padding: 6px; }}")
        self._key.returnPressed.connect(self._try_key)
        self._go = QPushButton(t("ui.lock.unlock"))
        self._go.setCursor(Qt.CursorShape.PointingHandCursor)
        self._go.setFont(hud_font(8, QFont.Weight.Bold))
        self._go.setFixedHeight(30)
        self._go.setStyleSheet(f"QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI}; "
                               f"border: 1px solid {C.PRI}; border-radius: 3px; padding: 0 14px; }}")
        self._go.clicked.connect(self._try_key)
        row.addStretch(1)
        row.addWidget(self._key)
        row.addWidget(self._go)
        row.addStretch(1)
        self._key_row.hide()
        lay.addWidget(self._key_row)

        self._key_msg = QLabel()
        self._key_msg.setFont(hud_font(8))
        self._key_msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._key_msg.setStyleSheet(f"color: {C.RED}; background: transparent;")
        self._key_msg.hide()
        lay.addWidget(self._key_msg)

        self._bridge = _Bridge(self)
        self._bridge.show.connect(self._on_show)
        self._bridge.hide.connect(self._on_hide)
        self._bridge.frame.connect(self._on_frame)
        self._bridge.relabel.connect(self._relabel)
        self._raiser = QTimer(self)
        self._raiser.timeout.connect(self._keep_on_top)
        parent.installEventFilter(self)
        inputlock.bind_screen(self._bridge.show.emit, self._bridge.hide.emit,
                              self._bridge.frame.emit)

    # ── what the holder sends ────────────────────────────────────────────────
    def _on_show(self, state: dict) -> None:
        self._state = dict(state or {})
        values = self._state.get("values") or {}
        self._title.setText(_filled(self._state.get("title") or "", values).upper())
        self._line.setText(_filled(self._state.get("line") or "", values))
        color = getattr(C, _TONE.get(self._state.get("tone") or "idle", "TEXT"))
        self._line.setStyleSheet(f"color: {color}; background: transparent;")
        locked = bool(self._state.get("fallback"))
        self._badge.setVisible(locked)
        self._other.setVisible(locked and not self._key_row.isVisible())
        if not locked:
            self._key_row.hide()
            self._key_msg.hide()
        self._learn(self._state.get("title"), self._state.get("line"))
        if not self.isVisible():
            self._cover()

    def _on_frame(self, data: bytes) -> None:
        px = QPixmap()
        if px.loadFromData(data):
            self._pixmap = px
            self._fit()

    def _on_hide(self) -> None:
        self._raiser.stop()
        self.hide()
        self._key.clear()
        self._key_row.hide()
        self._key_msg.hide()
        self._view.clear()
        self._pixmap = None
        for w in self._disabled:
            try:
                w.setEnabled(True)
            except RuntimeError:
                pass                                   # gone meanwhile
        self._disabled = []

    def _learn(self, *english) -> None:
        """A line not known in this language yet: translated in the background
        when this install can (gui/phrases), then shown again."""
        todo = [e for e in english if e and e not in self._learning]
        if not todo:
            return
        self._learning.update(todo)
        try:
            from gui.phrases import _can_translate
            missing = i18n.missing_phrases(todo)
        except (ImportError, AttributeError):
            return                     # no phrase catalog here: English is shown
        if not missing or not _can_translate():
            return
        tag = i18n.current()

        def work():
            if i18n.learn_phrases(missing, tag):
                self._bridge.relabel.emit()
        threading.Thread(target=work, daemon=True, name="lock-phrases").start()

    def _relabel(self) -> None:
        if self.isVisible() and self._state:
            self._on_show(self._state)

    # ── covering the window ──────────────────────────────────────────────────
    def _cover(self) -> None:
        parent = self.parentWidget()
        self.setGeometry(parent.rect())
        self.show()
        self.raise_()
        self._disable_rest()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self._raiser.start(self._RAISE_MS)

    def _disable_rest(self) -> None:
        """Every sibling switched off - so focus, typing and shortcuts tied to
        them cannot reach the window under the screen. Only those switched off
        here are switched on again."""
        parent = self.parentWidget()
        for w in parent.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
            if w is not self and w.isEnabled():
                w.setEnabled(False)
                self._disabled.append(w)

    def _keep_on_top(self) -> None:
        # Something opened later (a notice, an update banner) goes under it.
        self.raise_()
        self._disable_rest()

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize and self.isVisible():
            self.setGeometry(obj.rect())
        return super().eventFilter(obj, event)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def _fit(self) -> None:
        if self._pixmap is None:
            return
        w, h = self._view.width(), self._view.height()
        if w > 1 and h > 1:
            self._view.setPixmap(self._pixmap.scaled(
                w, h, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(0, 5, 9, 250))
        color = getattr(C, _TONE.get(self._state.get("tone") or "idle", "TEXT"))
        if (self._state.get("tone") or "idle") == "idle":
            color = C.PRI
        pen = QPen(QColor(color))
        pen.setWidthF(2.0)
        p.setPen(pen)
        r = QRectF(self.rect()).adjusted(14, 14, -14, -14)
        n = min(46.0, r.width() / 6, r.height() / 6)
        for (x, y, dx, dy) in ((r.left(), r.top(), 1, 1), (r.right(), r.top(), -1, 1),
                               (r.left(), r.bottom(), 1, -1), (r.right(), r.bottom(), -1, -1)):
            p.drawLine(int(x), int(y), int(x + dx * n), int(y))
            p.drawLine(int(x), int(y), int(x), int(y + dy * n))
        p.end()

    # ── the way back in ──────────────────────────────────────────────────────
    def _open_key(self) -> None:
        self._other.hide()
        self._key_row.show()
        self._key.setFocus()

    def _try_key(self) -> None:
        typed = self._key.text()
        self._key.clear()
        if not typed.strip():
            return
        ok, wait = inputlock.unlock_with_licence(typed)
        if ok:
            return                                       # hide() comes from the lock
        self._key_msg.setText(t("ui.lock.wait", seconds=wait) if wait else t("ui.lock.wrong"))
        self._key_msg.show()

    def keyPressEvent(self, e):
        e.accept()                                       # nothing passes to the window
