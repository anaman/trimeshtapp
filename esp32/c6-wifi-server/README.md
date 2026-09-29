# ESP32-C6 WiFi Mesh Server

Uses the ESP32-C6 DevKitC-1 (no LoRa radio) as a WiFi-side hub that connects to
Heltec V3 (ESP32-S3 + SX1262) MeshCore nodes over the local network, receives
mesh frames via TCP, and relays them to MQTT upstream.

## Architecture

See `DESIGN.md` for the full design document.

```
[Heltec V3 nodes] ──WiFi──> [ESP32-C6] ──MQTT──> [Linux host bridge]
     TCP :5000              hub/server            mqtt_bridge.py
```

## Quick Start

### 1. Configure

Edit `platformio.ini` and set:
- `WIFI_SSID` / `WIFI_PWD` — local WiFi credentials (2.4 GHz only)
- `MQTT_BROKER` — `mqtt.example.com`, port `8883` with `MQTT_TLS=1`
- `MQTT_USER` / `MQTT_PASS` — broker credentials (use your own; never commit secrets)
- `NODE_ADDRESSES` — optional static fallback; leave `""` to rely on mDNS

### Node discovery (mDNS)

V3 nodes advertise their WiFi mesh interface as `_meshcore._tcp`. The C6
queries for that service every 60 s and connects automatically. The V3
firmware must be built with `-D MDNS_ADVERTISE=1` (see `v3-wifi-env.ini`).

### TLS

The broker connection uses TLS with **ISRG Root X2** pinned
(`src/mqtt_ca.h`, extracted from the live `mqtt.example.com` chain).
Add `-D MQTT_TLS_INSECURE=1` to skip verification (not recommended).

### 2. Build & Flash

```bash
cd esp32/c6-wifi-server
pio run -e c6-wifi-server -t upload
pio device monitor -e c6-wifi-server
```

### 3. V3 Node Firmware

Re-flash your Heltec V3 nodes with MeshCore `companion_radio` firmware
with WiFi enabled. Add these build flags to the V3's platformio.ini:

```ini
-D WIFI_SSID='"YOUR_SSID"'
-D WIFI_PWD='"YOUR_PASSWORD"'
-D TCP_PORT=5000
```

See `DESIGN.md` §5 for the full V3 environment config.

### 4. Verify

- C6 status page: `http://c6-mesh-server.local/`
- JSON status: `http://c6-mesh-server.local/status`
- MQTT frames: `mosquitto_sub -t 'mesh/c6/frames/#' -v`

## Status Endpoints

| Endpoint   | Description                              |
|------------|------------------------------------------|
| `/`        | HTML dashboard (auto-refresh, node list) |
| `/status`  | JSON: wifi, mqtt, nodes, uptime, frames  |
| `/health`  | `{"ok":true}` liveness                   |
| `/nodes`   | JSON array of connected V3 nodes          |
| `/frames`  | Buffered mesh frames (JSON, `?since=SEQ`) |

## MQTT Schema

Each mesh frame from a V3 node is published as JSON:

```json
{
  "source": "192.168.1.50",
  "direction": "outbound",
  "frame_len": 42,
  "frame_b64": "Pi8AMi3x...",
  "ts": 1725724800
}
```

Topic: `mesh/c6/frames/{node_ip}`

## Files

| File               | Purpose                          |
|--------------------|----------------------------------|
| `platformio.ini`   | Build config                     |
| `src/config.h`     | Compile-time settings            |
| `src/main.cpp`     | Entry point, module orchestration|
| `src/wifi_client.cpp`   | WiFi station + reconnect    |
| `src/mesh_client.cpp`   | TCP client to V3 nodes      |
| `src/mqtt_relay.cpp`    | MQTT publish of frames     |
| `src/status_server.cpp` | HTTP status endpoint       |
| `DESIGN.md`        | Full architecture document       |
