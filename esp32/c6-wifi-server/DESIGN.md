# ESP32-C6 WiFi Mesh Server — Design Document

> Created 2026-09-07.  
> Project root: `esp32/c6-wifi-server/`

## 1. Goal

Use the ESP32-C6 DevKitC-1 (no LoRa radio) as a **WiFi-side hub/server** that:
- connects to Heltec V3 (ESP32-S3 + SX1262) LoRa nodes over the local WiFi network
- receives MeshCore mesh frames from each node via TCP
- relays the aggregated data upstream via MQTT to the existing bridge infrastructure
- serves a lightweight HTTP status endpoint for monitoring

## 2. Architecture

```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│  Heltec V3   │  │  Heltec V3   │  │  Heltec V3   │
│  ESP32-S3    │  │  ESP32-S3    │  │  ESP32-S3    │
│  + SX1262    │  │  + SX1262    │  │  + SX1262    │
│  MeshCore    │  │  MeshCore    │  │  MeshCore    │
│  + WiFi I/F  │  │  + WiFi I/F  │  │  + WiFi I/F  │
│  TCP :5000   │  │  TCP :5000   │  │  TCP :5000   │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                 │
       └────── WiFi LAN (2.4 GHz) ─────────┘
                         │
              ┌──────────▼──────────┐
              │   ESP32-C6 DevKitC   │
              │                     │
              │  WiFi station        │
              │  TCP client × N      │
              │  Mesh frame parser   │
              │  MQTT client         │
              │  HTTP status server  │
              │  mDNS broadcast      │
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────┐
              │  Linux host (bridge) │
              │  MQTT broker :1883  │
              │  mqtt_bridge.py      │
              │  rns_bridge.py       │
              │  lora-dashboard      │
              └─────────────────────┘
```

## 3. MeshCore WiFi Interface (existing)

Source: `meshcore-src/src/helpers/esp32/SerialWifiInterface.cpp`

The V3 nodes already have the code. To enable it, add build flags to the
MeshCore companion_radio firmware:

```
-D WIFI_SSID='"YOUR_SSID"'
-D WIFI_PWD='"YOUR_PASSWORD"'
-D TCP_PORT=5000
```

This makes the V3 node:
- connect to WiFi as a station
- listen on TCP port 5000
- accept one client connection at a time
- serve mesh frames with a simple framing protocol:
  - Outbound (node → client): `>` (0x3e) + 16-bit LE length + payload
  - Inbound (client → node): `<` (0x3c) + 16-bit LE length + payload
