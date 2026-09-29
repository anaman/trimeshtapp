# MeshCore RS232 Bridge — Protocol Spec & Phase 1 Integration Notes

> Verified directly from MeshCore source v1.17.0 (`meshcore-src/`), 2026-08-13. Files: `src/helpers/bridges/RS232Bridge.{h,cpp}`, `src/helpers/bridges/BridgeBase.{h,cpp}`, `src/MeshCore.h`.

## 1. What it is

`Heltec_v3_repeater_bridge_rs232` = MeshCore repeater that also carries mesh packets over a wired UART (Serial2 on the V3, pins **RX=5 / TX=6**, 3.3 V). Bidirectional: radio→serial (node pushes received mesh packets out the UART) and serial→radio (host-injected packets get queued for RF transmission). It exists so a MeshCore radio can be attached to a host over a wire (point-to-point link).

## 2. Wire protocol (exact bytes)

```
┌──────────┬──────────────┬───────────────────────┬──────────────┐
│ MAGIC    │ LENGTH (BE)  │ PAYLOAD               │ CRC (BE)     │
│ 2 bytes  │ 2 bytes      │ len bytes             │ 2 bytes      │
│ 0xC0 0x3E│ len_hi len_lo│ serialized mesh packet│ fletcher-16  │
└──────────┴──────────────┴───────────────────────┴──────────────┘
```
- **Magic:** `0xC03E` (bytes `C0 3E`) — frame sync. RX state machine resyncs on invalid bytes (handles a corrupt magic mid-stream).
- **Length:** uint16 big-endian = payload length. RX rejects `len > MAX_TRANS_UNIT+1` (= 256) and resyncs.
- **Payload:** MeshCore `mesh::Packet` serialization (`Packet::writeTo` / `readFrom`) — this is the **encrypted** mesh packet (network-key crypto). A host does NOT need to decrypt to transport it.
- **CRC:** Fletcher-16 over payload only: `sum1=(sum1+b)%255; sum2=(sum2+sum1)%255; crc=(sum2<<8)|sum1`, sent big-endian.
- **Max frame:** 262 bytes (255+1 payload + 6 overhead). `MAX_TRANS_UNIT = 255` (src/MeshCore.h:23).
- **Baud:** from node prefs `bridge_baud` (default 115200 per header comment "fixed baud rate at 115200"); RX/TX pins from build defines `WITH_RS232_BRIDGE_RX/TX`.
- **Dedup on TX:** `sendPacket` drops packets already in `_seen_packets` (SimpleMeshTables) — prevents loops.
- **Dedup on RX:** `handleReceivedPacket` checks `_seen_packets`, then `queueInbound(pkt, millis()+bridge_delay)` — `bridge_delay` pref buffers re-injection so the radio doesn't immediately re-TX what it just received (loop prevention).
- **Debug:** `BRIDGE_DEBUG_PRINTLN` — enable with `-D BRIDGE_DEBUG=1` build flag (currently commented out in the variant env).

## 3. Converter (host) design for Phase 1

```
[Heltec A: MeshCore repeater_bridge_rs232]   [Heltec B: Meshtastic]
        Serial2 TX=6 ──┐                 ┌── USB serial (protobuf stream)
        Serial2 RX=5 ──┤                 │
                       ▼                 ▼
              ┌─────────────────────────────────────┐
              │  converter host (extend mesh-relay) │
              │  - RS232 bridge parser (this spec)  │
              │  - Meshtastic serial/protobuf side  │
              │  - message-level gateway (keys!)    │
              │  - dedup (time-windowed) + metrics  │
              │  - unified BLE/API for clients      │
              └─────────────────────────────────────┘
```

### 3.1 Host ↔ Heltec A (RS232 bridge framing)
- Host needs a **3.3 V UART** (USB-UART adapter or T-Deck UART) — NOT the CP2102 USB port (that's the console).
- RX path: host reads frames, syncs on `C0 3E`, validates length ≤ 262 and Fletcher-16, forwards payload bytes to the gateway core.
- TX path: gateway core hands back MeshCore packet bytes → host frames them (`C0 3E len crc`) → Heltec A re-broadcasts over the MeshCore RF mesh.
- No crypto needed in the host for *transport*; crypto only if the host must *construct* MeshCore packets (then use meshcore.js / meshcore-cli patterns or the companion protocol).

### 3.2 The honest cross-network picture
MeshCore and Meshtastic are **different modulation profiles AND different encrypted protocols**. Raw bridge frames injected into the Meshtastic channel are noise to Meshtastic nodes. Cross-network **messaging** must happen at the message layer in the converter — i.e. a gateway node that participates in both networks (both keys/configs) and re-encodes, exactly like the existing `mesh-relay/mesh_gateway.py` (Telegram/WhatsApp pattern). The RS232 bridge gives that gateway a **wired, remote radio leg** — Heltec A can live on a roof/antenna, converter in a closet.

### 3.3 What the lab must still measure
- Actual baud as flashed (confirm 115200 vs pref default) + `bridge_delay` pref behavior.
- Largest observed payload (telemetry vs chat) — confirm 262-byte ceiling holds.
- Whether the node console (CP2102) also echoes bridge traffic when `BRIDGE_DEBUG` off (expected: no).
- Boot log visibility: MeshCore repeater printed nothing on USB console at 115200 after flash — confirm via OLED or `help` CLI whether the console is alive (may need baud 921600 on some builds).

## 4. Source of truth

- `meshcore-src/src/helpers/bridges/RS232Bridge.cpp` (RX state machine, TX framing)
- `meshcore-src/src/helpers/bridges/BridgeBase.cpp` (Fletcher-16, dedup, queueInbound + bridge_delay)
- `meshcore-src/variants/heltec_v3/platformio.ini` (`WITH_RS232_BRIDGE=Serial2`, RX=5, TX=6)
- `meshcore-src/src/MeshCore.h` (`MAX_TRANS_UNIT 255`)
