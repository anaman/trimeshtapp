#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
lora-dashboard — LoRa Bridge Control Panel

Single-pane web dashboard for the MeshCore / Meshtastic / Reticulum message
bridge stack running on this host:

  - Login uses the local mosquitto MQTT credentials (verified by an
    authenticated MQTT CONNECT against 127.0.0.1:1883).
  - Status: mesh-gateway, mqtt-bridge, rns-bridge, rns-status-server, rnsd,
    mosquitto + live bridge event feed.
  - Bridge settings: MQTT brokers (mqtt_bridge.json) and RNS bridge
    (rns-bridge/rns_bridge.json) with service restarts.
  - ESP32 LoRa protocol settings: MeshCore (meshcore lib over TCP 5000),
    Meshtastic (meshtastic CLI), Reticulum/RNode (rnodeconf EEPROM).
  - Devices: MeshCore companion nodes on LAN + tailnet (TCP 5000 / 4403).

Runs as an unprivileged user behind a reverse proxy; state-changing calls use
`sudo -n systemctl` against a strict allowlist.
"""

import asyncio
import ipaddress
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import paho.mqtt.client as mqtt

from flask import Flask, jsonify, render_template, request, session

BASE = Path(__file__).resolve().parent
CONFIG_DIR = BASE / "config"
CONFIG_FILE = CONFIG_DIR / "dashboard.json"
AUDIT_FILE = BASE / "audit.log"
ATTEMPT_FILE = CONFIG_DIR / "login_attempts.json"

MESH_RELAY_DIR = Path(os.environ.get("MESH_RELAY_DIR", "/opt/mesh-bridge/mesh-relay"))
RNS_BRIDGE_DIR = Path(os.environ.get("RNS_BRIDGE_DIR", "/opt/mesh-bridge/rns-bridge"))

MQTT_BRIDGE_CFG = MESH_RELAY_DIR / "mqtt_bridge.json"
RNS_BRIDGE_CFG = RNS_BRIDGE_DIR / "rns_bridge.json"
PEER_TRACKER_CFG = RNS_BRIDGE_DIR / "state" / "rns_peer_tracker.json"
PEER_TRACKER_STATUS = Path("/tmp/rns_peer_tracker_status.json")
EVENT_FILE = Path("/tmp/meshgw_events.jsonl")
GATEWAY_STATUS_URL = "http://127.0.0.1:8082/"
RNS_STATUS_URL = "http://127.0.0.1:8451/status.json"
MQTT_BRIDGE_STATE = Path("/tmp/mqtt_bridge_state.json")

MOSQUITTO_HOST, MOSQUITTO_PORT = "127.0.0.1", 1883
MESH_RELAY_VENV = os.environ.get("MESH_RELAY_VENV", str(MESH_RELAY_DIR / ".venv" / "bin"))
RNS_VENV = os.environ.get("RNS_VENV", str(RNS_BRIDGE_DIR / ".venv" / "bin"))

SERVICE_ALLOWLIST = {
    "mesh-gateway", "mqtt-bridge", "rns-bridge",
    "rns-status-server", "rnsd", "mosquitto", "lora-dashboard",
}

LAN_SUBNET = os.environ.get("LAN_SUBNET", "192.168.1.0/24")  # the /24 that hosts your mesh radios
DEVICE_PORTS = {5000: "MeshCore companion", 4403: "Meshtastic API"}

log = logging.getLogger("lora-dashboard")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


# ------------------------------------------------------------------------- helpers

def atomic_write(path: Path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, mode)
    tmp.replace(path)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def dashboard_cfg():
    cfg = load_json(CONFIG_FILE, {})
    if not cfg.get("secret_key"):
        cfg["secret_key"] = secrets.token_hex(32)
        atomic_write(CONFIG_FILE, cfg)
    return cfg


def audit(action, detail=""):
    try:
        with AUDIT_FILE.open("a") as f:
            f.write(json.dumps({
                "ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "user": session.get("user", "?"), "action": action, "detail": detail,
            }) + "\n")
    except Exception as e:
        log.warning("audit write failed: %s", e)


def run(cmd, timeout=30, env=None):
    """Run a command, return (rc, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"
    except Exception as e:
        return -1, "", str(e)


def service_state(name):
    rc, out, _ = run(["systemctl", "is-active", f"{name}.service"], timeout=8)
    return out if rc == 0 else "inactive"


def service_pid_uptime(name):
    rc, out, _ = run(["systemctl", "show", f"{name}.service", "-p", "MainPID", "-p", "ActiveEnterTimestamp"], timeout=8)
    d = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        d[k] = v
    pid = d.get("MainPID", "0")
    return {"pid": pid, "uptime_since": d.get("ActiveEnterTimestamp", "")}


def sudo_systemctl(args, unit):
    if unit not in SERVICE_ALLOWLIST:
        return {"ok": False, "error": "unit not in allowlist"}
    rc, out, err = run(["sudo", "-n", "systemctl", args, f"{unit}.service"], timeout=60)
    return {"ok": rc == 0, "rc": rc, "out": out, "err": err}


# ------------------------------------------------------------------------- MQTT auth

class LoginLimiter:
    MAX_FAIL = 5
    LOCK_SECS = 900

    def __init__(self):
        self._lock = threading.Lock()
        self._data = load_json(ATTEMPT_FILE, {})

    def _save(self):
        atomic_write(ATTEMPT_FILE, self._data)

    def _key(self, user, ip):
        return f"{user}@{ip}"

    def blocked(self, user, ip):
        with self._lock:
            e = self._data.get(self._key(user, ip))
            if not e:
                return False
            if e["count"] >= self.MAX_FAIL and time.time() < e["until"]:
                return e["until"] - time.time()
            if time.time() >= e["until"]:
                self._data.pop(self._key(user, ip), None)
                self._save()
            return False

    def fail(self, user, ip):
        with self._lock:
            k = self._key(user, ip)
            e = self._data.get(k, {"count": 0, "until": 0})
            e["count"] += 1
            if e["count"] >= self.MAX_FAIL:
                e["until"] = time.time() + self.LOCK_SECS
            self._data[k] = e
            self._save()

    def ok(self, user, ip):
        with self._lock:
            if self._data.pop(self._key(user, ip), None) is not None:
                self._save()


limiter = LoginLimiter()


def verify_mqtt_credentials(user, password):
    """Authenticate against the local mosquitto broker via MQTT CONNECT.

    paho's connect() returns 0 even when the broker rejects credentials
    (CONNACK rc != 0), so the authoritative result is captured from the
    on_connect callback.
    """
    result = {"rc": None}
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"lora-dash-{secrets.token_hex(4)}",
        protocol=mqtt.MQTTv311,
    )

    def _on_connect(c, ud, flags, reason_code, properties=None):
        try:
            if hasattr(reason_code, "is_failure"):
                result["rc"] = 1 if reason_code.is_failure else 0
            else:
                result["rc"] = 0 if str(reason_code) == "Success" else 1
        except Exception:
            result["rc"] = -1

    client.on_connect = _on_connect
    client.username_pw_set(user, password)
    try:
        client.connect(MOSQUITTO_HOST, MOSQUITTO_PORT, keepalive=10)
        client.loop_start()
        deadline = time.time() + 6
        while result["rc"] is None and time.time() < deadline:
            time.sleep(0.05)
        client.loop_stop()
        return result["rc"] == 0
    except Exception as e:
        log.info("mqtt auth probe failed for %r: %s", user, e)
        return False
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


