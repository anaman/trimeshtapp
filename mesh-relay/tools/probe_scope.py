# SPDX-License-Identifier: GPL-3.0-or-later
import asyncio, json, sys
# (run with the mesh-relay venv python so meshcore is importable)
from meshcore import MeshCore, EventType

import os as _os, glob as _glob
PORT = _os.environ.get("MESHGW_MC_PORT") or (
    _glob.glob("/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit*") or ["/dev/ttyACM0"])[0]

async def main():
    mc = await MeshCore.create_serial(PORT, auto_reconnect=True, max_reconnect_attempts=5)
    if mc is None:
        print(json.dumps({"error": "no conn"})); return
    for _ in range(300):
        if mc.is_connected: break
        await asyncio.sleep(0.1)

    result = {}
    async def on_scope(event):
        result["scope"] = event.payload
        print("SCOPE:", json.dumps(event.payload), flush=True)
    mc.subscribe(EventType.DEFAULT_FLOOD_SCOPE, on_scope)

    # trigger device query (may elicit scope frame)
    try:
        r = await mc.commands.send_device_query()
        print("device_query event:", r.type if r else None, flush=True)
    except Exception as e:
        print("dq err:", e, flush=True)

    for _ in range(30):
        if "scope" in result: break
        await asyncio.sleep(0.5)
    print("RESULT:", json.dumps(result, default=str)[:800], flush=True)
    open("/tmp/probe_scope_out.json", "w").write(json.dumps(result, default=str))
    await mc.disconnect()

asyncio.run(main())
