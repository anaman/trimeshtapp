---
name: "reticulum-rns-integration"
description: "Reticulum (RNS) app dev: identities, destinations, packet callbacks, local-loopback tests, bridge peer-gating."
---

# Reticulum (RNS) integration

Use when building or debugging Reticulum (RNS) apps: destinations, identities, packet senders, bridges, or local test harnesses. API facts below observed with RNS 1.4.x in a python3.12 venv — grep the installed `RNS/*.py` before trusting signatures.

## Persistent identity (stable hash across restarts)
- `Identity.get_private_key()` returns BYTES, not a hex string; `Identity.from_bytes(raw)` loads raw key bytes. Never `.encode()` the result.
- Pattern: init `RNS.Reticulum()` first; load `~/.reticulum/<name>_identity` if it exists, else create Identity and write `get_private_key()` raw; build IN SINGLE Destination with that identity; `dest.announce()` and re-announce from a thread (sleep 60).
- Fresh `Identity()` each start changes the hash — peers lose the destination. Persist for any service that must stay findable.
- `Destination(identity, RNS.Destination.IN, RNS.Destination.SINGLE, "aspect_a", "aspect_b")` — the aspects must match on sender and receiver.

## Packet callback: receives bytes, not a Packet
- Observed: `dest.set_packet_callback(fn)` delivers the raw payload BYTES (`'bytes' object has no attribute 'packet_type'`); a bound method can also hit "takes 2 positional arguments but 3 were given".
- Defensive signature: `def cb(self, *args)`; `payload = args[0]`; if it has `.data`, use `.data`; bail unless bytes/bytearray.

## Local testing without a second radio/peer
- RNS delivers local-to-local: a sender in the same instance reaches an IN destination with `Transport.has_path(hash)` True over no RF.
- Test harness: sender loads the service's identity file → OUT SINGLE Destination with identical aspects → same hash → `RNS.Packet(dest, payload).send()` → service callback fires locally.
- Remote hashes: `Transport.request_path(hash)` then poll `has_path` (~15 s timeout).

## Bridge outbound: peer-gate, never self-send
- Sending to your OWN IN destination loops back locally (RNS local delivery) — mesh→RNS traffic re-enters the mesh (feedback loop).
- Register `RNS.Transport.register_announce_handler(fn)`; handler args per installed API: (aspect_filter, destination_hash, remote_identity, app_data). Store hash → (identity, ts); deliver outbound to the most-recent peer; hold/log when no peers.
- Offset-tracked file tailers re-fire old lines after offset reset/truncation — dedup by content key (ts|net|text) with a bounded set.

## Restarting services
- `pkill -f '[r]ns_bridge'` still matches YOUR own shell if the pattern text appears anywhere else in the same command line (heredocs, ExecStart strings) — it SIGTERMs the command. Keep kills in a separate exec with no other pattern text.

## Verification
- Round-trip: sender prints "sent N bytes" and path True; service log shows the payload received; downstream (file/journal) shows the forwarded line.
- With no peers, a mesh→RNS send must NOT produce a re-ingest/forward line (no loop).