# ------------------------------------------------------------------------- status

def tail_events(limit=100):
    events = []
    try:
        with EVENT_FILE.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass
    total = len(events)
    nets = {}
    for e in events:
        n = e.get("net", "?")
        nets[n] = nets.get(n, 0) + 1
    return {"total": total, "by_net": nets, "events": events[-limit:]}


def rns_status():
    try:
        import urllib.request
        with urllib.request.urlopen(RNS_STATUS_URL, timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}


def rns_bridge_status():
    return load_json(Path("/tmp/rns_bridge_status.json"), {})


def gateway_status():
    try:
        import urllib.request
        with urllib.request.urlopen(GATEWAY_STATUS_URL, timeout=4) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}


def mqtt_bridge_state():
    d = load_json(MQTT_BRIDGE_STATE, {})
    pub = d.get("published", [])
    return {
        "offset": d.get("offset", 0),
        "last_published": pub[-3:] if pub else [],
    }


def serial_units():
    units = []
    for dev, proto, note in [
        ("/dev/ttyUSB0", "Reticulum (RNode)", "RNS LoRa leg — KISS interface for rnsd"),
        ("/dev/ttyACM0", "MeshCore", "MeshCore node serial leg (owned by the mesh gateway)"),
        ("/dev/ttyACM1", "Meshtastic", "Meshtastic serial leg (owned by the mesh gateway)"),
    ]:
        units.append({
            "dev": dev, "protocol": proto, "note": note,
            "present": os.path.exists(dev),
        })
    return units