- payloads are MeshCore encrypted mesh packets (the host doesn't decrypt)

## 4. C6 Firmware Design

### 4.1 Framework
- PlatformIO + Arduino framework (ESP32-C6 support via pioarduino platform)
- ESP-IDF under the hood (WiFi 6, LWIP, mDNS)

### 4.2 Modules

```
c6-wifi-server/
├── platformio.ini          # build config
├── src/
│   ├── main.cpp            # entry point, task orchestration
│   ├── wifi_client.cpp     # WiFi station connect + reconnect
│   ├── mesh_client.cpp     # TCP client to V3 nodes, frame parser
│   ├── mqtt_relay.cpp      # MQTT publish of aggregated frames
│   ├── status_server.cpp   # HTTP status endpoint (port 80)
│   ├── mdns_broadcast.cpp  # mDNS service registration
│   └── config.h            # compile-time config (SSIDs, broker addr, node list)
├── DESIGN.md              # this file
└── README.md              # quick-start
```

### 4.3 Configuration

Compile-time via `config.h` or build flags:

| Setting           | Default              | Description                        |
|-------------------|----------------------|------------------------------------|
| `WIFI_SSID`       | —                    | Local network SSID                 |
| `WIFI_PWD`        | —                    | Local network password             |
| `MQTT_BROKER`     | `192.168.1.100`      | MQTT broker IP (the bridge host)      |
| `MQTT_PORT`       | `1883`               | MQTT broker port                    |
| `MQTT_TOPIC`      | `mesh/c6/frames`     | MQTT topic for relayed frames      |
| `NODE_ADDRESSES`  | —                    | Comma-separated V3 node IPs        |
| `NODE_PORT`       | `5000`               | MeshCore WiFi interface TCP port   |
| `STATUS_PORT`     | `80`                 | HTTP status server port            |
| `MDNS_NAME`       | `c6-mesh-server`    | mDNS service name                  |

### 4.4 Data Flow

```
V3 node (LoCa RX) → MeshCore dispatch → SerialWifiInterface → TCP frame
                                                          ↓
C6: TCP client reads frame → parse (> + len + payload)
                          → tag with source_node IP
                          → MQTT publish: mesh/c6/frames/{node_ip}
                          → (optional) HTTP SSE stream

C6: inbound MQTT command or HTTP POST → frame (< + len + payload)
                                       → TCP write to target V3 node
                                       → V3 node injects into mesh
```

### 4.5 MQTT Schema

Each frame from a V3 node is published as:

```json
{
  "source": "192.168.1.50",
  "direction": "outbound",
  "frame_len": 42,
  "frame_b64": "Pi8AMi3x...",
  "ts": 1725724800
}
```

The existing `mqtt_bridge.py` on the Linux host can subscribe and
integrate with the bridge software.

### 4.6 HTTP Status Endpoints

| Endpoint    | Description                              |
|-------------|------------------------------------------|
| `/`         | HTML dashboard (node list, link status)  |
| `/status`   | JSON status (nodes, uptime, frame count) |
| `/health`   | `{"ok":true}` liveness probe             |
| `/nodes`    | JSON array of connected V3 nodes         |

## 5. V3 Node Firmware Changes

Re-flash each Heltec V3 with the `companion_radio` MeshCore firmware
with WiFi enabled. New platformio.ini environment:

```ini
[env:Heltec_v3_companion_wifi]
extends = Heltec_lora32_v3
build_flags =
  ${Heltec_lora32_v3.build_flags}
  -D MAX_CONTACTS=350
  -D MAX_GROUP_CHANNELS=40
  -D BLE_PIN_CODE=123456
  -D OFFLINE_QUEUE_SIZE=256
  -D ENABLE_PRIVATE_KEY_IMPORT=1
  -D ENABLE_PRIVATE_KEY_EXPORT=1
  -D WIFI_SSID='"YOUR_SSID"'
  -D WIFI_PWD='"YOUR_PASSWORD"'
  -D TCP_PORT=5000
  -D WIFI_DEBUG_LOGGING=1
build_src_filter =
  ${Heltec_lora32_v3.build_src_filter}
  +<helpers/esp32/*.cpp>
  -<helpers/esp32/ESPNOWRadio.cpp>
  +<../examples/companion_radio/*.cpp>
  -<../examples/companion_radio/ui-tiny>
  -<../examples/companion_radio/ui-orig>
lib_deps =
  ${Heltec_lora32_v3.lib_deps}
  densaugeo/base64 @ ~1.4.0
```

## 6. Build Order

| Phase | Objective | Exit criterion |
|-------|-----------|----------------|
| 1 | C6 firmware: WiFi + status server | C6 visible on network at `c6-mesh-server.local` |
| 2 | C6 firmware: TCP mesh frame client | C6 connects to one V3 node and logs frames |
| 3 | V3 node: re-flash with WiFi interface | V3 node visible on WiFi, TCP 5000 accepting connections |
| 4 | C6 firmware: MQTT relay | Frames flowing to MQTT broker on Linux host |
| 5 | Integration: mqtt_bridge subscribes | End-to-end: V3 LoRa → C6 WiFi → MQTT → bridge → dashboard |
| 6 | Multi-node: add remaining V3 nodes | All nodes visible in C6 status dashboard |

## 7. Constraints & Notes

- ESP32-C6 has 512KB SRAM, 4MB flash (DevKitC-1 N8 variant) — plenty for this role
- C6 WiFi is 2.4 GHz WiFi 6 only (no 5 GHz) — match the local network
- SerialWifiInterface accepts ONE client at a time per V3 node — the C6 is the sole client
- MeshCore frames are encrypted; the C6 transports them without decryption
- C6 can also relay inbound commands (client → V3 → mesh) using the `<` frame type
- Power: C6 draws ~80mA active WiFi; suitable for USB or solar+battery deployment
- The existing USB serial bridge (mesh_gateway.py) can run in parallel for nodes that stay wired

## 8. Sources

- `meshcore-src/src/helpers/esp32/SerialWifiInterface.h/.cpp` — WiFi TCP frame protocol
- `meshcore-src/examples/companion_radio/main.cpp` — WiFi interface setup (lines 35-44, 194-210)
- `meshcore-src/variants/heltec_v3/platformio.ini` — V3 build config
- `meshcore-src/variants/xiao_c6/platformio.ini` — C6 variant reference
- `meshcore-src/src/helpers/bridges/RS232Bridge.cpp` — RS232 frame protocol (for reference)
- `rns-bridge/rns_bridge.py` — existing RNS bridge (file-based IPC pattern)
- `mesh-relay/mesh_gateway.py` — existing serial gateway (architecture reference)
