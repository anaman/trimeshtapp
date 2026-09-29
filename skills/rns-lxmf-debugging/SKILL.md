---
name: "rns-lxmf-debugging"
description: "Debug RNS/LXMF LXMessage sends: 'Invalid material supplied for destination hash calculation', empty delivery_destinations, announce handlers ignored."
---

# rns-lxmf-debugging

Debug and extend RNS (Reticulum) 1.4.x + LXMF apps: LXMF delivery to peers, announce handling, LXMessage send failures. Use when: an announce handler never fires; `TypeError: Invalid material supplied for destination hash calculation`; `StopIteration` on `delivery_destinations`; an LXMF reply silently falls back to the raw RNS path.

## Announce handlers (RNS 1.4.2)
- Register an OBJECT, not a callback: class with `aspect_filter` and `received_announce(self, destination_hash, announced_identity, app_data, **kwargs)`. A plain callable is silently ignored — no error, no log.
- `announced_identity` arrives as a recalled `RNS.Identity` (has `.hexhash` / `.hash`); usable directly in `RNS.Destination(identity, OUT, SINGLE, "lxmf", "delivery")`.

## "Invalid material supplied for destination hash calculation"
- Raised in `RNS/Destination.py` (`Destination.hash()`) when the identity arg is neither an `RNS.Identity` nor 16 raw bytes. Does NOT mean the identity is corrupt — wrong object/type was passed.
- Find the failing line: wrap EACH step of the send path with its own print inside the try/except (identity type+hash → dest → src → LXMessage → handle_outbound); rerun; trim the prints after the fix.
- Reproduce against a controlled local peer before deep-diving: a flapping remote peer (e.g. public Sideband TCP server) can produce transient send failures that vanish on retry.

## LXMRouter
- Fresh `LXMRouter(identity=..., storagepath=..., name=...)` has EMPTY `delivery_destinations`. Call `router.register_delivery_identity(router.identity, display_name=...)` first; otherwise `next(iter(router.delivery_destinations.values()))` raises StopIteration.
- `LXMessage(dest, src, text)` needs RNS.Destination objects for BOTH: dest = built from the peer identity; src = the registered DeliveryDestination.
- `router.announce()` keys by the DELIVERY hash, not the identity hash — track peers by delivery hash.
- One LXMRouter = one delivery identity only (see rns-lxmf-propagation-node).

## Payload decoding
- msgpack bin8 strings (app_data, peer names) arrive as `bytes`, not str — decode explicitly.

## Verify
1. Standalone smoke test before touching the live service (references/lxmf_smoke_test.py): fresh identity → Destination → register_delivery_identity → LXMessage → handle_outbound; must not raise.
2. End-to-end: run the project's LXMF test client against the live bridge; inject a mesh event; assert the CLIENT RECEIVES it (bidirectional loop), not just that the bridge sent.
3. TCP transport: verify with a second RNS instance in a SEPARATE configdir using a TCPClientInterface; expect announces and two-way packet delivery.