def gather_status():
    svcs = {}
    for name in ["mosquitto", "mesh-gateway", "mqtt-bridge", "rns-bridge",
                 "rns-status-server", "rnsd", "lora-dashboard"]:
        svcs[name] = {"state": service_state(name)}
        svcs[name].update(service_pid_uptime(name))
    return {
        "services": svcs,
        "gateway": gateway_status(),
        "rns": rns_status(),
        "rns_leg": rns_bridge_status(),
        "mqtt_bridge": mqtt_bridge_state(),
        "events": tail_events(60),
        "serial": serial_units(),
        "host": socket.gethostname(),
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


# ------------------------------------------------------------------------- bridge configs

def read_mqtt_bridge():
    cfg = load_json(MQTT_BRIDGE_CFG, {"brokers": []})
    brokers = []
    for b in cfg.get("brokers", []):
        b = dict(b)
        if b.get("password"):
            b["password"] = "********"
        brokers.append(b)
    return {"brokers": brokers, "path": str(MQTT_BRIDGE_CFG)}


def write_mqtt_bridge(payload):
    brokers = payload.get("brokers", [])
    cleaned = []
    for b in brokers:
        nb = {k: b.get(k) for k in
              ("name", "host", "port", "username", "password", "tls",
               "mode", "status", "publish_topic", "subscribe_topic", "status_topic")}
        if nb.get("password") == "********":
            # preserve existing secret
            cur = load_json(MQTT_BRIDGE_CFG, {"brokers": []}).get("brokers", [])
            for cb in cur:
                if cb.get("name") == nb.get("name"):
                    nb["password"] = cb.get("password", "")
                    break
            else:
                nb["password"] = ""
        nb["port"] = int(nb.get("port") or 1883)
        nb["tls"] = bool(nb.get("tls"))
        nb["status"] = bool(nb.get("status"))
        cleaned.append(nb)
    atomic_write(MQTT_BRIDGE_CFG, {"brokers": cleaned}, mode=0o644)
    return {"brokers": cleaned}


def read_rns_bridge():
    cfg = load_json(RNS_BRIDGE_CFG, {})
    identity = Path(os.path.expanduser("~/.reticulum/meshbridge_identity"))
    return {
        "settings": {
            "announce_app": cfg.get("announce_app", "meshbridge"),
            "announce_name": cfg.get("announce_name", "bridge"),
            "reannounce_interval_s": int(cfg.get("reannounce_interval_s", 60)),
            "announce_enabled": bool(cfg.get("announce_enabled", True)),
            "lxmf_enabled": bool(cfg.get("lxmf_enabled", True)),
        },
        "identity_file": str(identity),
        "identity_exists": identity.exists(),
        "path": str(RNS_BRIDGE_CFG),
    }


def read_peer_tracker():
    cfg = load_json(PEER_TRACKER_CFG, {})
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "period_value": int(cfg.get("period_value", 1) or 1),
        "period_unit": str(cfg.get("period_unit", "days")),
        "path": str(PEER_TRACKER_CFG),
    }


def write_peer_tracker(payload):
    s = payload.get("settings", payload) if isinstance(payload, dict) else {}
    unit = str(s.get("period_unit", "days"))
    if unit not in ("seconds", "minutes", "hours", "days"):
        unit = "days"
    out = {
        "enabled": bool(s.get("enabled", True)),
        "period_value": max(1, min(int(s.get("period_value", 1) or 1), 3650)),
        "period_unit": unit,
    }
    atomic_write(PEER_TRACKER_CFG, out, mode=0o644)
    return out


def read_peer_tracker_status():
    return load_json(PEER_TRACKER_STATUS, {})


def write_rns_bridge(payload):
    s = payload.get("settings", {})
    out = {
        "announce_app": s.get("announce_app", "meshbridge")[:32],
        "announce_name": s.get("announce_name", "bridge")[:32],
        "reannounce_interval_s": max(5, min(int(s.get("reannounce_interval_s", 60)), 86400)),
        "announce_enabled": bool(s.get("announce_enabled", True)),
        "lxmf_enabled": bool(s.get("lxmf_enabled", True)),
    }
    atomic_write(RNS_BRIDGE_CFG, out, mode=0o644)
    return out


# ------------------------------------------------------------------------- protocols: MeshCore (meshcore lib, TCP)

