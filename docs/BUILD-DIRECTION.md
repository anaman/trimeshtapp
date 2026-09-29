# MeshCore–Meshtastic–RNode Gateway Build Direction

The strongest direction is to **build the gateway intelligence on Linux first and keep the radios as native protocol endpoints**. That gets you a working MeshCore↔Meshtastic↔RNode architecture much sooner, while avoiding the risk of burying routing logic inside custom ESP32 firmware before you know exactly what the gateway needs.

One small housekeeping point first: the snippet shows **four added build defines**, not five. More importantly, I would stop describing the current build as simply a “correct US build.” `910.525 MHz + SF7` is proven at the PHY level, but field tests indicated that the complete network profile includes additional region/transport information that is still missing.

## Recommended architecture

```text
                    ┌───────────────────────────┐
                    │       Client Layer        │
                    │ Android / Web / T-Deck    │
                    │ map · inbox · diagnostics │
                    └─────────────┬─────────────┘
                                  │
                         WebSocket / local API
                                  │
                    ┌─────────────▼─────────────┐
                    │       Gateway Broker      │
                    │                           │
                    │ canonical message model   │
                    │ routing policy            │
                    │ deduplication              │
                    │ contact/node database      │
                    │ link metrics               │
                    │ store-and-forward          │
                    │ diagnostics                │
                    └──────┬────────┬───────────┘
                           │        │
              ┌────────────┘        └──────────────┐
              │                                    │
     ┌────────▼────────┐                 ┌─────────▼─────────┐
     │ MeshCore Adapter│                 │Meshtastic Adapter │
     │ native serial   │                 │ native protobuf   │
     └────────┬────────┘                 └─────────┬─────────┘
              │                                    │
          Heltec V3                           Meshtastic radio
              │
              │                    ┌─────────────────────────┐
              └────────────────────│ later: RNode Adapter    │
                                   │ native RNode/Reticulum  │
                                   └─────────────────────────┘
```

I would **not write the ESP32 dual-radio converter yet**.

You already have something more valuable: native MeshCore and Meshtastic interfaces that Linux can talk to directly. Build the broker using those first. Once that works reliably, the embedded converter becomes an optimization/packaging project rather than the place where the architecture is invented.

## Build order I recommend

| Phase | Objective | Exit criterion |
|---|---|---|
| **0 — Freeze current state** | Preserve everything you've proven | reproducible V3 build + logs + hashes |
| **1 — Region unlock** | Clone the actual regional network parameters | The MeshCore node and V3 exchange accepted packets |
| **2 — MeshCore adapter** | Turn your probe scripts into a reusable service | continuous RX/TX/contact events |
| **3 — Meshtastic adapter** | Add native Meshtastic serial/protobuf | both radios simultaneously visible |
| **4 — Gateway broker** | Normalize both protocols | one event stream/database |
| **5 — Diagnostics engine** | Explain links and rejected packets | gateway tells you *why* communication fails |
| **6 — Link-map client** | Build the flagship UI | live nodes/links/RSSI/SNR |
| **7 — Unified inbox** | Cross-network messaging UI | MeshCore + Meshtastic conversations |
| **8 — RNode/Reticulum** | Add third network family | RNS destinations appear in broker |
| **9 — Routing policy** | Controlled inter-network forwarding | no loops/flooding |
| **10 — Embedded bridge** | Package into ESP32/portable appliance | Linux-less/simplified deployment where needed |

### Phase 0 matters more than it sounds

Before changing the V3 again, create something like:

```text
lab-notes/
└── known-good-2026-08-13/
    ├── firmware.bin
    ├── firmware.sha256
    ├── platformio.ini
    ├── armaros-rx-test.log
    ├── passive-listen.log
    ├── radio-self-info.json
    └── README.md
```

Tag the source as well:

```bash
git tag rf-rx-proven
```

The critical evidence is:

```text
The MeshCore node RF TX
      ↓
V3 PHY demodulation successful
      ↓
MeshCore Dispatcher
      ↓
FLOOD rejected due to transport/region handling
```

Don't lose that baseline while fixing the next problem.

## Phase 1: make region configuration a first-class component

This is now the highest-priority engineering task.

Don't bake another region-specific number into `platformio.ini` and call it solved.

Instead move toward:

