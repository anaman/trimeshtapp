---
name: "mesh-relay-setup"
description: "Mesh Relay Setup: deploy or validate the MeshCore <-> Meshtastic gateway; discover radios, write ports.json, flush/restart safely, verify end-to-end."
metadata:
  license: GPL-3.0-or-later
  user-invocable: true
---

# Mesh Relay Setup (MeshCore <-> Meshtastic gateway)

Set up or validate the unified mesh gateway that bridges a MeshCore node and
a Meshtastic radio, draining both networks' public channels to Telegram
(primary) / WhatsApp (failover) with a reply path back over the radios.

Use when the user wants to (re)deploy the mesh bridge, discover which USB
port is which radio, check gateway/service health, or after re-plugging radios.

## What it runs on
- One MeshCore companion-radio USB node (ESP32-class).
- One Meshtastic radio (e.g. Heltec Wireless Tracker).
- A host running the mesh-gateway service plus a delivery watcher (mesh_watch.py).

## Files
- `mesh-relay/mesh_relay_setup.py` — discovery + status/apply/flush CLI.
- `mesh-relay/mesh_gateway.py` — owns both radios; drains events; MessageDedup loop prevention.
- `mesh-relay/ports.json` — radio port map, written by `apply` and read by the gateway.

## Workflow

1. **Discover the radios** (ports may be held by the running gateway; to get a
   clean scan, stop the service first or run while ports are free):
   ```bash
   python3 mesh_relay_setup.py scan
   ```
   Expect both lines: MeshCore (USB_JTAG descriptor) and Meshtastic
   (Heltec_Tracker). If either is NOT FOUND, the port is busy (gateway
   running) or there is a cable/role problem — see Troubleshooting.

2. **Check health** (works anytime; the gateway may be in its 2-min boot delay):
   ```bash
   python3 mesh_relay_setup.py status
   ```
   The status endpoint http://127.0.0.1:8082 also returns `dedup` stats
   (tracked / window_s / hold_s / max_tracked). Expected: window_s=3600
   (1 hr), hold_s=3600, max_tracked=120.

3. **Apply discovered ports** (writes ports.json; the gateway reads it at start):
   ```bash
   python3 mesh_relay_setup.py apply --yes
   sudo systemctl restart mesh-gateway.service   # note: 2-min boot delay
   ```

4. **Flush buffers / clear gateway state** (clears events, outbound queue,
   send queue, dedup memory, and watcher offset so old messages do not
   re-deliver; then restarts the gateway to re-init BOTH radio links):
   ```bash
   python3 mesh_relay_setup.py flush
   ```
   This is gateway-side history only. On-device radio history is NOT wiped
   (the MeshCore lib has no message-delete primitive; factory reset is far
   more destructive and requires explicit confirmation).

5. **Verify end-to-end**:
   - Gateway: `systemctl is-active mesh-gateway.service` -> active;
     status endpoint http://127.0.0.1:8082 returns `{"status":"active",...}`.
   - Inbound: send a message on either radio -> should arrive in Telegram
     within ~5s (the 5s watcher job announces it).
   - Reply: user sends `mt: <text>` / `mc: <text>` / `both: <text>` ->
     agent runs `meshgw reply {mc|mt|both} "<text>"` -> gateway transmits.
   - Dedup: send the same sender+text on the other network within 1 hour ->
     the duplicate is suppressed (log shows `[dedup] suppressed repeat`).

## Deployed components
- `mesh-gateway.service` — owns BOTH radios; drains to
  /tmp/meshgw_events.jsonl; status :8082; polls /tmp/meshgw_send.txt for
  outbound; 120s boot delay (TimeoutStartSec=300); Restart=on-failure.
- 5s watcher job (mesh_watch.py) — command payload (NO model cost), announces
  new mesh messages to your chat channel.
- Health check job (30 min) — checks mesh-gateway.service, alerts on failure.
- `meshgw` — start/stop/status/tail/reply/events helper.

## Regional frequency behavior
- MeshCore: the software should not care which frequency is active — detect
  and respond on the regional frequency (GPS-derived for the set national
  region, see `mesh-relay/tools/regions.py`).
- Meshtastic: always use LongFast for the region.

## Relay-loop prevention (MessageDedup)
The gateway rejects inbound messages that repeat a recent (sender, text) to
stop MT<->MC relay loops. This is GLOBAL across both networks: a message
first seen on MeshCore suppresses the same message arriving on Meshtastic
within the window, and vice versa (stops the bridge double-reaching the
user / double-relaying).
- window_s = 3600 (1 hour): a repeat inside this window is dropped.
- hold_s = 3600 (1 hour): entries are retained for the full reject window.
  (MUST be >= window_s or entries would be evicted before the window can
  reject — this was a real bug when hold_s=120 < window_s=180.)
- max_tracked = 120: only the last 120 messages are tracked (bounded).
- PERSISTENT: state in /tmp/meshgw_dedup.json, so a gateway restart does NOT
  forget recent history (avoids loops across restarts).
- Keyed on sender+text (mesh-wide), not just text or node — different
  messages with the same text are NOT over-dropped.
Config knobs are the LOOP_WINDOW_S / HOLD_S / MAX_TRACKED constants at the
top of mesh_gateway.py; `status`/the endpoint surface the live values.

## Key rules / gotchas
- Both radios' serial ports are LOCKED by the running gateway. Any second
  serial connection fails (`Could not exclusively lock port`). All radio
  sends MUST go through the gateway via /tmp/meshgw_send.txt
  (`meshgw reply`), never a second connection.
- The 5-s watcher is a command-payload job (no model billing). Do NOT use a
  model-billed agent turn for a 5s loop.
- Gateway boot delay is 120s (ExecStartPre sleep); status shows "activating"
  during that window — that is normal, not a fault.
- After editing mesh_gateway.py, `systemctl restart` re-runs the 120s delay.
- hold_s must stay >= window_s for loop prevention to work.

## Troubleshooting
- Radios not found by scan: stop the gateway first
  (`sudo systemctl stop mesh-gateway.service`), then scan; restart after.
  'unidentified/busy' entries are locked ports.
- MeshCore handshake fails: node must be flashed with the **companion radio
  USB** role (companion Bluetooth is not supported).
- Telegram receive works but no message: confirm the 5s watcher job is
  enabled and `mesh_watch.py` exists.
- "flush" shows buffers repopulating: that is NEW live mesh traffic, not a
  failure — the flush only clears history at the moment it runs.
