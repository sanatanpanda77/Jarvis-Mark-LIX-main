"""
plugins/network_radar.py — Network Radar, Wi-Fi Diagnostics & LAN Sentinel for JARVIS.

Provides real-time network intelligence: Wi-Fi signal quality, latency (ping),
active listening ports, public/local IP details, and LAN device discovery.
"""
from __future__ import annotations

import json
import os
import platform
import re
import socket
import subprocess
import time
import urllib.request
from typing import Optional

_OS = platform.system()

PLUGIN = {
    "name": "network_radar",
    "label": "Network Radar",
    "level": "basic",
    "tier": "core",
    "summary": "Real-time network diagnostics: Wi-Fi signal, ping latency, open ports, and LAN device scanner.",
    "setup": [
        "Zero configuration required.",
        "Say: 'Jarvis, show network radar', 'open network radar', 'check ping', 'scan local network', 'what ports are open?'.",
    ],
    "description": (
        "Comprehensive network diagnostic radar and live network display. "
        "Use this tool WHENEVER the user asks for 'network radar', 'radar', 'open network radar', 'show network radar', "
        "internet speed/latency, ping, Wi-Fi signal strength, open ports, or scanning devices on the local LAN. "
        "Always call this tool for any network radar query — do NOT call live_panels for network radar. "
        "Actions: "
        "'radar' or 'open' (displays the dedicated futuristic animated 360-degree radar panel on the HUD with live ping, Wi-Fi, and LAN devices), "
        "'close' (close the radar panel on the HUD), "
        "'ping' (measure connection latency in milliseconds to game/DNS servers or target), "
        "'ports' (list open listening ports and active network connections on this machine), "
        "'lan_scan' (discover other devices active on the local Wi-Fi network via ARP), "
        "'public_ip' (fast public IP address lookup)."
    ),
    "brief": "Network diagnostics: Wi-Fi signal, ping latency, open ports, and LAN device discovery.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["radar", "open", "close", "ping", "ports", "lan_scan", "public_ip"],
                "description": "Network diagnostic action to perform (default 'radar')."
            },
            "target": {
                "type": "STRING",
                "description": "For 'ping': custom hostname or IP (e.g. '8.8.8.8', 'google.com', '1.1.1.1')."
            }
        },
        "required": ["action"]
    }
}

PLUGIN_SETTINGS = {
    "namespace": "network_radar",
    "title": "📡  NETWORK RADAR",
    "fields": [
        {
            "key": "default_ping_target",
            "label": "Default Ping Target",
            "type": "text",
            "default": "1.1.1.1"
        }
    ]
}