```text
profiles/
├── region.meshprofile
├── meshcore-default-us.meshprofile
└── lab-direct.meshprofile
```

Conceptually:

```yaml
name: Region
protocol: meshcore

radio:
  frequency_mhz: 910.525
  spreading_factor: 7
  bandwidth: ...
  coding_rate: ...
  tx_power: ...

mesh:
  region_definition: ...
  transport_codes:
    - ...
  default_scope: ...

metadata:
  source: armaros
  created: 2026-08-13
```

Then your diagnostic software can say:

```text
LOCAL PROFILE
Region-Test

OBSERVED PACKET
PHY compatible:       YES
Transport recognized: NO
Region recognized:    NO

Likely profile mismatch.
```

That's much better than another compilation cycle.

## Your existing Python tools should become one package

Right now you have several excellent diagnostic scripts:

```text
probe_meshcore.py
probe_neighbors.py
probe_regions.py
watch_contacts.py
cli-query.py
cli-power.py
cli-tx.py
passive-listen.py
```

Don't keep growing these independently.

Refactor toward:

```text
gateway/
├── meshcore/
│   ├── adapter.py
│   ├── console.py
│   ├── contacts.py
│   ├── regions.py
│   └── diagnostics.py
├── meshtastic/
│   └── adapter.py
├── rnode/
│   └── adapter.py
├── broker/
│   ├── events.py
│   ├── routing.py
│   ├── dedup.py
│   └── database.py
└── cli.py
```

Then your commands become:

```bash
gatewayctl meshcore info
gatewayctl meshcore contacts
gatewayctl meshcore regions
gatewayctl meshcore listen
gatewayctl meshcore advert

gatewayctl meshtastic nodes

gatewayctl status
```

That gives you a real operational interface instead of a collection of one-off probes.

## Define the normalized packet model early

This is probably the most important software-design decision after resolving region unlock.

Do **not** translate MeshCore into Meshtastic objects or vice versa.

Translate both into your own neutral representation.

Something approximately like:

```python
@dataclass
class MeshEvent:
    event_id: str

    network: str
    interface: str

    source: str | None
    destination: str | None

    event_type: str

    rssi: float | None
    snr: float | None
    hops: int | None

    timestamp: float

    payload: bytes | None

    native_packet_id: str | None
    native_metadata: dict

    gateway_origin: str
    bridge_hops: int
```

Preserve `native_metadata`.

That's important because otherwise six months from now you'll discover a useful MeshCore property that your generic model discarded.

The rule should be:

```text
Native protocol
      ↓
lossless-ish adapter
      ↓
canonical metadata
      +
native metadata
```

not:

```text
Native packet
      ↓
lowest common denominator
```

## SQLite is enough for the first gateway

I would resist adding a large backend stack.

For the first implementation:

```text
Python asyncio
    +
SQLite
    +
WebSocket
    +
small REST API
```

is more than sufficient.

Tables could eventually represent:

```text
nodes
interfaces
links
packets
messages
contacts
region_profiles
routing_events
rejections
```

Then your LoRa map is simply querying:

```text
links seen during last N minutes
```

with:

```text
source
destination
protocol
RSSI
SNR
hops
last_seen
packet_count
```

That creates the foundation for the UI you wanted.

## Make diagnostics part of the broker, not debug logging

This experience points to a very useful design.

Instead of storing:

```text
allowPacketForward: unknown transport code...
```

only in a console log, normalize it:

```json
{
  "type": "packet_rejected",
  "network": "meshcore",
  "stage": "forwarding",
  "reason": "unknown_transport",
  "rssi": -63,
  "phy_decode": true
}
```

Then the client can display:

```text
⚠ V3 hears the MeshCore node

RF link: GOOD
RSSI: -63 dBm
PHY decode: successful

Packet forwarding:
REJECTED

Reason:
Unknown transport/region code

Recommended action:
Compare local MeshCore region profile with the MeshCore node.
```

That is a **real product feature born directly from the failure you just diagnosed**.

## Your RS232 framing work is still valuable

I would absolutely keep:

```text
rs232-bridge-protocol.md
```

But treat it as an **adapter transport specification**, rather than the core architecture.

Your verified:

```text
C0 3E
LEN
PAYLOAD
FLETCHER16
```

