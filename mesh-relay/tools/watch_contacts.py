#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""watch_contacts.py — stay connected to the MeshCore node and report NEW_CONTACT
events + any contact whose advert just refreshed. Also dumps neighbors via CLI? No—
this is node-side only. Usage: .venv/bin/python watch_contacts.py [seconds]"""
import asyncio, json, sys, time
# (run with the mesh-relay venv python so meshcore is importable)
from meshcore import MeshCore, EventType

import os as _os, glob as _glob
PORT = _os.environ.get("MESHGW_MC_PORT") or (
    _glob.glob("/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit*") or ["/dev/ttyACM0"])[0]
DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 180
t0 = time.time()
seen = {}

async def main():
    mc = await MeshCore.create_serial(PORT, auto_reconnect=True, max_reconnect_attempts=5)
    if mc is None:
        print("ERR create_serial None"); return

    async def on_new_contact(event):
        c = event.payload or {}
        print(f"[NEW_CONTACT +{time.time()-t0:.0f}s] key={c.get('public_key','')[:16]}... name={c.get('adv_name')}", flush=True)

    async def on_contacts(event):
        for k, c in (event.payload or {}).items():
            if c.get("public_key") not in seen:
                seen[c.get("public_key")] = c
                print(f"[CONTACT +{time.time()-t0:.0f}s] {c.get('adv_name')} key={c.get('public_key','')[:16]}...", flush=True)

    mc.subscribe(EventType.NEW_CONTACT, on_new_contact)
    mc.subscribe(EventType.CONTACTS, on_contacts)
    await mc.ensure_contacts(follow=True)
    print(f"connected; watching {DURATION}s for new nodes...", flush=True)
    await asyncio.sleep(DURATION)
    print("=== final contacts (by last_advert, top 8) ===", flush=True)
    contacts = mc.contacts
    for c in sorted(contacts.values(), key=lambda x: x.get("last_advert", 0), reverse=True)[:8]:
        print(f"  {c.get('adv_name')} | last_advert={c.get('last_advert')} | key={c.get('public_key','')[:16]}...", flush=True)
    await mc.disconnect()

asyncio.run(main())
