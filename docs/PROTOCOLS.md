# Protocols

Wire-level reference for everything the bridge speaks.

## 1. RNS payload protocol (Reticulum → bridge)

Packets sent to the bridge destination carry **one byte of routing**, then
UTF-8 text:

```
┌──────────┬──────────────────────────┐
│ byte 0   │ UTF-8 text               │
│ target   │ message body             │
└──────────┴──────────────────────────┘
```

| byte 0 | meaning |
|---|---|
| `0x01` | deliver text over **MeshCore** |
| `0x02` | deliver text over **Meshtastic** |
| `0x03` | deliver text over **both** networks |
| `0x04` | status/echo request (logged; no delivery) |
| `0x05` | direct-RNS user message (relay to peers) |

Any other value is ignored (logged). Empty payloads ignored.

**Mesh → RNS** deliveries arrive at the most recently announced RNS peer as:

```
byte 0 (0x01 MC-origin / 0x02 MT-origin) + "MC:<sender>: <text>"
```

If no RNS peer is known, mesh→RNS packets are held/logged — never looped
back into the mesh.

## 2. LXMF (Sideband) routing

The bridge is an LXMF node (delivery identity registered). Message bodies
use explicit prefixes:

| Prefix | Route |
|---|---|
| `MC: …` | MeshCore |
| `MT: …` | Meshtastic |
| `BOTH: …` | both radios |
| *(none)* | **chat-only** — delivered to the channel agent, never transmitted on any radio and never re-propagated |

The unprefixed default exists because public bridge addresses attract
bot/automation traffic; only messages with an explicit prefix are allowed
to touch the radios.

## 3. MeshCore RS232 bridge framing (verified from MeshCore source)

The `Heltec_v3_repeater_bridge_rs232` env turns a MeshCore repeater into one
that carries mesh packets over a wired UART (Serial2, RX=5/TX=6, 3.3 V):

```
┌──────────┬──────────────┬───────────────────────┬──────────────┐
│ MAGIC    │ LENGTH (BE)  │ PAYLOAD               │ CRC (BE)     │
│ 0xC0 0x3E│ len_hi len_lo│ serialized mesh packet│ Fletcher-16  │
└──────────┴──────────────┴───────────────────────┴──────────────┘
```

- Resync on invalid bytes; reject `len > 256`.
- Payload = MeshCore `mesh::Packet` serialization (encrypted — the host
  transports without decrypting).
- Fletcher-16: `sum1=(sum1+b)%255; sum2=(sum2+sum1)%255; crc=(sum2<<8)|sum1`.
- Dedup + `bridge_delay` re-injection prevent echo loops on the radio side.
- Max frame 262 bytes.

Cross-network caveat: MeshCore and Meshtastic are different modulation
profiles *and* different encrypted protocols. Raw bridge frames injected
into the other network are noise. Cross-network messaging happens at the
message layer (this stack), not by packet forwarding.

## 4. MeshCore WiFi interface (`SerialWifiInterface`)

Used by the optional ESP32-C6 hub. The node listens on TCP `:5000` and
frames both directions:

```
Outbound (node → client):  '>' (0x3e) + 16-bit LE length + payload
Inbound  (client → node):  '<' (0x3c) + 16-bit LE length + payload
```

One client per node at a time. Payloads are encrypted mesh packets. V3
nodes advertise `_meshcore._tcp` via mDNS when built with
`-D MDNS_ADVERTISE=1`.

## 5. MeshCore companion protocol (USB)

The production MeshCore leg uses the **companion radio USB** role and the
`meshcore` python library:

- handshake/appstart required (raw serial writes get no reply);
- channel messages are anonymous — identity rides in a text prefix such as
  `N0CALL: message` (the bridge extracts that prefix as the display name);
- DMs arrive with a pubkey prefix; replies go to the recorded peer.

## 6. Meshtastic interface (USB/TCP)

- Library/CLI: `meshtastic` python package. Over TCP: `--host host:4403`.
- Read: `meshtastic --export-config yaml`.
- Write: `meshtastic --begin-edit --set lora.… --set mqtt.… --commit-edit`.
- DM vs channel: broadcast packets have `toId="^all"` / `to=0xFFFFFFFF`.
- Radio presets: reference is LongFast for the region.

## 7. RNode / Reticulum leg

- RNode firmware = transparent KISS modem; radio params in EEPROM, set via
  `rnodeconf` (`--freq/--bw/--sf/--cr/--txp`; `-i` info; `-t` display
  blanking — **run `-t` alone**, never combined with `--config`).
- RNS uses a plain `KISSInterface` to the RNode; `rnsd` owns the port.
- Writing EEPROM requires stopping `rnsd` (it holds the port); restart after.

## 8. Loop prevention model (message layer)

Three independent guards, all needed in practice:

1. **Radio-side** (MeshCore): `_seen_packets` + `bridge_delay` re-injection.
2. **Gateway-side**: `MessageDedup` — global (sender, text) key, 1-hour
   window, persistent across restarts, bounded size. Stops MT⇄MC relay
   loops and double-delivery.
3. **MQTT-side**: published/received/injected key sets, plus a 300 s
   radio-echo suppression window, so the bridge never re-publishes its own
   transmissions or double-injects across brokers.

Relayed messages carry a `[MT<>MC]` prefix; anything carrying that prefix is
never re-relayed.
