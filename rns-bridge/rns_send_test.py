#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""rns_send_test.py — send a test packet to the bridge using its persistent identity."""
import sys, time, os
# Run with the rns-bridge venv python.
import json
from pathlib import Path
import RNS

TARGET = int(sys.argv[1]) if len(sys.argv) > 1 else 0x01
TEXT = sys.argv[2] if len(sys.argv) > 2 else "RNS bridge test hello"

RNS.Reticulum()
ID_FILE = os.path.expanduser("~/.reticulum/meshbridge_identity")
identity = RNS.Identity.from_bytes(open(ID_FILE, "rb").read())
cfg = {}
try:
    cfg = json.loads((Path(__file__).resolve().parent / "rns_bridge.json").read_text())
except Exception:
    pass
APP = cfg.get("announce_app", "meshbridge")
ASPECT = cfg.get("announce_name", "bridge")
dest = RNS.Destination(identity, RNS.Destination.OUT, RNS.Destination.SINGLE, APP, ASPECT)
print(f"[sender] destination {dest.hash.hex()} (path: {RNS.Transport.has_path(dest.hash)})", flush=True)
if not RNS.Transport.has_path(dest.hash):
    RNS.Transport.request_path(dest.hash)
    t1 = time.time()
    while not RNS.Transport.has_path(dest.hash) and time.time() - t1 < 15:
        time.sleep(0.5)
print(f"[sender] path: {RNS.Transport.has_path(dest.hash)}", flush=True)
payload = bytes([TARGET]) + TEXT.encode()
RNS.Packet(dest, payload).send()
print(f"[sender] sent {len(payload)} bytes target=0x{TARGET:02x}", flush=True)
time.sleep(4)
print("[sender] done", flush=True)
