#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
mqtt_bridge.py — multi-broker MQTT client for the MeshCore node relay
(MeshCore ESP32 companion radio, bridged by the mesh gateway).

Topology (2026-08-16):
  - Public brokers (mqtt.chimesh.org, mqtt.meshtastic.org): INCOMING
    messages only (subscribe; never publish our traffic there).
  - Local MQTT broker (mosquitto on this host): receives STATUS data
    (nodes, location, sensors) + our messages. Publish-only for status;
    both ways for messages (local clients may inject too).

What it does
------------
1. INBOUND (radio -> MQTT): tails /tmp/meshgw_events.jsonl (new MeshCore
   messages emitted by mesh_gateway.py) and publishes each to brokers with
   mode "out"/"both" as JSON: {"net","sender","text","ts"}.
2. STATUS (gateway -> local MQTT): tails /tmp/meshgw_status.jsonl (node
   snapshots: self info, contacts, battery, neighbours, telemetry emitted by
   the gateway) and publishes each to brokers with "status": true, on the
   broker's status_topic (default "meshcore/status").
3. OUTBOUND (MQTT -> radio): subscribes (brokers with mode "in"/"both") and
   writes received messages to /tmp/meshgw_send.txt as {"net":"mc","text":...}
   so the gateway transmits them on the MeshCore radio.
4. Loop prevention: published/received/injected dedup (see state file).

Config
------
JSON at /opt/mesh-bridge/mesh-relay/mqtt_bridge.json:
{
  "brokers": [
    {"name":"chime","host":"mqtt.chimesh.org","port":1883,
     "username":"meshdev","password":"***","tls":false,
     "mode":"in",                       // in | out | both
     "status":false,                    // receive status publications?
     "publish_topic":"meshcore/armaros",
     "subscribe_topic":"meshcore/armaros",
     "status_topic":"meshcore/status"}
  ]
}

