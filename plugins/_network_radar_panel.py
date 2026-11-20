"""
plugins/_network_radar_panel.py — Sci-Fi Visual Network Radar Panel for JARVIS HUD.

Renders an animated 360-degree radar scope, live ping telemetry, IP configuration,
and discovered LAN devices directly on the central HUD glass canvas.
"""
from __future__ import annotations

import math
import platform
import socket
import threading
import time
from typing import Optional, List, Tuple

from PyQt6.QtCore import Qt, QRectF, QPointF
from PyQt6.QtGui import QPainter, QColor, QPen, QBrush, QConicalGradient, QPolygonF

from plugins import _live_panels_core as lb
from plugins import network_radar as nr

_OS = platform.system()

# UI Color palette
BG   = (6, 10, 16)
TEXT = (226, 238, 248)
DIM  = (118, 138, 158)
WARN = (255, 196, 64)
HOT  = (255, 84, 96)
OK   = (64, 230, 130)


def _q(rgb, a: int = 255) -> QColor:
    return QColor(int(rgb[0]), int(rgb[1]), int(rgb[2]), int(a))


class _RadarSampler:
    """Background polling worker for network telemetry (non-blocking)."""

    def __init__(self):
        self.stop_ev = threading.Event()
        self.lock = threading.Lock()
        self.local_ip = "127.0.0.1"
        self.public_ip = "Fetching..."
        self.wifi_info = {"ssid": "Scanning...", "signal": "—", "type": "Detecting..."}
        self.current_ping = 0.0
        self.ping_history: List[float] = [12.0] * 30
        self.lan_devices: List[str] = []
        self.listening_ports: List[str] = []
        self.last_update = 0.0
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self.stop_ev.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="radar-sampler")
        self._thread.start()

    def stop(self):
        self.stop_ev.set()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "local_ip": self.local_ip,
                "public_ip": self.public_ip,
                "wifi": dict(self.wifi_info),
                "ping": self.current_ping,
                "history": list(self.ping_history),
                "devices": list(self.lan_devices),
                "ports": list(self.listening_ports),
            }

    def _run(self):
        # Initial quick probe
        try:
            loc = nr._get_local_ip()
            wf = nr._get_wifi_info()
            with self.lock:
                self.local_ip = loc
                self.wifi_info = wf
        except Exception:
            pass

        try:
            pub = nr._get_public_ip()
            with self.lock:
                self.public_ip = pub
        except Exception:
            pass

        lan_timer = 0.0
        ports_timer = 0.0

        while not self.stop_ev.is_set():
            now = time.time()

            # Fast socket ping check every 2 seconds
            ms = nr._measure_socket_ping("1.1.1.1", 53, timeout=1.2)
            if ms is None:
                ms = nr._measure_socket_ping("8.8.8.8", 53, timeout=1.2)
            if ms is None:
                ms = 999.0

            with self.lock:
                self.current_ping = ms
                self.ping_history.append(ms)
                if len(self.ping_history) > 40:
                    self.ping_history.pop(0)

            # Refresh Wi-Fi details every 6 seconds
            if now - self.last_update > 6.0:
                self.last_update = now
                try:
                    wf = nr._get_wifi_info()
                    with self.lock:
                        self.wifi_info = wf
                except Exception:
                    pass

            # Scan local LAN devices every 20 seconds
            if now - lan_timer > 20.0 or not self.lan_devices:
                lan_timer = now
                try:
                    devs = nr._scan_arp_devices()
                    with self.lock:
                        self.lan_devices = devs
                except Exception:
                    pass

            # Refresh listening ports every 15 seconds
            if now - ports_timer > 15.0 or not self.listening_ports:
                ports_timer = now
                try:
                    pts = nr._get_listening_ports(limit=12)
                    with self.lock:
                        self.listening_ports = pts
                except Exception:
                    pass

            for _ in range(20):
                if self.stop_ev.is_set():
                    break
                time.sleep(0.1)


