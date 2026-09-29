# Configuration reference

Every knob the stack reads, where it lives, and what it does.

## 1. `mesh-relay/ports.json` (radio port map)

Written by `mesh_relay_setup.py apply`; read by `mesh_gateway.py` at start.
Precedence: **env var > ports.json > /dev/serial/by-id auto-match > fallback**.

```json
{ "meshcore": "/dev/ttyACM0", "meshtastic": "/dev/ttyACM1" }
```

Env overrides: `MESHGW_MC_PORT`, `MESHGW_MT_PORT`.

## 2. `mesh-relay/mqtt_bridge.json` (MQTT legs)

```json
{
  "brokers": [
    {
      "name": "local",            // label, used in logs/client id
      "host": "127.0.0.1",
      "port": 1883,
      "username": "", "password": "",
      "tls": false,
      "mode": "both",             // in | out | both
      "status": true,             // receive status publications?
      "publish_topic": "meshcore/mynode",
      "subscribe_topic": "meshcore/mynode",
      "status_topic": "meshcore/status"
    }
  ]
}
```

- `mode`: `in` = subscribe-only (use for community/public brokers);
  `out` = publish only; `both` = full.
- Injection rule: inbound MQTT can only inject single-network sends
  (`mc`/`mt`). `both`/unknown values are rejected so a public broker can
  never trigger a two-radio broadcast.
- Secrets: this file contains broker passwords — keep it readable only by
  the service user (`chmod 600`).

## 3. `rns-bridge/rns_bridge.json` (RNS leg)

```json
{
  "announce_app": "meshbridge",
  "announce_name": "bridge",
  "reannounce_interval_s": 60,
  "announce_enabled": true,
  "lxmf_enabled": true
}
```

- `announce_app`/`announce_name` = the RNS destination aspects. They must
  match what clients use (the test sender reads this file automatically).
- The destination hash is stable across restarts thanks to the persistent
  identity at `~/.reticulum/meshbridge_identity` — read it from the bridge
  log after first start and share it with clients.
- `lxmf_enabled` registers an LXMF delivery identity (Sideband-compatible).

## 4. `rns-bridge/state/rns_peer_tracker.json`

```json
{ "enabled": true, "period_value": 1, "period_unit": "days" }
```

`period_unit`: `seconds | minutes | hours | days`. Changes apply on the next
daemon loop (no restart needed). `--check` consumes a pending notice once;
`--daily` emails a digest (silent when no new peers).

Env: `RNS_TRACKER_EMAIL`, `RNS_TRACKER_EMAIL_FROM`, `RNS_TRACKER_FROM_ADDR`,
`RNS_TRACKER_TZ` (default UTC).

## 5. `lora-dashboard/config/dashboard.json`

Created on first run. Contents:

- `secret_key` — Flask session key (auto-generated).
- `devices` — saved device list (added from the Devices tab).
- `scan` — scan settings: `lan`, `tailnet`, `advertised`, `custom_ranges`,
  `ports` (default `[5000, 4403]`), `timeout_ms` (default 400),
  `auto_interval_min` (0 = off).
- `login_attempts.json` — rate-limit state (auto).

Env overrides: `LORA_DASH_PORT` (default 8452), `MESH_RELAY_DIR`,
`RNS_BRIDGE_DIR`, `MESH_RELAY_VENV`, `RNS_VENV`, `LAN_SUBNET`
(default `192.168.1.0/24`).

## 6. `~/.reticulum/config` (Reticulum transport)

Interfaces (KISS for the RNode; optional TCP server/client for network
peers). See `docs/SETUP.md` §6. Pin `prefer_ipv6 = False` on TCP client
interfaces if you see "Network is unreachable" reconnect loops.

## 7. Environment variables (all components)

| Variable | Default | Used by |
|---|---|---|
| `MESHGW_MC_PORT` / `MESHGW_MT_PORT` | auto | gateway, tools |
| `MESHGW_BRIDGE` | `1` | gateway (MT⇄MC auto-bridge on/off) |
| `MESHGW_HTTP_PORT` | `8082` | gateway status endpoint (0 disables) |
| `MESHGW_WATCH_ADVERTS` | (empty) | gateway (advert names to pin once seen) |
| `MESH_LIVE_PORT` | `8083` | mesh live feed |
| `LORA_DASH_PORT` | `8452` | dashboard |
| `LAN_SUBNET` | `192.168.1.0/24` | dashboard scanner |
| `RNSTATUS_BIN` | auto | rns status server |
| `RNS_TRACKER_*` | see §4 | peer tracker |
| `RNS_DAILY_NOEMAIL` | — | peer tracker (suppress email in a run) |

## 8. Tunables in code (constants)

| Where | Constant | Meaning |
|---|---|---|
| `mesh_gateway.py` | `LOOP_WINDOW_S` (3600) | dedup reject window |
| | `HOLD_S` (3600) | entry retention — **must stay ≥ LOOP_WINDOW_S** |
| | `MAX_TRACKED` (120) | bounded dedup entries |
| | `BRIDGE_PREFIX` (`[MT<>MC]`) | tag on relayed messages (loop marker) |
| | `CONTACTS_TTL_DAYS` (90) | contact registry idle expiry |
| `mqtt_bridge.py` | `HOLD_S` / `INJECT_S` (3600/300) | dedup + radio-echo suppression |
| `mesh_live.py` | `HISTORY` (300) | in-memory feed size |
| `rns_bridge.py` | target bytes `0x01–0x05` | RNS payload routing protocol |

## 9. Security-relevant files

| File | Notes |
|---|---|
| `~/.reticulum/meshbridge_identity` | RNS private key — host only |
| `~/.reticulum/lxmf/` | LXMF router storage |
| `mesh-relay/mqtt_bridge.json` | broker passwords — `chmod 600` |
| `lora-dashboard/config/dashboard.json` | session key + devices |
| `lora-dashboard/audit.log` | login + action audit trail |
