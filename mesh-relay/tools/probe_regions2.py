# SPDX-License-Identifier: GPL-3.0-or-later
import asyncio, json, sys, logging
# (run with the mesh-relay venv python so meshcore is importable)
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
from meshcore import MeshCore

import os as _os, glob as _glob
PORT = _os.environ.get("MESHGW_MC_PORT") or (
    _glob.glob("/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit*") or ["/dev/ttyACM0"])[0]

async def main():
    mc = await MeshCore.create_serial(PORT, auto_reconnect=True, max_reconnect_attempts=5, debug=False)
    if mc is None:
        print("no conn"); return
    for _ in range(300):
        if mc.is_connected: break
        await asyncio.sleep(0.1)
    await mc.ensure_contacts(); await asyncio.sleep(0.5)
    si = mc.self_info or {}
    self_key = si.get("public_key", "")
    print("self:", si.get("name"), self_key[:16], flush=True)
    res = await mc.commands.req_regions_sync({"public_key": self_key}, timeout=30, min_timeout=20)
    print("REGIONS_RESULT:", json.dumps(res, default=str)[:2000] if res else None, flush=True)
    await mc.disconnect()

asyncio.run(main())
