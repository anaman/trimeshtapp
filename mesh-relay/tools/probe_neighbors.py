#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""probe_neighbors.py — fetch the MeshCore node's RF neighbor list (what the radio physically hears)."""
import asyncio, json, sys
# (run with the mesh-relay venv python so meshcore is importable)
from meshcore import MeshCore

import os as _os, glob as _glob
PORT = _os.environ.get("MESHGW_MC_PORT") or (
    _glob.glob("/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit*") or ["/dev/ttyACM0"])[0]

async def main():
    mc = await MeshCore.create_serial(PORT, auto_reconnect=True, max_reconnect_attempts=5)
    if mc is None:
        print(json.dumps({"error": "no connection"})); return
    for _ in range(300):
        if mc.is_connected: break
        await asyncio.sleep(0.1)
    await mc.ensure_contacts()
    await asyncio.sleep(0.5)
    si = mc.self_info or {}
    self_key = si.get("public_key", "")
    print("self:", si.get("name"), self_key[:16], flush=True)
    contact = {"public_key": self_key}
    res = await mc.commands.req_neighbours_sync(contact, count=255, timeout=20, min_timeout=5)
    print("=== NEIGHBOURS ===", flush=True)
    print(json.dumps(res, indent=1, default=str)[:4000], flush=True)
    await mc.disconnect()

asyncio.run(main())
