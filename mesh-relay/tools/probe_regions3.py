# SPDX-License-Identifier: GPL-3.0-or-later
import asyncio, json, sys
# (run with the mesh-relay venv python so meshcore is importable)
from meshcore import MeshCore

import os as _os, glob as _glob
PORT = _os.environ.get("MESHGW_MC_PORT") or (
    _glob.glob("/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit*") or ["/dev/ttyACM0"])[0]
TARGETS = ["NODE-A", "Repeater 1", "Repeater 2", "Example Node"]  # put names from your contact list here

async def main():
    mc = await MeshCore.create_serial(PORT, auto_reconnect=True, max_reconnect_attempts=5)
    if mc is None:
        print(json.dumps({"error": "no conn"})); return
    for _ in range(300):
        if mc.is_connected: break
        await asyncio.sleep(0.1)
    await mc.ensure_contacts(); await asyncio.sleep(0.5)

    out = {}
    for name in TARGETS:
        contact = mc.get_contact_by_name(name)
        if not contact:
            out[name] = "contact not found"
            continue
        try:
            res = await mc.commands.req_regions_sync(contact, timeout=40, min_timeout=25)
            out[name] = res
            print(f"--- {name}: {str(res)[:600]}", flush=True)
        except Exception as e:
            out[name] = f"err: {e}"
            print(f"--- {name}: ERR {e}", flush=True)
    open("/tmp/probe_regions3_out.json", "w").write(json.dumps(out, indent=1, default=str))
    await mc.disconnect()

asyncio.run(main())
