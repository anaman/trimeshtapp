# Architecture

## 1. System overview

The bridge merges three LoRa network families that each keep their own
protocol and encryption:

```
                 ┌────────────────────────────────────────────────────────────┐
                 │                        Linux host                          │
                 │                                                            │
  MeshCore node  │  ┌───────────────────┐        /tmp/meshgw_events.jsonl   │
  (USB serial) ──┼─►│  mesh_gateway.py  ├──────► (inbound events) ────┐      │
                 │  │  owns BOTH radios │◄────── /tmp/meshgw_send.txt  │      │
 Meshtastic radio│  └───────────────────┘        (outbound queue)      ▼      │
  (USB serial) ──┼─►        ▲ │                        ┌──────────────────┐  │
                 │         │ │                        │  mesh_watch.py   │  │
                 │         │ │  ┌───────────────┐     │ (delivery agent) │  │
                 │         │ └─►│ mqtt_bridge.py│     └───────┬──────────┘  │
                 │         │    └───────┬───────┘             │ chat        │
                 │         │            ▼                     ▼             │
                 │         │      MQTT brokers         Telegram/WhatsApp   │
                 │         │                                              │
  RNode (Heltec  │  ┌──────┴────────┐      ┌──────────────────┐          │
  V3, KISS/USB)──┼─►│    rnsd       │◄────►│  rns_bridge.py   │          │
                 │  └──────┬────────┘      │ (RNS ⇄ mesh IPC) │          │
                 │         │               └────────┬─────────┘          │
                 │         ▼                        ▼                    │
                 │   RNS peers (LoRa/TCP)   /tmp/rns_send.txt etc.        │
                 │                                                          │
                 │  ┌──────────────────────────────────┐                    │
                 │  │ lora-dashboard (web control)     │                    │
                 │  │ services · settings · protocols  │                    │
                 │  │ devices · logs · audit           │                    │
                 │  └──────────────────────────────────┘                    │
                 └────────────────────────────────────────────────────────────┘
```

## 2. Components

### 2.1 Mesh gateway (`mesh_gateway.py`)

- One asyncio process, the **single owner of both radios** (MeshCore +
  Meshtastic serial ports). Serial ports are exclusive; a second process
  opening them fails with `Could not exclusively lock port`.
- Subscribes to MeshCore channel/DM events and Meshtastic text messages.
- Writes every inbound message to `/tmp/meshgw_events.jsonl` (one JSON per
  line) for downstream consumers (watcher, RNS bridge, MQTT bridge).
- Reads outbound commands from `/tmp/meshgw_send.txt` — the only correct way
  to transmit. Commands: `{"net": "mc"|"mt"|"both", "text": ...}`, DM replies
  add `{"dm": true, "target": ...}`, contact ops use `{"cmd": "contact"|"advert"|...}`.
- Optional MT⇄MC auto-bridge (`MESHGW_BRIDGE=1`) with an hour-wide, persistent,
  cross-network **MessageDedup** to prevent relay loops and double delivery.
- Headless status endpoint on `127.0.0.1:8082` (JSON, includes live dedup stats).
- Contact registry with idle expiry (`state/meshgw_contacts.json`) and DM
  reply routing (last DM peer per network).

### 2.2 Channel watcher (`mesh_watch.py`)

- Runs on a short cadence (reference: 5 s as a command job — no model cost).
- Tails the event file, emits delivery directives to
  `/tmp/meshgw_outbound.jsonl` (the delivery agent picks them up), and
  processes reply commands from `/tmp/meshgw_inbox.jsonl`:
  `mt` / `mc` / `rt` (RNS) / `both` sends, DM replies (`dmc`, `dmt`),
  contact management (`save`/`forget`/`contacts`).
- Keeps offset + dedup state in `/tmp/meshwatch_state.json` (idempotent).

### 2.3 MQTT bridge (`mqtt_bridge.py`)

- Multi-broker paho client. Per-broker modes: `in` (subscribe only — used
  for community brokers), `out`, `both` (local broker).
- Radio→MQTT: publishes events; gateway→MQTT: publishes status snapshots
  (`/tmp/meshgw_status.jsonl`) on `status_topic`.
- MQTT→radio: injects single-network sends (`mc`/`mt` only — a public-broker
  payload can never trigger a two-radio broadcast).
- Loop prevention: published/received/injected key sets (bounded, persisted
  in `/tmp/mqtt_bridge_state.json`).

### 2.4 RNS bridge (`rns_bridge.py`)

- Connects to the local RNS transport (`rnsd`), owns **no** serial ports.
- Announces one destination (`announce_app`/`announce_name` from
  `rns_bridge.json`, default `meshbridge.bridge`) so RNS peers can find it.
- Payload protocol: **byte 0 = target** (`0x01` MeshCore, `0x02` Meshtastic,
  `0x03` both, `0x04` status/echo, `0x05` direct-RNS user message), rest is
  UTF-8 text → written to the gateway's send queue.
- Mesh→RNS: tails the event file, delivers to the most recently announced
  peer (never to itself — that would loop into the mesh). Prefers LXMF
  (Sideband) peers when present.
- LXMF node: registers a delivery identity for Sideband compatibility;
  routing prefixes `MC:` / `MT:` / `BOTH:` in LXMF message bodies; **default
  (unprefixed) LXMF traffic is Telegram-only** and is never transmitted on
  the radios (prevents bot/automation replies from flooding both meshes).
- Dumps live transport state to `/tmp/rns_bridge_status.json` every 30 s
  (in-process RNS state is invisible to external `rnstatus`).

