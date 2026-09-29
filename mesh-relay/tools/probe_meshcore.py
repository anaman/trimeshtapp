#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""probe_meshcore.py — read production MeshCore node (ACM0) state WITHOUT changing anything.
Prints: self_info (radio freq/bw/sf, name, key), contacts, pending contacts.
Usage: .venv/bin/python probe_meshcore.py [--add-contact <key> <name>]
Gateway must be stopped while this runs (it owns the serial port).
"""
import asyncio, json, sys
# (run with the mesh-relay venv python so meshcore is importable)
from meshcore import MeshCore, EventType

import os as _os, glob as _glob
PORT = _os.environ.get("MESHGW_MC_PORT") or (
    _glob.glob("/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit*") or ["/dev/ttyACM0"])[0]

async def main():
    add_key, add_name = None, None
    if len(sys.argv) >= 4 and sys.argv[1] == "--add-contact":
        add_key, add_name = sys.argv[2], sys.argv[3]

    mc = await MeshCore.create_serial(PORT, auto_reconnect=True, max_reconnect_attempts=5)
    if mc is None:
        print(json.dumps({"error": "create_serial returned None"}))
        return
    # wait for connection
    for _ in range(300):
        if mc.is_connected:
            break
        await asyncio.sleep(0.1)
    if not mc.is_connected:
        print(json.dumps({"error": "not connected"}))
        return

    await mc.ensure_contacts(follow=True)
    await asyncio.sleep(1.0)

    si = mc.self_info or {}
    out = {
        "self_info": {
            k: si.get(k) for k in ("name", "public_key", "radio_freq", "radio_bw", "radio_sf", "radio_cr", "manual_add_contacts")
        },
        "contacts": mc.contacts,
        "pending_contacts": mc.pending_contacts,
    }

    if add_key and add_name:
        # find pending/known contact by key prefix to build full contact dict
        contact = None
        for c in list(mc.pending_contacts.values()) + list(mc.contacts.values()):
            if c.get("public_key", "").startswith(add_key.lower()):
                contact = c
                break
        if contact is None:
            contact = {"public_key": add_key.lower(), "name": add_name}
        else:
            contact["name"] = add_name
        ev = await mc.commands.add_contact(contact)
        out["add_contact_result"] = {"contact": contact, "event": ev.type if ev else None,
                                     "payload": getattr(ev, "payload", None) if ev else None}
        await mc.ensure_contacts(follow=True)
        await asyncio.sleep(0.5)
        out["contacts_after"] = mc.contacts

    print(json.dumps(out, indent=1, default=str))
    open("/tmp/probe_meshcore_out.json", "w").write(json.dumps(out, indent=1, default=str))
    await mc.disconnect()

asyncio.run(main())