def _get_local_ip() -> str:
    """Find local IPv4 address without external lookup."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _get_public_ip() -> str:
    """Fetch public IPv4 with quick fallback."""
    endpoints = ["https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"]
    for url in endpoints:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                return resp.read().decode("utf-8").strip()
        except Exception:
            continue
    return "Unavailable"


def _get_wifi_info() -> dict:
    """Read Wi-Fi interface details on Windows / Linux / macOS."""
    info = {"ssid": "Unknown", "signal": "N/A", "type": "Ethernet / Other"}
    if _OS == "Windows":
        try:
            res = subprocess.run(
                ["netsh", "wlan", "show", "interfaces"],
                capture_output=True, text=True, timeout=3,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
            )
            out = res.stdout
            ssid_m = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.M)
            sig_m = re.search(r"^\s*Signal\s*:\s*(\d+)%", out, re.M)
            state_m = re.search(r"^\s*State\s*:\s*(.+)$", out, re.M)
            if ssid_m:
                info["ssid"] = ssid_m.group(1).strip()
            if sig_m:
                info["signal"] = f"{sig_m.group(1).strip()}%"
            if state_m:
                info["type"] = f"Wi-Fi ({state_m.group(1).strip()})"
        except Exception:
            pass
    return info


def _measure_socket_ping(host: str, port: int = 53, timeout: float = 2.0) -> Optional[float]:
    """Measure TCP handshake latency in ms (pure Python, non-admin)."""
    try:
        t0 = time.perf_counter()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        sock.close()
        t1 = time.perf_counter()
        return (t1 - t0) * 1000.0
    except Exception:
        return None


def _get_listening_ports(limit: int = 8) -> list[str]:
    """Inspect active listening ports on the machine."""
    ports = []
    try:
        import psutil
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == "LISTEN" and conn.laddr:
                proc_name = ""
                try:
                    if conn.pid:
                        proc_name = psutil.Process(conn.pid).name()
                except Exception:
                    pass
                name_str = f" ({proc_name})" if proc_name else ""
                port_entry = f":{conn.laddr.port}{name_str}"
                if port_entry not in ports:
                    ports.append(port_entry)
            if len(ports) >= limit:
                break
    except Exception:
        # Fallback to netstat on Windows
        try:
            res = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=4,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
            )
            for line in res.stdout.splitlines():
                if "LISTENING" in line:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] not in ports:
                        ports.append(parts[1])
                if len(ports) >= limit:
                    break
        except Exception:
            pass
    return ports


def _scan_arp_devices() -> list[str]:
    """Discover devices on local subnet using ARP cache."""
    devices = []
    try:
        res = subprocess.run(
            ["arp", "-a"],
            capture_output=True, text=True, timeout=4,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        )
        for line in res.stdout.splitlines():
            line = line.strip()
            # Match IPv4 addresses followed by MAC
            m = re.match(r"^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s+([0-9a-fA-F\-]{17})\s+(\w+)", line)
            if m:
                ip, mac, kind = m.group(1), m.group(2), m.group(3)
                if not ip.endswith(".255") and not ip.startswith("224."):
                    devices.append(f"{ip} [{mac}]")
    except Exception:
        pass
    return devices


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "radar").strip().lower()
    target = str(p.get("target") or "").strip()

    try:
        # ── 1. Full Radar / Open Dedicated HUD Panel ─────────────────────────
        if action in ("radar", "open"):
            # Display the dedicated futuristic 360° Network Radar Panel on the HUD
            if player:
                try:
                    from plugins import _network_radar_panel as nr_panel
                    nr_panel.show_radar(player)
                except Exception as e:
                    pass

            local_ip = _get_local_ip()
            wifi = _get_wifi_info()
            
            # Measure quick ping to Cloudflare and Google
            cf_ping = _measure_socket_ping("1.1.1.1", 53)
            goog_ping = _measure_socket_ping("8.8.8.8", 53)
            
            latency_str = ""
            if cf_ping is not None:
                latency_str = f"{cf_ping:.1f} ms"
            elif goog_ping is not None:
                latency_str = f"{goog_ping:.1f} ms"
            else:
                latency_str = "Offline / Timed out"

            pub_ip = _get_public_ip()
            wifi_str = f"Wi-Fi '{wifi['ssid']}' (Signal: {wifi['signal']})" if wifi["ssid"] != "Unknown" else "Wired/Active Connection"

            return (
                f"Network Radar active on your display: {wifi_str}. Local IP is {local_ip}, "
                f"Public IP is {pub_ip}. Ping latency is {latency_str}."
            )

        # ── Close Panel ──────────────────────────────────────────────────────
        if action in ("close", "hide"):
            if player:
                try:
                    from plugins import _live_panels_core as lb
                    lb.close_all()
                except Exception:
                    pass
            return "Closed the Network Radar panel, sir."

        # ── 2. Ping / Latency ────────────────────────────────────────────────
        if action == "ping":
            host = target or "1.1.1.1"
            # Strip protocol if user passed URL
            host = re.sub(r"^https?://", "", host).split("/")[0].split(":")[0]
            
            # Resolve if domain
            try:
                resolved_ip = socket.gethostbyname(host)
            except Exception:
                return f"Could not resolve host '{host}', sir."

            # Test latency across 3 probes
            samples = []
            for _ in range(3):
                ms = _measure_socket_ping(resolved_ip, 80 if host != resolved_ip else 53, timeout=1.5)
                if ms is None:
                    ms = _measure_socket_ping(resolved_ip, 443, timeout=1.5)
                if ms is not None:
                    samples.append(ms)
                time.sleep(0.08)

            if not samples:
                return f"Target '{host}' ({resolved_ip}) did not answer ping probes."
            
            avg = sum(samples) / len(samples)
            return f"Ping to {host}: Average latency is {avg:.1f} ms (min: {min(samples):.1f} ms, max: {max(samples):.1f} ms)."

        # ── 3. Open Ports ────────────────────────────────────────────────────
        if action == "ports":
            ports = _get_listening_ports(limit=10)
            if not ports:
                return "No active listening ports detected or permission was restricted."
            return f"Active listening ports on this machine: {', '.join(ports)}."

        # ── 4. LAN Scan (ARP) ────────────────────────────────────────────────
        if action == "lan_scan":
            devs = _scan_arp_devices()
            if not devs:
                return "LAN scan complete. No other active devices found in the local ARP table."
            return f"LAN Radar: Detected {len(devs)} active device(s) on your local subnet: {', '.join(devs[:8])}."

        # ── 5. Public IP ─────────────────────────────────────────────────────
        if action == "public_ip":
            ip = _get_public_ip()
            return f"Your public IP address is {ip}, sir."

        return f"Unknown Network Radar action '{action}'."

    except Exception as e:
        return f"Network Radar encountered an error: {e}"