### 2.5 Peer tracker (`rns_peer_tracker.py`)

- Consumes first-seen peer events from the bridge's persistent registry
  (`state/rns_peer_registry.json`, `logs/rns_new_peers.jsonl`).
- SQLite DB (`state/rns_peers.db`): stable token IDs per peer hash,
  first/last seen, LXMF flag. Reports "N unique peers in the last period"
  with configurable period (seconds→days).
- Writes a single-line notice file when a notice is due; `--check` consumes
  it exactly once (for cron delivery). `--daily` emails a digest.

### 2.6 Propagation node (`rns_propagation.py`)

- LXMF propagation node so local clients can mint stamps without depending
  on remote public nodes. Persistent identity, periodic announcements.

### 2.7 Web dashboard (`lora-dashboard/app.py`)

Single-pane control panel (Flask + waitress, localhost-bound):

- **Login**: verifies username/password via an authenticated MQTT CONNECT
  against the local mosquitto broker (no separate password store).
  Rate limiting (5 fails → 15 min lockout, per user+IP), signed session
  cookie, CSRF token on every POST.
- **Overview**: service states (mesh-gateway, mqtt-bridge, rns-bridge,
  rns-status-server, rnsd, mosquitto, dashboard), gateway status, RNS leg
  status, live event feed, serial device presence.
- **Bridge tab**: edit MQTT brokers and RNS bridge settings; restarts the
  relevant service via a strict `sudo systemctl` allowlist.
- **Protocols tab**: MeshCore over TCP (`:5000`, meshcore lib — telemetry,
  tuning, stats, advert, reboot), Meshtastic over TCP (`:4403`, meshtastic
  CLI — `--export-config`, `--begin-edit/--commit-edit`), RNode EEPROM over
  serial (`rnodeconf`; stops rnsd briefly since it owns the port).
- **Devices tab**: scanner for MeshCore companion nodes (`:5000`) and
  Meshtastic API (`:4403`) across your LAN /24, overlay-network peers and
  peer-advertised subnets; persistent registry with first/last seen and
  NEW badges; optional auto-scan interval.
- **Logs tab**: audit trail (JSONL) of logins and every state-changing call.

### 2.8 ESP32-C6 WiFi hub (`esp32/c6-wifi-server/`)

Optional hardware add-on: the C6 connects to Heltec V3 nodes over WiFi
(MeshCore `SerialWifiInterface` TCP framing), aggregates frames and relays
them upstream via MQTT, with its own HTTP status page. See its README/DESIGN.

## 3. IPC files (the integration surface)

All cross-component communication is deliberately file-based (crash-safe,
inspectable, restart-tolerant):

| File | Writer → Reader | Purpose |
|---|---|---|
| `/tmp/meshgw_events.jsonl` | gateway → watcher, rns bridge, mqtt bridge, dashboard | inbound messages |
| `/tmp/meshgw_send.txt` | watcher, rns bridge, mqtt bridge → gateway | outbound radio commands |
| `/tmp/rns_send.txt` | watcher → rns bridge | outbound RNS sends (`rt`/`both`) |
| `/tmp/rns_lxmf_inbound.jsonl` | rns bridge → watcher | unprefixed LXMF → chat only |
| `/tmp/meshgw_outbound.jsonl` | watcher → delivery agent | chat delivery directives |
| `/tmp/meshgw_inbox.jsonl` | delivery agent → watcher | user reply commands |
| `/tmp/meshgw_notice.jsonl` | gateway → watcher | operational notices |
| `/tmp/meshgw_dedup.json` | gateway (persistent) | loop-prevention state |
| `/tmp/rns_bridge_status.json` | rns bridge → dashboard | live RNS state |
| `/tmp/rns_peer_tracker_status.json` | peer tracker → dashboard | peer counts |
| `/tmp/mqtt_bridge_state.json` | mqtt bridge (persistent) | offsets + dedup |

## 4. Design principles

1. **Observe first, route second.** Diagnostics and visibility come before
   any forwarding. The system shows *why* something is (not) happening.
2. **One owner per port.** The gateway owns the radios; RNS bridge owns
   nothing; `rnsd` owns the RNode. Everything else communicates via files.
3. **Never translate between protocols at the packet layer.** Each network
   stays itself; bridging happens at the message layer with explicit rules.
4. **Loop prevention is global, persistent and bounded.** (sender, text)
   window across both networks; survives restarts; capped size.
5. **Delivery is delegated.** The bridge writes events; a separate agent
   decides how/where to deliver (chat apps), keeping SDKs and credentials
   out of the bridge processes.
6. **Localhost by default.** Every HTTP surface binds to 127.0.0.1; exposing
   them is an explicit, documented choice (reverse proxy / VPN).

## 5. Service map

| Service | Unit | Ports |
|---|---|---|
| Mesh gateway | `mesh-gateway.service` | 127.0.0.1:8082 |
| Mesh Live | `mesh-live.service` | :8462 |
| MQTT bridge | `mqtt-bridge.service` | — (outbound MQTT) |
| Reticulum transport | `rnsd.service` | — |
| RNS bridge | `rns-bridge.service` | — |
| RNS status | `rns-status-server.service` | 127.0.0.1:8451 |
| Peer tracker | `rns-peer-tracker.service` | — |
| Propagation node | `rns-propagation.service` | — |
| Dashboard | `lora-dashboard.service` | 127.0.0.1:8452 |
| MQTT broker | `mosquitto.service` | 127.0.0.1:1883 |