class NetworkRadarBoard(lb.Board):
    """Futuristic 360-degree radar scope and network intelligence HUD panel."""

    def __init__(self, acc, parent=None):
        super().__init__(acc, parent)
        self.sampler = _RadarSampler()
        self.tab = "devices"  # "devices" or "ports"
        self.sweep_angle = 0.0

    def on_open(self) -> None:
        self.sampler.start()

    def on_close(self) -> None:
        self.sampler.stop()

    def animating(self) -> bool:
        return True

    def title(self):
        data = self.sampler.snapshot()
        wf = data["wifi"]
        ssid = wf.get("ssid") or "Wired"
        if ssid == "Unknown":
            ssid = "LAN Active"
        return (
            "Network Radar",
            f"WI-FI: {ssid}  ·  LOCAL: {data['local_ip']}  ·  PING: {data['ping']:.1f} ms  ·  {time.strftime('%H:%M:%S')}"
        )

    def clicked(self, key: str) -> None:
        if key.startswith("tab:"):
            self.tab = key[4:]

    def draw(self, p: QPainter, dt: float, now: float) -> None:
        data = self.sampler.snapshot()
        self.sweep_angle = (self.sweep_angle + dt * 110.0) % 360.0

        try:
            # Layout coordinates (Canvas: 1600 x 1000)
            # Left side: Radar Scope (cx=420, cy=580, R=310)
            # Right side: Metric cards and data tabs
            self._draw_radar_scope(p, 420.0, 580.0, 310.0, now, data)
        except Exception as e:
            print(f"[NetworkRadar] Radar scope paint error: {e}")

        try:
            self._draw_status_cards(p, 800.0, 180.0, 730.0, data)
        except Exception as e:
            print(f"[NetworkRadar] Status cards paint error: {e}")

        try:
            self._draw_bottom_tabs(p, 800.0, 510.0, 730.0, 420.0, data)
        except Exception as e:
            print(f"[NetworkRadar] Bottom tabs paint error: {e}")

    # ── 1. Animated Radar Scope ──────────────────────────────────────────────
    def _draw_radar_scope(self, p: QPainter, cx: float, cy: float, r: float, now: float, data: dict) -> None:
        # Outer Radar Housing Ring
        p.setPen(QPen(_q(self.acc, 70), 2))
        p.setBrush(_q(BG, 220))
        p.drawEllipse(QPointF(cx, cy), r + 20, r + 20)

        # Concentric Radar Range Rings
        rings = [(0.25, "25ms"), (0.50, "50ms"), (0.75, "100ms"), (1.00, "200ms")]
        for frac, label in rings:
            rr = r * frac
            p.setPen(QPen(_q(self.acc, 45 if frac < 1.0 else 120), 1.5 if frac < 1.0 else 2.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), rr, rr)
            self.text(p, cx + 6, cy - rr + 4, label, self.font("mono", 14), DIM)

        # Crosshairs
        p.setPen(QPen(_q(self.acc, 50), 1, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(cx - r - 15, cy), QPointF(cx + r + 15, cy))
        p.drawLine(QPointF(cx, cy - r - 15), QPointF(cx, cy + r + 15))

        # Rotating Radar Sweep Wedge
        p.save()
        p.translate(cx, cy)
        p.rotate(self.sweep_angle)

        grad = QConicalGradient(0, 0, 0)
        grad.setColorAt(0.0, _q(self.acc, 130))
        grad.setColorAt(0.15, _q(self.acc, 20))
        grad.setColorAt(0.16, _q(self.acc, 0))
        grad.setColorAt(1.0, _q(self.acc, 0))

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawPie(QRectF(-r, -r, 2 * r, 2 * r), 0, int(360 * 16))

        # Leading beam line
        p.setPen(QPen(_q((255, 255, 255), 220), 2))
        p.drawLine(QPointF(0, 0), QPointF(r, 0))
        p.restore()

        # Center emitter dot
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_q(self.acc, 240))
        p.drawEllipse(QPointF(cx, cy), 5, 5)

        # Draw Target Blips on Radar
        # Target 1: Gateway / Router
        self._draw_blip(p, cx - 45, cy - 35, "GATEWAY", OK, now)

        # Target 2: DNS Target
        ping_val = max(5.0, min(data.get("ping", 20.0), 200.0))
        ping_dist = (ping_val / 200.0) * (r * 0.9)
        ping_x = cx + ping_dist * 0.707
        ping_y = cy - ping_dist * 0.707
        self._draw_blip(p, ping_x, ping_y, f"DNS ({ping_val:.0f}ms)", WARN if ping_val > 80 else self.acc, now)

        # Target 3+: LAN devices mapped around perimeter
        devices = data.get("devices", [])
        for i, dev in enumerate(devices[:6]):
            ang = (i * 55.0 + 30.0) * (math.pi / 180.0)
            dist = r * (0.45 + (i % 3) * 0.18)
            bx = cx + math.cos(ang) * dist
            by = cy + math.sin(ang) * dist
            ip_str = dev.split()[0] if dev else f"DEV-{i+1}"
            self._draw_blip(p, bx, by, ip_str, HOT if i == 0 else self.acc, now)

        # Radar Scope Legend
        self.text(p, cx, cy + r + 38, "POLAR SCAN 360° · REAL-TIME TELEMETRY", self.font("mono", 16), DIM, align="c")

    def _draw_blip(self, p: QPainter, x: float, y: float, label: str, color, now: float) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_q(color, 255))
        p.drawEllipse(QPointF(x, y), 4, 4)

        # Pulse halo
        pulse = 0.5 + 0.5 * math.sin(now * 4.0 + x * 0.05)
        p.setBrush(_q(color, int(60 * pulse)))
        p.drawEllipse(QPointF(x, y), 10, 10)

        # Text tag
        self.text(p, x + 8, y + 4, label, self.font("mono", 13), TEXT)

    # ── 2. Status Metric Cards ───────────────────────────────────────────────
    def _draw_status_cards(self, p: QPainter, x: float, y: float, w: float, data: dict) -> None:
        card_w = (w - 20) / 2
        card_h = 145.0

        # Card 1: Ping & Latency
        r1 = QRectF(x, y, card_w, card_h)
        self.panel(p, r1, 0.14)

        ping = data.get("ping", 0.0)
        ping_col = OK if ping < 40 else (WARN if ping < 100 else HOT)
        self.text(p, x + 20, y + 32, "INTERNET LATENCY", self.font("display", 16), DIM)
        self.text(p, x + 20, y + 85, f"{ping:.1f}", self.font("display", 54), ping_col, glow=True)
        self.text(p, x + 165, y + 80, "ms", self.font("display", 24), DIM)

        # Draw sparkline of ping history
        hist = data.get("history", [])
        if len(hist) > 2:
            self._draw_sparkline(p, x + card_w - 180, y + 35, 160, 85, hist, ping_col)

        # Card 2: Connection & Wi-Fi
        r2 = QRectF(x + card_w + 20, y, card_w, card_h)
        self.panel(p, r2, 0.14)

        wf = data.get("wifi", {})
        ssid = wf.get("ssid") or "Wired Ethernet"
        sig = wf.get("signal") or "100%"
        self.text(p, x + card_w + 40, y + 32, "LINK & INTERFACE", self.font("display", 16), DIM)
        self.text(p, x + card_w + 40, y + 68, ssid.upper()[:20], self.font("display", 26), TEXT)
        self.text(p, x + card_w + 40, y + 105, f"SIGNAL QUALITY: {sig}  ·  ACTIVE", self.font("mono", 16), OK)

        # Card 3: Addressing Bar
        r3 = QRectF(x, y + card_h + 16, w, 110.0)
        self.panel(p, r3, 0.14)

        self.text(p, x + 24, y + card_h + 45, "LOCAL IP ADDRESS", self.font("mono", 14), DIM)
        self.text(p, x + 24, y + card_h + 82, data.get("local_ip", "127.0.0.1"), self.font("mono", 22), TEXT)

        self.text(p, x + 380, y + card_h + 45, "PUBLIC IP (WAN)", self.font("mono", 14), DIM)
        self.text(p, x + 380, y + card_h + 82, data.get("public_ip", "Detecting..."), self.font("mono", 22), self.acc, glow=True)

    def _draw_sparkline(self, p: QPainter, sx: float, sy: float, sw: float, sh: float, values: list, col) -> None:
        if not values:
            return
        v_min, v_max = min(values), max(values)
        rng = max(10.0, v_max - v_min)
        n = len(values)
        poly = QPolygonF()
        for i, v in enumerate(values):
            vx = sx + (i / max(1, n - 1)) * sw
            vy = sy + sh - ((v - v_min) / rng) * (sh - 10)
            poly.append(QPointF(vx, vy))

        p.setPen(QPen(_q(col, 180), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolyline(poly)

    # ── 3. Bottom Tabs (LAN Devices & Listening Ports) ────────────────────────
    def _draw_bottom_tabs(self, p: QPainter, x: float, y: float, w: float, h: float, data: dict) -> None:
        # Tab Header Buttons
        r_tab1 = QRectF(x, y, 190, 42)
        r_tab2 = QRectF(x + 205, y, 190, 42)

        is_dev = self.tab == "devices"
        over1 = self.hit(r_tab1, "tab:devices")
        over2 = self.hit(r_tab2, "tab:ports")

        self.panel(p, r_tab1, 0.35 if is_dev else (0.18 if over1 else 0.08), active=is_dev)
        self.text(p, x + 24, y + 27, "LAN TARGETS", self.font("display", 16), self.acc if is_dev else DIM)

        self.panel(p, r_tab2, 0.35 if not is_dev else (0.18 if over2 else 0.08), active=not is_dev)
        self.text(p, x + 229, y + 27, "ACTIVE PORTS", self.font("display", 16), self.acc if not is_dev else DIM)

        # Tab Body Container
        body_r = QRectF(x, y + 52, w, h - 52)
        self.panel(p, body_r, 0.12)

        if is_dev:
            # Render LAN Devices List
            devs = data.get("devices", [])
            if not devs:
                self.text(p, x + 30, y + 100, "Scanning local ARP cache for active subnet devices...", self.font("mono", 18), DIM)
            else:
                self.text(p, x + 30, y + 90, f"DETECTED HOSTS ON SUBNET ({len(devs)} TOTAL):", self.font("mono", 14), DIM)
                for idx, dev in enumerate(devs[:8]):
                    dy = y + 125 + idx * 30
                    parts = dev.split()
                    ip_part = parts[0] if parts else dev
                    mac_part = parts[1] if len(parts) > 1 else ""
                    self.text(p, x + 30, dy, f"▸ {ip_part}", self.font("mono", 18), TEXT)
                    if mac_part:
                        self.text(p, x + 240, dy, f"[{mac_part}]", self.font("mono", 16), DIM)
                    self.text(p, x + 580, dy, "ONLINE", self.font("mono", 14), OK)
        else:
            # Render Listening Ports
            ports = data.get("ports", [])
            if not ports:
                self.text(p, x + 30, y + 100, "No active listening ports detected.", self.font("mono", 18), DIM)
            else:
                self.text(p, x + 30, y + 90, f"OPEN LISTENING PORTS ON THIS HOST ({len(ports)}):", self.font("mono", 14), DIM)
                for idx, port in enumerate(ports[:8]):
                    dy = y + 125 + idx * 30
                    self.text(p, x + 30, dy, f"● PORT {port}", self.font("mono", 18), TEXT)
                    self.text(p, x + 580, dy, "LISTEN", self.font("mono", 14), self.acc)


def show_radar(player) -> bool:
    """Open the dedicated Network Radar Board on the HUD. Safe from any thread."""
    return bool(lb._gui(lambda: lb._open(player, "radar", NetworkRadarBoard)))
