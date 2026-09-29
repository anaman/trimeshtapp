#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""rns_propagation.py — LXMF Propagation Node for the Reticulum leg.

Runs a local LXMF propagation node so Sideband clients on this RNS transport
can mint stamps and send LXMF messages without depending on a remote public
propagation node.

Design (2026-08-19):
- Uses the same RNS transport as rnsd (rnsd.service owns the interfaces).
- Persistent identity at ~/.reticulum/lxmf_propagation_identity so the
  propagation destination hash stays stable across restarts.
- Announces the LXMF propagation aspect ("lxmf.propagation") periodically so
  peers discover the node.
- Requires: a venv with RNS + LXMF installed (run with that python).

Run: .venv/bin/python rns_propagation.py
"""
import os
import sys
import time
import threading

# Run with the rns-bridge venv python (RNS + LXMF installed there).
import RNS  # noqa: E402
from LXMF import LXMRouter  # noqa: E402

IDENTITY_FILE = os.path.expanduser("~/.reticulum/lxmf_propagation_identity")
STORAGE_PATH = os.path.expanduser("~/.reticulum/lxmf_propagation_store")
ANNOUNCE_INTERVAL = 300  # seconds


def load_or_create_identity():
    identity = None
    if os.path.exists(IDENTITY_FILE):
        try:
            with open(IDENTITY_FILE, "rb") as f:
                identity = RNS.Identity.from_bytes(f.read())
            print(f"[prop] loaded identity from {IDENTITY_FILE}")
        except Exception as e:
            print(f"[prop] identity load failed: {e}")
    if identity is None:
        identity = RNS.Identity()
        with open(IDENTITY_FILE, "wb") as f:
            f.write(identity.get_private_key())
        print(f"[prop] created new identity at {IDENTITY_FILE}")
    return identity


def main():
    print("[prop] starting LXMF Propagation Node…")
    RNS.Reticulum()

    identity = load_or_create_identity()
    router = LXMRouter(
        identity=identity,
        storagepath=STORAGE_PATH,
        name="mesh-propagation",
        enforce_stamps=False,
    )
    router.enable_propagation()
    print(f"[prop] propagation node enabled, identity hash: {identity.hash.hex()}")

    def reannounce():
        while True:
            time.sleep(ANNOUNCE_INTERVAL)
            try:
                router.announce_propagation_node()
                print("[prop] re-announced propagation node", flush=True)
            except Exception as e:
                print(f"[prop] re-announce failed: {e}", file=sys.stderr)

    threading.Thread(target=reannounce, daemon=True).start()
    print("[prop] running. Ctrl-C to stop.", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[prop] stopped")


if __name__ == "__main__":
    main()