can later support:

```text
MeshCore radio
       │ UART
       ▼
 ESP32 appliance
       │
USB / BLE / network
       ▼
 Gateway Broker
```

The broker shouldn't care whether that MeshCore packet came through:

```text
USB serial
RS232 framing
BLE
TCP
```

That's precisely why the adapter boundary is valuable.

## There is one thing I would add to the framing specification now

Design explicit recovery behavior for malformed streams.

For example:

```text
search C0 3E
   ↓
read length
   ↓
length > maximum?
   └─ discard + resync

read payload
read checksum
   ↓
checksum invalid?
   └─ increment rx_crc_error
      resync at next C0 3E
```

And record counters:

```text
frames_rx
frames_valid
checksum_errors
length_errors
resync_events
duplicate_frames
```

Those counters should eventually appear in the gateway UI.

## Anti-loop needs two levels

Your existing dedup design is a good start, but the cross-network bridge will need more.

Use both:

```text
Protocol deduplication
```

and:

```text
Gateway bridge deduplication
```

because this topology will eventually exist:

```text
        Gateway A
       /          MeshCore       RNS
    |             |
    |             |
 Gateway B────────┘
```

A bridged message should carry internal metadata such as:

```text
bridge_id
origin_gateway
origin_network
bridge_hops
ttl
first_seen
```

The internal metadata does **not** necessarily have to go over every RF network. The gateways can maintain it locally where necessary.

I'd also avoid using only:

```text
SHA256(payload)
```

as the message identity.

Two people can legitimately send:

```text
OK
```

at the same time.

Build identity from something closer to:

```text
protocol
source
native packet/message ID
timestamp bucket
payload hash
```

when native IDs exist.

## What I would deliberately NOT build yet

This will save you a lot of work.

**BLE API:** postpone it.

**OTA over LoRa:** postpone it.

**Automatic profile scanning/transmitting:** postpone active transmission; passive observation is useful.

**Telegram/WhatsApp relay:** keep the existing relay separate until gateway routing is solid.

**Automatic protocol bridging:** initially require explicit rules.

**Custom Android app:** don't begin with native Android.

Build the first visualization as a local web UI:

```text
http://gateway.local/
```

Once the information model feels right, wrap or reproduce it on Android.

Otherwise you'll be debugging:

```text
RF
+
MeshCore
+
Meshtastic
+
gateway
+
Android lifecycle
+
BLE
```

simultaneously.

That's exactly the kind of integration explosion you want to avoid.

## I would also separate `meshcore-src` from your project logic

Your small upstream modification is a healthy sign.

Keep it that way.

Ideally:

```text
third_party/
└── MeshCore/
```

and:

```text
firmware-config/
└── heltec-v3-region/
```

Your project should contain a patch or build overlay rather than gradually turning MeshCore into a fork full of gateway functionality.

Something like:

```bash
patches/
└── meshcore/
    └── 0001-heltec-v3-region-profile.patch
```

Eventually the goal should be:

```text
upstream MeshCore
+
your gateway configuration
```

rather than:

```text
an increasingly divergent MeshCore fork
```

That will save substantial maintenance work.

## The immediate milestone

I would define your next success condition very narrowly:

```text
ARMAROS
   ⇅
  RF
   ⇅
HELTEC V3
   ⇅
USB
   ⇅
meshcore-adapter
   ⇅
gateway broker
```

And the broker should show:

```text
Node: mc-node
Protocol: MeshCore
Status: reachable
Last heard: 3 sec
RSSI: ...
SNR: ...
Profile: Region
Packets accepted: ...
Packets rejected: ...
```

**No Meshtastic bridging yet.**

Once that is stable, add the Meshtastic radio beside it:

```text
MeshCore ─────┐
              │
              ▼
            BROKER
              ▲
              │
Meshtastic ───┘
```

Then make both appear on the same diagnostic page **before forwarding a single message between them**.

That separation—**observe first, route second**—is the architectural principle I would carry through the entire project.

The project has now moved beyond “can an ESP32 translate two serial protocols?” The more useful system you're positioned to build is a **protocol-independent local mesh operations layer** where MeshCore, Meshtastic and Reticulum remain themselves, while one gateway can observe, diagnose, store, visualize and—only under explicit policy—bridge between them.
