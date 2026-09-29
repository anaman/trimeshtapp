#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Standalone LXMF construction smoke test — proven working 2026-08-20.
Run BEFORE touching the live bridge/service: catches Destination and
LXMRouter setup errors in isolation.
"""
import RNS
RNS.Reticulum(configdir="/tmp/rns-smoke")  # isolated config dir
from LXMF import LXMRouter, LXMessage

peer_identity = RNS.Identity()  # simulate an announced peer identity
dest = RNS.Destination(peer_identity, RNS.Destination.OUT,
                       RNS.Destination.SINGLE, "lxmf", "delivery")
print("dest hash:", dest.hash.hex())

router = LXMRouter(identity=RNS.Identity(), storagepath="/tmp/lxmf-smoke", name="Smoke")
router.register_delivery_identity(router.identity, display_name="Smoke")  # REQUIRED
# StopIteration here means register_delivery_identity was missed:
dd = next(iter(router.delivery_destinations.values()))
print("src dd:", dd.hash.hex())

msg = LXMessage(dest, dd, "test content")
router.handle_outbound(msg)
print("smoke OK")