def meshcore_read(host, port, timeout=15):
    async def _read():
        from meshcore import MeshCore
        mc = await asyncio.wait_for(
            MeshCore.create_tcp(host, port, only_error=True, default_timeout=8), timeout=timeout)
        if mc is None:
            return {"error": "no response (device refused or not a MeshCore companion)"}
        try:
            out = {}
            cmds = mc.commands
            for key, meth in [
                ("self_telemetry", "get_self_telemetry"),
                ("tuning", "get_tuning"),
                ("stats_radio", "get_stats_radio"),
                ("stats_core", "get_stats_core"),
                ("stats_packets", "get_stats_packets"),
                ("path_hash_mode", "get_path_hash_mode"),
                ("default_flood_scope", "get_default_flood_scope"),
            ]:
                fn = getattr(cmds, meth, None)
                if fn is None:
                    continue
                try:
                    out[key] = await asyncio.wait_for(fn(), timeout=8)
                except Exception as e:
                    out[key] = {"error": str(e)[:120]}
            return out
        finally:
            try:
                await mc.disconnect()
            except Exception:
                pass
    try:
        return asyncio.run(_read())
    except asyncio.TimeoutError:
        return {"error": "connection timed out"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def meshcore_apply(host, port, fields, timeout=25):
    """Apply radio/tuning fields. Supported keys map to library commands."""
    async def _apply():
        from meshcore import MeshCore
        mc = await asyncio.wait_for(
            MeshCore.create_tcp(host, port, only_error=True, default_timeout=8), timeout=timeout)
        if mc is None:
            return {"error": "no response"}
        results = {}
        try:
            cmds = mc.commands
            if "tx_power" in fields:
                fn = getattr(cmds, "set_tx_power", None)
                if fn:
                    results["tx_power"] = await asyncio.wait_for(fn(int(fields["tx_power"])), timeout=8)
                else:
                    results["tx_power"] = {"error": "set_tx_power not in lib"}
            if "name" in fields:
                fn = getattr(cmds, "set_name", None)
                if fn:
                    results["name"] = await asyncio.wait_for(fn(str(fields["name"])), timeout=8)
                else:
                    results["name"] = {"error": "set_name not in lib"}
            if any(k in fields for k in ("frequency", "bandwidth", "spread_factor", "coding_rate")):
                fn = getattr(cmds, "set_tuning", None)
                if fn:
                    cur = await asyncio.wait_for(cmds.get_tuning(), timeout=8) or {}
                    cur = cur if isinstance(cur, dict) else {}
                    for k in ("frequency", "bandwidth", "spread_factor", "coding_rate"):
                        if k in fields and fields[k] not in (None, ""):
                            cur[k] = fields[k]
                    results["tuning"] = await asyncio.wait_for(fn(**cur), timeout=8)
                else:
                    results["tuning"] = {"error": "set_tuning not in lib"}
            return results
        finally:
            try:
                await mc.disconnect()
            except Exception:
                pass
    try:
        return asyncio.run(_apply())
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def meshcore_action(host, port, action):
    async def _act():
        from meshcore import MeshCore
        mc = await asyncio.wait_for(
            MeshCore.create_tcp(host, port, only_error=True, default_timeout=8), timeout=20)
        if mc is None:
            return {"error": "no response"}
        try:
            if action == "advert":
                await asyncio.wait_for(mc.commands.send_advert(), timeout=10)
                return {"ok": True, "action": "advert"}
            if action == "reboot":
                await asyncio.wait_for(mc.commands.reboot(), timeout=10)
                return {"ok": True, "action": "reboot"}
            return {"error": f"unknown action {action}"}
        finally:
            try:
                await mc.disconnect()
            except Exception:
                pass
    try:
        return asyncio.run(_act())
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ------------------------------------------------------------------------- protocols: Meshtastic (CLI)

def _meshtastic_base(hostport):
    return [f"{MESH_RELAY_VENV}/meshtastic", "--host", hostport]


def meshtastic_read(hostport):
    rc, out, err = run(_meshtastic_base(hostport) + ["--export-config", "yaml"], timeout=40)
    if rc != 0:
        return {"error": err or out or f"rc={rc}"}
    cfg = {}
    try:
        import yaml  # meshtastic CLI venv has pyyaml
        cfg = yaml.safe_load(out) or {}
    except Exception:
        cfg = {"raw": out[:4000]}
    lora = cfg.get("lora", {}) if isinstance(cfg, dict) else {}
    mqttc = cfg.get("mqtt", {}) if isinstance(cfg, dict) else {}
    return {
        "lora": {
            "region": lora.get("region"),
            "modem_preset": lora.get("modem_preset"),
            "frequency": lora.get("frequency"),
            "bandwidth": lora.get("bandwidth"),
            "spread_factor": lora.get("spread_factor"),
            "coding_rate": lora.get("coding_rate"),
            "tx_power": lora.get("tx_power"),
        },
        "mqtt": {
            "enabled": mqttc.get("enabled"),
            "address": mqttc.get("address"),
            "username": mqttc.get("username"),
            "root": mqttc.get("root"),
        },
        "owner": (cfg.get("owner", {}) or {}).get("long_name") if isinstance(cfg, dict) else None,
    }


def meshtastic_apply(hostport, fields):
    LORA_KEYS = {
        "frequency": "lora.frequency", "bandwidth": "lora.bandwidth",
        "spread_factor": "lora.spread_factor", "coding_rate": "lora.coding_rate",
        "tx_power": "lora.tx_power", "region": "lora.region",
        "modem_preset": "lora.modem_preset",
    }
    MQTT_KEYS = {
        "mqtt_enabled": "mqtt.enabled", "mqtt_address": "mqtt.address",
        "mqtt_username": "mqtt.username", "mqtt_root": "mqtt.root",
    }
    args = _meshtastic_base(hostport) + ["--begin-edit"]
    for k, v in fields.items():
        if v in (None, ""):
            continue
        key = LORA_KEYS.get(k) or MQTT_KEYS.get(k)
        if key:
            args += ["--set", key, str(v)]
    if len(args) == 3:
        return {"error": "no editable fields supplied"}
    args += ["--commit-edit"]
    rc, out, err = run(args, timeout=60)
    return {"ok": rc == 0, "rc": rc, "out": out[-2000:], "err": err[-2000:]}


def meshtastic_action(hostport, action):
    if action == "reboot":
        rc, out, err = run(_meshtastic_base(hostport) + ["--reboot"], timeout=30)
        return {"ok": rc == 0, "rc": rc, "err": err}
    return {"error": f"unknown action {action}"}


# ------------------------------------------------------------------------- protocols: Reticulum / RNode (rnodeconf)

def rnode_read(port="/dev/ttyUSB0"):
    rc, out, err = run([f"{RNS_VENV}/rnodeconf", "-i", port], timeout=30)
    if rc != 0:
        return {"error": err or out or f"rc={rc}", "port": port}
    parsed = {"port": port, "raw": out[:1200]}
    import re
    for key, pat in [
        ("frequency", r"(?:freq|frequency)[^\d]*([\d.]+)"),
        ("bandwidth", r"(?:bw|bandwidth)[^\d]*([\d.]+)"),
        ("spread_factor", r"(?:sf|spread)[^\d]*([\d.]+)"),
        ("coding_rate", r"(?:cr|coding)[^\d]*([\d.]+)"),
        ("tx_power", r"(?:txp|tx_power)[^\d]*([\d.]+)"),
    ]:
        m = re.search(pat, out, re.IGNORECASE)
        if m:
            parsed[key] = m.group(1)
    return parsed


def rnode_apply(port, fields, restart_rnsd=True):
    """Stop rnsd (owns the serial port), write EEPROM, start rnsd again."""
    steps = []
    if restart_rnsd:
        r = sudo_systemctl("stop", "rnsd")
        steps.append(("stop rnsd", r))
        if not r["ok"]:
            return {"ok": False, "steps": steps}
        time.sleep(1.5)
    args = [f"{RNS_VENV}/rnodeconf"]
    for flag, key in [("--freq", "frequency"), ("--bw", "bandwidth"),
                      ("--sf", "spread_factor"), ("--cr", "coding_rate"),
                      ("--txp", "tx_power")]:
        if key in fields and fields[key] not in (None, ""):
            args += [flag, str(fields[key])]
    args.append(port)
    rc, out, err = run(args, timeout=60)
    steps.append(("rnodeconf write", {"rc": rc, "out": out[-800:], "err": err[-800:]}))
    if restart_rnsd:
        time.sleep(1.0)
        r = sudo_systemctl("start", "rnsd")
        steps.append(("start rnsd", r))
    return {"ok": rc == 0, "steps": steps}


# ------------------------------------------------------------------------- devices

SCAN_STATE_FILE = CONFIG_DIR / "devices_state.json"
DEFAULT_SCAN = {
    "lan": True,
    "tailnet": True,
    "advertised": True,
    "custom_ranges": "",
    "ports": [5000, 4403],
    "timeout_ms": 400,
    "auto_interval_min": 0,
}
SCAN_STATE = {"devices": {}, "last_auto_ts": 0}
SCAN_LOCK = threading.Lock()


def save_scan_state():
    try:
        atomic_write(SCAN_STATE_FILE, SCAN_STATE)
    except Exception as e:
        log.warning("scan state save failed: %s", e)


def load_scan_state():
    global SCAN_STATE
    SCAN_STATE = load_json(SCAN_STATE_FILE, {"devices": {}, "last_auto_ts": 0})
    SCAN_STATE.setdefault("devices", {})
    SCAN_STATE.setdefault("last_auto_ts", 0)


load_scan_state()


def get_scan_config():
    cfg = dashboard_cfg()
    sc = dict(DEFAULT_SCAN)
    sc.update(cfg.get("scan", {}) or {})
    return sc


def save_scan_config(data):
    cfg = dashboard_cfg()
    sc = get_scan_config()
    for k in DEFAULT_SCAN:
        if k in data:
            sc[k] = data[k]
    sc["ports"] = [int(p) for p in sc.get("ports", []) if str(p).isdigit()] or [5000, 4403]
    sc["timeout_ms"] = max(100, min(int(sc.get("timeout_ms", 400)), 5000))
    sc["auto_interval_min"] = max(0, min(int(sc.get("auto_interval_min", 0)), 1440))
    cfg["scan"] = sc
    atomic_write(CONFIG_FILE, cfg)
    return sc


def tailnet_targets():
    """Return (target_ips, ip->name map, advertised_subnet_ips)."""
    names, adv_ips = {}, []
    rc, out, _ = run(["tailscale", "status", "--json"], timeout=15)
    if rc != 0:
        return [], names, adv_ips
    try:
        ts = json.loads(out)
        for node in (ts.get("Peer") or {}).values():
            for ip in node.get("TailscaleIPs", []):
                if ":" not in ip:
                    names[ip] = (node.get("DNSName", "") or node.get("HostName", "") or "").rstrip(".")
            for route in node.get("AdvertisedRoutes", []) or []:
                if ":" in route:
                    continue
                try:
                    net = ipaddress.ip_network(route, strict=False)
                    if net.prefixlen >= 24 and net.num_addresses <= 4096:
                        adv_ips.extend(str(h) for h in net.hosts())
                except Exception:
                    continue
    except Exception:
        pass
    return list(names), names, adv_ips


def expand_ranges(ranges_text):
    """Expand user-supplied targets: CIDRs, single IPs, hostnames.
    Returns (ips, errors). Hostnames resolved via gethostbyname."""
    ips, errors = [], []
    for raw in (ranges_text or "").replace(";", ",").split(","):
        tok = raw.strip()
        if not tok:
            continue
        try:
            if "/" in tok:
                net = ipaddress.ip_network(tok, strict=False)
                if net.num_addresses > 4096:
                    errors.append(f"{tok}: range too large (max 4096 hosts)")
                    continue
                ips.extend(str(h) for h in net.hosts() if h.version == 4)
            else:
                ip = socket.gethostbyname(tok)
                if ":" not in ip:
                    ips.append(ip)
        except Exception as e:
            errors.append(f"{tok}: {e}")
    return ips, errors


def _probe(ip, port, results, timeout):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout / 1000.0)
    try:
        if s.connect_ex((ip, port)) == 0:
            results.append((ip, port))
    except Exception:
        pass
    finally:
        s.close()