State: /tmp/mqtt_bridge_state.json (event tail offset + dedup).
"""
import json
import os
import sys
import threading
import time

import paho.mqtt.client as mqtt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "mqtt_bridge.json")
EVENT_FILE = "/tmp/meshgw_events.jsonl"
STATUS_FILE = "/tmp/meshgw_status.jsonl"
SEND_FILE = "/tmp/meshgw_send.txt"
STATE_FILE = "/tmp/mqtt_bridge_state.json"

HOLD_S = 3600          # dedup hold for published/received keys (matches gateway)
INJECT_S = 300         # radio-echo suppression window for our own transmissions
MAX_TRACKED = 500
MAX_FORWARD_PER_RUN = 50

DEFAULT_BROKERS = [
    {
        "name": "chime",
        "host": "mqtt.chimesh.org",
        "port": 1883,
        "username": "meshdev",
        "password": "***",
        "tls": False,
        "mode": "in",
        "status": False,
        "publish_topic": "meshcore/armaros",
        "subscribe_topic": "meshcore/armaros",
        "status_topic": "meshcore/status",
    },
    {
        "name": "meshtastic-official",
        "host": "mqtt.meshtastic.org",
        "port": 1883,
        "username": "meshdev",
        "password": "***",
        "tls": False,
        "mode": "in",
        "status": False,
        "publish_topic": "meshcore/armaros",
        "subscribe_topic": "meshcore/armaros",
        "status_topic": "meshcore/status",
    },
    {
        "name": "local",
        "host": "127.0.0.1",
        "port": 1883,
        "username": "",
        "password": "",
        "tls": False,
        "mode": "both",
        "status": True,
        "publish_topic": "meshcore/armaros",
        "subscribe_topic": "meshcore/armaros",
        "status_topic": "meshcore/status",
    },
]


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        brokers = cfg.get("brokers") or DEFAULT_BROKERS
        return brokers
    except Exception:
        return DEFAULT_BROKERS


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"offset": 0, "status_offset": 0, "published": [], "received": [], "injected": []}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[mqtt-bridge] state save failed: {e}", file=sys.stderr)


def prune(entries, now, hold):
    return [(k, t) for (k, t) in entries if (now - t) < hold][-MAX_TRACKED:]


def tail_new_lines(path, state, key):
    """Return lines appended after the last-read offset for `path`; advance."""
    new = []
    try:
        size = os.path.getsize(path)
        offset = state.get(key, 0)
        if size < offset:
            offset = 0  # file rotated/truncated
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
        if not chunk:
            return new
        state[key] = offset + len(chunk)
        for line in chunk.decode("utf-8", "replace").splitlines():
            if line.strip():
                new.append(line)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[mqtt-bridge] tail {path}: {e}", file=sys.stderr)
    return new


def queue_radio_send(net, text):
    """Write a send command to the gateway-owned queue (bypasses the port
    conflict: the gateway itself holds the radios and polls this file)."""
    try:
        with open(SEND_FILE, "a") as f:
            f.write(json.dumps({"net": net, "text": text}) + "\n")
    except Exception as e:
        print(f"[mqtt-bridge] send-queue write failed: {e}", file=sys.stderr)


class Bridge:
    def __init__(self):
        self.brokers = load_config()
        self.state = load_state()
        self.lock = threading.Lock()
        self.clients = []
        self.by_name = {}

    # -- MQTT callbacks -----------------------------------------------------
    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        cfg = userdata or {}
        name = cfg.get("name", "?")
        if reason_code == 0:
            if cfg.get("mode") in ("in", "both"):
                topic = cfg.get("subscribe_topic", "")
                if topic:
                    client.subscribe(topic, qos=0)
                    print(f"[mqtt-bridge] {name}: subscribed {topic}")
            print(f"[mqtt-bridge] connected to {name} ({cfg.get('host')})")
        else:
            print(f"[mqtt-bridge] {name} connect failed: {reason_code}", file=sys.stderr)

    def on_message(self, client, userdata, msg):
        cfg = userdata or {}
        name = cfg.get("name", "?")
        try:
            payload = json.loads(msg.payload.decode("utf-8", "replace"))
        except Exception:
            print(f"[mqtt-bridge] {name}: non-JSON payload on {msg.topic}, ignored")
            return
        net = payload.get("net", "mc")
        text = (payload.get("text") or "").strip()
        sender = payload.get("sender", "mqtt")
        if not text:
            return
        # Design rule (2026-08-25): MQTT may inject single-network sends only
        # (mc/mt). Reject "both"/other so a public-broker payload can never
        # trigger a broadcast on both radios.
        if net not in ("mc", "mt"):
            print(f"[mqtt-bridge] {name}: rejecting net={net!r} injection (mc/mt only)")
            return
        now = time.time()
        key = f"{net}|{sender}|{text}"
        with self.lock:
            st = self.state
            st["received"] = prune(st["received"], now, HOLD_S)
            if any(k == key for k, _ in st["received"]):
                return  # already handled (e.g. same msg on two brokers)
            st["received"].append((key, now))
            # remember we injected it, to suppress the radio echo on publish
            st["injected"] = prune(st["injected"], now, INJECT_S)
            st["injected"].append((f"{net}|{text}", now))
            save_state(st)
        print(f"[mqtt-bridge] {name}: relay to radio: {text}")
        queue_radio_send(net, text)

    # -- publish helpers ----------------------------------------------------
    def publish_to(self, brokers, topic, obj):
        payload = json.dumps(obj).encode()
        for c in self.clients:
            cfg = getattr(c, "_userdata", None) or {}
            if cfg.get("name") in brokers:
                try:
                    c.publish(topic, payload, qos=0)
                except Exception as e:
                    print(f"[mqtt-bridge] publish err {cfg.get('name')}: {e}",
                          file=sys.stderr)

    # -- inbound event tail (radio -> MQTT messages) ------------------------
    def publish_loop(self):
        while True:
            try:
                with self.lock:
                    st = self.state
                    now = time.time()
                    st["published"] = prune(st["published"], now, HOLD_S)
                    st["injected"] = prune(st["injected"], now, INJECT_S)
                    events = []
                    for line in tail_new_lines(EVENT_FILE, st, "offset")[:MAX_FORWARD_PER_RUN]:
                        try:
                            events.append(json.loads(line))
                        except Exception:
                            continue
                    for ev in events:
                        net = ev.get("net", "mc")
                        sender = ev.get("sender", "?")
                        text = (ev.get("text") or "").strip()
                        ts = ev.get("ts", "")
                        if not text:
                            continue
                        # never re-publish our own transmission echoes
                        if any(k == f"{net}|{text}" for k, _ in st["injected"]):
                            continue
                        key = f"{net}|{sender}|{text}|{ts}"
                        if any(k == key for k, _ in st["published"]):
                            continue
                        st["published"].append((key, now))
                        # mark as already-handled for the MQTT echo path too
                        st["received"].append((f"{net}|{sender}|{text}", now))
                        st["received"] = prune(st["received"], now, HOLD_S)
                        save_state(st)
                        out = [b["name"] for b in self.brokers
                               if b.get("mode") in ("out", "both")]
                        for b in self.brokers:
                            if b.get("mode") in ("out", "both") and b.get("publish_topic"):
                                self.publish_to([b["name"]], b["publish_topic"], ev)
            except Exception as e:
                print(f"[mqtt-bridge] publish loop err: {e}", file=sys.stderr)
            time.sleep(2)

    # -- status tail (gateway -> local MQTT) --------------------------------
    def status_loop(self):
        while True:
            try:
                with self.lock:
                    st = self.state
                    lines = tail_new_lines(STATUS_FILE, st, "status_offset")
                    if not lines:
                        time.sleep(2)
                        continue
                    status_brokers = [b["name"] for b in self.brokers if b.get("status")]
                    for line in lines:
                        try:
                            obj = json.loads(line)
                        except Exception:
                            continue
                        for b in self.brokers:
                            if b.get("status") and b.get("status_topic"):
                                self.publish_to([b["name"]], b["status_topic"], obj)
                                print(f"[mqtt-bridge] status -> {b['name']}: {line[:100]}")
                    save_state(st)
            except Exception as e:
                print(f"[mqtt-bridge] status loop err: {e}", file=sys.stderr)
            time.sleep(2)

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        for cfg in self.brokers:
            cid = f"meshbridge-{cfg.get('name','broker')}-armaros"
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=cid,
                userdata=cfg,
            )
            if cfg.get("username"):
                client.username_pw_set(cfg.get("username", ""), cfg.get("password", ""))
            client.on_connect = self.on_connect
            client.on_message = self.on_message
            if cfg.get("tls"):
                client.tls_set()
            port = int(cfg.get("port", 1883))
            try:
                client.connect_async(cfg["host"], port, keepalive=60)
                client.loop_start()
                self.clients.append(client)
                self.by_name[cfg["name"]] = client
                print(f"[mqtt-bridge] starting client for {cfg['name']} "
                      f"({cfg['host']}:{port}) mode={cfg.get('mode')} status={cfg.get('status')}")
            except Exception as e:
                print(f"[mqtt-bridge] {cfg['name']} start err: {e}", file=sys.stderr)

        threading.Thread(target=self.publish_loop, daemon=True).start()
        threading.Thread(target=self.status_loop, daemon=True).start()
        print("[mqtt-bridge] running. Ctrl-C to stop.")
        try:
            while True:
                time.sleep(5)
        except KeyboardInterrupt:
            print("[mqtt-bridge] stopping...")


if __name__ == "__main__":
    Bridge().start()
