# Build journal — what was built, and why it looks like this

This project was built iteratively (Aug–Sep 2026): start with a two-network
relay, then add Reticulum, then a control plane. This journal records the
notable changes, decisions, and fixes — the *why* behind the code — so that
another operator (or their LLM) can pick it up without re-deriving it.

## 1. Origins — one relay, then a gateway

- The first cut (`MT-MC_relay.py`, kept under `mesh-relay/` for reference)
  blindly relayed Meshtastic ⇄ MeshCore channel text with an ID cache.
  Problems: no dedup that actually worked, no chat delivery, no ops surface.
- **Change:** replaced by `mesh_gateway.py` as the *single owner* of both
  radios, with three separations that everything since depends on:
  1. radios owned by one process (serial ports are exclusive);
  2. all consumers talk to it via files (event log + send queue);
  3. delivery to chat is a *separate* job (no SDKs in the gateway).

## 2. Loop prevention (this is more subtle than it looks)

- Added `MessageDedup` keyed on (sender, text) with a 1-hour reject window.
- **Bug fixed:** `hold_s` (entry retention) once defaulted *below* the reject
  window, so entries were evicted before they could reject a repeat —
  duplicates got through. Rule now: `HOLD_S >= LOOP_WINDOW_S`, and the live
  values are surfaced on the status endpoint so you can verify.
- **Change:** dedup state persisted to disk — restarts used to forget the
  window and re-relay messages that arrived on both networks around a
  restart.
- Dedup is **global across networks**: same text from the same node on the
  other network inside the window is suppressed (stops the bridge
  double-reaching users), while different messages sharing text are not
  over-dropped.

## 3. Radio/port handling

- Serial paths pinned by `/dev/serial/by-id` (stable across reboots), with
  auto-match by USB descriptor prefix, `ports.json`, and env overrides added
  later so nothing is host-specific.
- `mesh_relay_setup.py` gained `scan` / `apply` / `flush` / `status`:
  discovery probes both protocols read-only and never holds a locked port;
  `apply` writes `ports.json` (earlier versions patched the source file —
  fragile; replaced).
- **Operational rule recorded:** all sends go through
  `/tmp/meshgw_send.txt`; a second serial connection always fails with
  `Could not exclusively lock port`. Tools that need the port (probes,
  rnodeconf, esptool) require stopping the owning service first.

## 4. Firmware & RF work (MeshCore side)

- Established a known-good firmware baseline (byte-verified flash) and used
  it while investigating regional profile behavior — the key lesson: a
  PHY-valid packet can still be rejected at the forwarding layer when the
  node's region/transport profile doesn't match the network.
- Verified the **RS232 bridge framing** (Fletcher-16, C0 3E magic) directly
  from MeshCore source before designing any converter — see
  `docs/PROTOCOLS.md` §3 and `esp32/rs232-bridge-protocol.md`.
- Added region work: `mesh-relay/tools/regions.py` (GPS → national region →
  MeshCore profile + Meshtastic LongFast; no-GPS fallback scan planned).
- Debug playbooks captured as skills: node-link debugging (CP2102 console
  wedge recovery via sysfs unbind/bind; stuck-RTC meaning "never heard a
  packet"), custom firmware builds, display-sleep configuration.

## 5. MQTT leg

- Added `mqtt_bridge.py`: multi-broker client with per-broker modes.
  Community/public brokers are subscribe-only (`in`); the local broker is
  `both` and also receives status snapshots.
- **Change (security):** MQTT injection restricted to single-network sends
  (`mc`/`mt`); a payload from a public broker can never trigger a two-radio
  broadcast.
- Loop guards on the MQTT side mirror the gateway: published/received key
  sets plus a short "injected" window so radio echoes of our own
  transmissions are never re-published.

## 6. Reticulum leg

- `rns_bridge.py` added: announces one destination over the RNode LoRa leg,
  routes RNS→mesh (target-byte protocol) and mesh→RNS (delivery to the most
  recently announced peer; **never to itself** — local self-delivery would
  loop the traffic back into the mesh).
- Persistent identity file so the destination hash is stable across
  restarts (a fresh identity per start made the bridge unfindable).
- **LXMF support** added (Sideband compatibility): the bridge registers a
  delivery identity and can message/receive with LXMF peers.
- **Routing rule added after real-world flooding:** unprefixed LXMF messages
  default to **chat-delivery only** — earlier, bot/automation replies sent
  to the public bridge address were auto-broadcast over both radios. Only
  explicit `MC:` / `MT:` / `BOTH:` prefixes transmit now.
- `rns_propagation.py`: local LXMF propagation node so clients can mint
  stamps without remote nodes.
- `rns_peer_tracker.py`: because per-peer chat pings were too noisy, peer
  discovery became a SQLite registry + "N unique peers in the last period"
  notices (period configurable; notices throttled and consumable exactly
  once).
- Status server added for a small read-only RNS web view; the bridge itself
  dumps transport state to `/tmp/rns_bridge_status.json` because in-process
  RNS state is invisible to `rnstatus`.
- **Bug fixed:** a batch launch had left 12 duplicate `rns_bridge.py`
  processes pegged at 100% CPU, all holding the RNode. Fix: restart the
  service (single process) + watch for >1 process. Root cause of the batch
  launch was never found (no trace); the lesson is to monitor process
  count.

## 7. Control plane — the web dashboard

- `lora-dashboard/` added: single-pane panel over everything above.
- **Login change:** authenticate against the local mosquitto broker with an
  authenticated MQTT CONNECT (no second password store) + rate limiting
  (5 fails → 15 min), signed session cookie, CSRF on all POSTs, JSONL audit
  log.
- **Safety change:** service restarts via `sudo -n systemctl` restricted to
  a strict unit allowlist.
- Panels: Bridge (MQTT + RNS settings, restarts), Protocols (MeshCore over
  TCP, Meshtastic CLI, RNode EEPROM — read-before-write with defaults),
  Devices (scanner for MeshCore `:5000` / Meshtastic `:4403` across LAN,
  overlay networks, peer-advertised subnets; persistent registry; auto-scan),
  Logs (audit).
- **Ops lessons:** RNode EEPROM writes require briefly stopping `rnsd`;
  Meshtastic `--get` right after `--set` can show the stale value (wait ~5 s
  and re-read); never probe the production radio port with esptool (kicks
  the node into its ROM bootloader).

## 8. ESP32-C6 WiFi hub (optional)

- Design + firmware for an ESP32-C6 acting as a WiFi-side hub: TCP client to
  Heltec V3 nodes (MeshCore `SerialWifiInterface` framing), frame
  aggregation, MQTT relay upstream, mDNS discovery (`_meshcore._tcp`),
  HTTP status page. Lets a wired-only node be reached without a USB host.

## 9. Delivery-agent pattern (chat integration)

- The final chat integration is *deliberately thin*: the watcher writes
  delivery directives; an agent (the reference deployment used an LLM agent
  runtime) tailing the file sends them via Telegram/WhatsApp and handles
  replies. No credentials or SDKs live in the bridge processes.
- Command set for replies: `mt` / `mc` / `rt` / `both` sends, `dmc`/`dmt`
  DM replies, contact save/forget/list, advert helpers.

## 10. Deliberately deferred

- BLE client API, OTA-over-LoRa, automatic protocol bridging without
  explicit rules, and a dual-radio ESP32 converter firmware. Rationale:
  observe first, route second; keep each network native; make the gateway
  the place where policy lives. See `docs/BUILD-DIRECTION.md` for the full
  build order that this repo follows.