def _resolve_name(ip, names):
    if ip in names:
        return names[ip]
    with ThreadPoolExecutor(max_workers=1) as ex:
        try:
            fut = ex.submit(socket.gethostbyaddr, ip)
            return fut.result(timeout=0.6)[0]
        except Exception:
            return ""


def scan_devices(overrides=None):
    """Full scan: LAN /24, tailnet peers, advertised subnets, custom ranges.
    Returns result dict; updates the persistent device registry."""
    sc = get_scan_config()
    if overrides:
        for k in DEFAULT_SCAN:
            if k in overrides:
                sc[k] = overrides[k]
    ports = [int(p) for p in sc.get("ports", [5000, 4403])]
    timeout = int(sc.get("timeout_ms", 400))
    targets = []
    names = {}
    source_count = {}
    if sc.get("lan"):
        base = LAN_SUBNET.rsplit(".", 1)[0]
        lan_ips = [f"{base}.{i}" for i in range(1, 255)]
        targets += lan_ips
        source_count["lan"] = len(lan_ips)
    if sc.get("tailnet"):
        tn_ips, tn_names, adv = tailnet_targets()
        targets += tn_ips
        names.update(tn_names)
        source_count["tailnet"] = len(tn_ips)
        if sc.get("advertised") and adv:
            targets += adv
            source_count["advertised"] = len(adv)
    custom_ips, custom_errors = expand_ranges(sc.get("custom_ranges", ""))
    targets += custom_ips
    source_count["custom"] = len(custom_ips)
    # saved devices always included as targets
    for dev in dashboard_cfg().get("devices", []):
        h = str(dev.get("host", "")).strip()
        if h:
            try:
                targets.append(socket.gethostbyname(h))
            except Exception:
                pass
    targets = sorted({t for t in targets if t})
    if not targets:
        return {"scanned": 0, "found": [], "sources": source_count, "errors": custom_errors, "duration_s": 0}
    t0 = time.time()
    found = []
    with ThreadPoolExecutor(max_workers=128) as ex:
        for ip in targets:
            for port in ports:
                ex.submit(_probe, ip, port, found, timeout)
    found.sort()
    # registry update
    reg = SCAN_STATE["devices"]
    now = time.time()
    for ip, port in found:
        e = reg.setdefault(ip, {"first_seen": now, "last_seen": 0, "ports": [], "name": ""})
        if port not in e["ports"]:
            e["ports"].append(port)
            e["ports"].sort()
        e["last_seen"] = now
        if not e.get("name"):
            e["name"] = _resolve_name(ip, names)
    with SCAN_LOCK:
        SCAN_STATE["last_auto_ts"] = now
        save_scan_state()
    out = []
    for ip, port in found:
        e = reg[ip]
        out.append({
            "ip": ip, "port": port,
            "service": DEVICE_PORTS.get(port, f"port {port}"),
            "name": e.get("name", ""),
            "first_seen": e.get("first_seen"),
            "last_seen": e.get("last_seen"),
        })
    return {
        "scanned": len(targets),
        "found": out,
        "sources": source_count,
        "errors": custom_errors,
        "duration_s": round(time.time() - t0, 1),
    }


