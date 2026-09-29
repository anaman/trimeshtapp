#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""probe_regions.py — fetch the MeshCore node's region/transport map (the RF config
the regional network runs). Gateway must be stopped while this runs."""
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
    contact = {"public_key": self_key}
    out = {"self": si.get("name"), "self_key": self_key[:16]}
    try:
        res = await mc.commands.req_regions_async(contact, timeout=20, min_timeout=5)
        out["regions"] = res.payload if res else None
    except Exception as e:
        out["regions_error"] = str(e)
    # also try sync variant
    try:
        res2 = await mc.commands.req_regions_sync(contact, timeout=20, min_timeout=5)
        out["regions_sync"] = res2
    except Exception as e:
        out["regions_sync_error"] = str(e)
    open("/tmp/probe_regions_out.json", "w").write(json.dumps(out, indent=1, default=str))
    print("done", flush=True)
    await mc.disconnect()

asyncio.run(main())