def discovered_devices():
    """Registry entries enriched with alive/new flags."""
    reg = SCAN_STATE.get("devices", {})
    now = time.time()
    out = []
    for ip, e in sorted(reg.items(), key=lambda kv: kv[1].get("last_seen", 0), reverse=True):
        last = e.get("last_seen", 0)
        out.append({
            "ip": ip,
            "name": e.get("name", ""),
            "ports": e.get("ports", []),
            "services": [DEVICE_PORTS.get(p, f"port {p}") for p in e.get("ports", [])],
            "first_seen": e.get("first_seen"),
            "last_seen": last,
            "alive": (now - last) < 600,
            "is_new": (now - e.get("first_seen", 0)) < 600,
        })
    return out


def _auto_scan_loop():
    while True:
        time.sleep(20)
        try:
            sc = get_scan_config()
            interval = int(sc.get("auto_interval_min", 0) or 0)
            if interval <= 0:
                continue
            last = SCAN_STATE.get("last_auto_ts", 0)
            if time.time() - last < interval * 60:
                continue
            log.info("auto device scan (interval %s min)", interval)
            scan_devices()
        except Exception as e:
            log.warning("auto scan failed: %s", e)


def get_devices():
    cfg = dashboard_cfg()
    return cfg.get("devices", [])


def save_devices(devices):
    cfg = dashboard_cfg()
    cfg["devices"] = devices
    atomic_write(CONFIG_FILE, cfg)
    return cfg["devices"]


# ------------------------------------------------------------------------- flask app

def create_app():
    app = Flask(__name__)
    cfg = dashboard_cfg()
    app.secret_key = cfg["secret_key"]

    @app.before_request
    def csrf_guard():
        if request.method == "POST" and request.path != "/api/login":
            token = request.headers.get("X-CSRF-Token", "")
            if not token or token != session.get("csrf"):
                return jsonify({"error": "bad or missing CSRF token"}), 403

    @app.context_processor
    def inject_csrf():
        return {"csrf_token": session.get("csrf", "")}

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/session")
    def api_session():
        return jsonify({
            "authed": bool(session.get("user")),
            "user": session.get("user"),
            "csrf": session.get("csrf", ""),
        })

    @app.route("/api/login", methods=["POST"])
    def api_login():
        data = request.get_json(force=True, silent=True) or {}
        user = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        ip = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()
        if not user or not password:
            return jsonify({"error": "username and password required"}), 400
        wait = limiter.blocked(user, ip)
        if wait:
            return jsonify({"error": f"too many attempts; try again in {int(wait // 60)} min"}), 429
        if not verify_mqtt_credentials(user, password):
            limiter.fail(user, ip)
            audit("login-failed", f"user={user} ip={ip}")
            return jsonify({"error": "invalid MQTT credentials"}), 401
        limiter.ok(user, ip)
        session.clear()
        session["user"] = user
        session["csrf"] = secrets.token_hex(16)
        session.permanent = True
        audit("login-ok", f"user={user} ip={ip}")
        return jsonify({"ok": True, "user": user, "csrf": session.get("csrf", "")})

    @app.route("/api/logout", methods=["POST"])
    def api_logout():
        audit("logout")
        session.clear()
        return jsonify({"ok": True})

    @app.route("/api/status")
    def api_status():
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        return jsonify(gather_status())

    @app.route("/api/events")
    def api_events():
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        limit = min(int(request.args.get("limit", 60)), 300)
        return jsonify(tail_events(limit))

    # ---- bridge settings

    @app.route("/api/bridge/mqtt", methods=["GET", "POST"])
    def api_bridge_mqtt():
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        if request.method == "GET":
            return jsonify(read_mqtt_bridge())
        data = request.get_json(force=True, silent=True) or {}
        out = write_mqtt_bridge(data)
        audit("mqtt-bridge-save", f"{len(out['brokers'])} brokers")
        res = sudo_systemctl("restart", "mqtt-bridge")
        audit("mqtt-bridge-restart", f"ok={res['ok']}")
        return jsonify({"ok": res["ok"], "restart": res, "brokers": out["brokers"]})

    @app.route("/api/bridge/rns", methods=["GET", "POST"])
    def api_bridge_rns():
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        if request.method == "GET":
            return jsonify(read_rns_bridge())
        data = request.get_json(force=True, silent=True) or {}
        out = write_rns_bridge(data)
        audit("rns-bridge-save", json.dumps(out))
        res = sudo_systemctl("restart", "rns-bridge")
        audit("rns-bridge-restart", f"ok={res['ok']}")
        return jsonify({"ok": res["ok"], "restart": res, "settings": out})

    @app.route("/api/peers/tracker", methods=["GET", "POST"])
    def api_peers_tracker():
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        if request.method == "GET":
            return jsonify({"config": read_peer_tracker(), "status": read_peer_tracker_status()})
        data = request.get_json(force=True, silent=True) or {}
        out = write_peer_tracker(data)
        audit("peers-tracker-save", json.dumps(out))
        return jsonify({"ok": True, "config": out})

    @app.route("/api/services/restart", methods=["POST"])
    def api_service_restart():
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        unit = str(request.get_json(force=True, silent=True) or {}).get("unit", "")
        if unit not in SERVICE_ALLOWLIST:
            return jsonify({"error": "unit not in allowlist"}), 400
        res = sudo_systemctl("restart", unit)
        audit("service-restart", f"unit={unit} ok={res['ok']}")
        return jsonify(res)

    # ---- protocols

    @app.route("/api/protocols/meshcore/read", methods=["POST"])
    def api_mc_read():
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        d = request.get_json(force=True, silent=True) or {}
        host, port = str(d.get("host", "")).strip(), int(d.get("port", 5000) or 5000)
        audit("meshcore-read", f"{host}:{port}")
        return jsonify(meshcore_read(host, port))

    @app.route("/api/protocols/meshcore/apply", methods=["POST"])
    def api_mc_apply():
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        d = request.get_json(force=True, silent=True) or {}
        host, port = str(d.get("host", "")).strip(), int(d.get("port", 5000) or 5000)
        fields = {k: v for k, v in (d.get("fields") or {}).items() if v not in (None, "")}
        audit("meshcore-apply", f"{host}:{port} fields={sorted(fields)}")
        return jsonify(meshcore_apply(host, port, fields))

    @app.route("/api/protocols/meshcore/action", methods=["POST"])
    def api_mc_action():
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        d = request.get_json(force=True, silent=True) or {}
        host, port = str(d.get("host", "")).strip(), int(d.get("port", 5000) or 5000)
        action = str(d.get("action", ""))
        audit("meshcore-action", f"{host}:{port} {action}")
        return jsonify(meshcore_action(host, port, action))

    @app.route("/api/protocols/meshtastic/read", methods=["POST"])
    def api_mt_read():
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        d = request.get_json(force=True, silent=True) or {}
        hostport = str(d.get("host", "")).strip()
        if ":" not in hostport:
            hostport = f"{hostport}:4403"
        audit("meshtastic-read", hostport)
        return jsonify(meshtastic_read(hostport))

    @app.route("/api/protocols/meshtastic/apply", methods=["POST"])
    def api_mt_apply():
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        d = request.get_json(force=True, silent=True) or {}
        hostport = str(d.get("host", "")).strip()
        if ":" not in hostport:
            hostport = f"{hostport}:4403"
        fields = {k: v for k, v in (d.get("fields") or {}).items() if v not in (None, "")}
        audit("meshtastic-apply", f"{hostport} fields={sorted(fields)}")
        return jsonify(meshtastic_apply(hostport, fields))

    @app.route("/api/protocols/meshtastic/action", methods=["POST"])
    def api_mt_action():
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        d = request.get_json(force=True, silent=True) or {}
        hostport = str(d.get("host", "")).strip()
        if ":" not in hostport:
            hostport = f"{hostport}:4403"
        audit("meshtastic-action", f"{hostport} {d.get('action')}")
        return jsonify(meshtastic_action(hostport, str(d.get("action", ""))))

    @app.route("/api/protocols/rnode/read", methods=["POST"])
    def api_rn_read():
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        d = request.get_json(force=True, silent=True) or {}
        port = str(d.get("port", "/dev/ttyUSB0"))
        audit("rnode-read", port)
        return jsonify(rnode_read(port))

    @app.route("/api/protocols/rnode/apply", methods=["POST"])
    def api_rn_apply():
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        d = request.get_json(force=True, silent=True) or {}
        port = str(d.get("port", "/dev/ttyUSB0"))
        fields = {k: v for k, v in (d.get("fields") or {}).items() if v not in (None, "")}
        restart = bool(d.get("restart_rnsd", True))
        audit("rnode-apply", f"{port} fields={sorted(fields)} restart_rnsd={restart}")
        return jsonify(rnode_apply(port, fields, restart_rnsd=restart))

    # ---- devices

    @app.route("/api/devices")
    def api_devices():
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"devices": get_devices(), "discovered": discovered_devices()})

    @app.route("/api/scan/config", methods=["GET", "POST"])
    def api_scan_config():
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        if request.method == "GET":
            return jsonify(get_scan_config())
        data = request.get_json(force=True, silent=True) or {}
        sc = save_scan_config(data)
        audit("scan-config-save", json.dumps(sc))
        return jsonify(sc)

    @app.route("/api/devices/scan", methods=["POST"])
    def api_devices_scan():
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        data = request.get_json(force=True, silent=True) or {}
        overrides = {k: v for k, v in data.items() if k in DEFAULT_SCAN}
        audit("devices-scan", json.dumps(overrides or "default"))
        return jsonify(scan_devices(overrides or None))

    @app.route("/api/devices/save", methods=["POST"])
    def api_devices_save():
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        devices = (request.get_json(force=True, silent=True) or {}).get("devices", [])
        save_devices(devices)
        audit("devices-save", f"{len(devices)} devices")
        return jsonify({"ok": True, "devices": devices})

    @app.route("/api/devices/forget", methods=["POST"])
    def api_devices_forget():
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        ip = str((request.get_json(force=True, silent=True) or {}).get("ip", "")).strip()
        if ip in SCAN_STATE.get("devices", {}):
            del SCAN_STATE["devices"][ip]
            with SCAN_LOCK:
                save_scan_state()
            audit("devices-forget", ip)
        return jsonify({"ok": True})

    @app.route("/api/audit")
    def api_audit():
        if not session.get("user"):
            return jsonify({"error": "unauthorized"}), 401
        limit = min(int(request.args.get("limit", 50)), 200)
        lines = []
        try:
            with AUDIT_FILE.open() as f:
                lines = [json.loads(l) for l in f if l.strip()][-limit:]
        except FileNotFoundError:
            pass
        return jsonify({"entries": lines})

    # auto-scan daemon (guarded)
    if not getattr(app, "_auto_scan_started", False):
        app._auto_scan_started = True
        threading.Thread(target=_auto_scan_loop, daemon=True).start()

    return app


if __name__ == "__main__":
    from waitress import serve
    port = int(os.environ.get("LORA_DASH_PORT", "8452"))
    log.info("lora-dashboard listening on 127.0.0.1:%s", port)
    serve(create_app(), host="127.0.0.1", port=port, threads=8)
