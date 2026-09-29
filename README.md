# LoRa Bridge Server — MeshCore · Meshtastic · Reticulum

A Linux-hosted bridge that merges **three LoRa mesh networks** into one
system, with a **web dashboard** for control and a file-based integration
layer that lets an agent (LLM or otherwise) relay messages to chat channels
and back.

```
 MeshCore  ─┐
 Meshtastic ─┼─►  mesh gateway  ─►  event files  ─►  channel relay (Telegram/WhatsApp)
 Reticulum ─┘         │                │
 (RNode)              │                └─►  RNS bridge  ─►  Reticulum/LXMF peers
                      └─►  web dashboard (status, settings, device scanner)
```

Nothing about the individual networks is replaced: each radio keeps its own
protocol and encryption. The bridge *observes* all three, relays messages
between them (with loop prevention), and exposes everything through one
control panel.

## What's inside

| Component | Path | Purpose |
|---|---|---|
| **Mesh gateway** | `mesh-relay/mesh_gateway.py` | Owns the MeshCore + Meshtastic radios; drains both to an event file; outbound via a send queue |
| **Channel watcher** | `mesh-relay/mesh_watch.py` | 5s-cadence watcher that turns events into chat notifications + routes replies to radios |
| **MQTT bridge** | `mesh-relay/mqtt_bridge.py` | Multi-broker MQTT leg (local status + community brokers) |
| **Mesh Live** | `mesh-relay/mesh_live.py` | Lightweight SSE web feed of MeshCore+Meshtastic traffic |
| **RNS bridge** | `rns-bridge/rns_bridge.py` | Reticulum leg: destination announcements, packet ⇄ mesh routing, LXMF (Sideband) support |
| **Peer tracker** | `rns-bridge/rns_peer_tracker.py` | SQLite-backed unique-peer counting + throttled notices |
| **Propagation node** | `rns-bridge/rns_propagation.py` | LXMF propagation node for stamp minting |
| **Web dashboard** | `lora-dashboard/` | Single-pane control panel: services, bridge settings, radio protocol panels, device scanner |
| **ESP32 WiFi hub** | `esp32/c6-wifi-server/` | ESP32-C6 firmware: WiFi-side hub linking Heltec nodes over TCP → MQTT |
| **Skills** | `skills/` | Operational playbooks (agent-ready) for setup and debugging |
| **Docs** | `docs/` | Architecture, setup, configuration, protocols, operations, changelog, LLM guide |

## Quick start

Full walkthrough: **[docs/SETUP.md](docs/SETUP.md)**.

```text
1. Install repo at /opt/mesh-bridge (or adjust paths everywhere)
2. Create two venvs: mesh-relay/.venv (relay + dashboard deps)
                     rns-bridge/.venv (rns, lxmf)
3. Flash/provide three radios:
     - MeshCore companion radio (USB)
     - Meshtastic radio (USB)
     - Heltec V3 running RNode firmware (USB, Reticulum/KISS)
4. python3 mesh-relay/mesh_relay_setup.py scan      # discover ports
   python3 mesh-relay/mesh_relay_setup.py apply --yes
5. Install systemd units (systemd/), start services
6. Open the dashboard, sign in with your mosquitto credentials
```

The dashboard runs on `127.0.0.1:8452` by default — put it behind your
favorite reverse proxy or VPN. The status endpoints are:
`:8082` (mesh gateway), `:8451` (RNS leg), `:8452` (dashboard).

## Using this repo with an LLM

Everything here is designed to be handed to a capable LLM/agent:

1. Give it the repo (or point it at this GitHub project).
2. Start with **[docs/LLM-GUIDE.md](docs/LLM-GUIDE.md)** — it contains the
   repo map, key invariants, suggested prompts, and guardrails.
3. The `skills/` directory contains task-specific playbooks the agent can
   follow for setup, debugging, and recovery.

## Security model (summary)

- Dashboard login verifies against the local **mosquitto** MQTT credentials —
  no separate password store. Rate-limited; CSRF-protected; audit log.
- Service restarts go through a `sudo` allowlist (strict unit list).
- The gateway is the **single owner** of each serial port; all sends go
  through its queue file. No second connections.
- Bridge identity files are private keys — keep them on the host; never
  commit them (`.gitignore` covers common state paths).
- The RNS bridge is intentionally unauthenticated at the mesh level (LoRa is
  a shared medium) — see `docs/PROTOCOLS.md` and the skills for the
  Telegram-only routing default for LXMF bot traffic.

## License

**GPL-3.0** — see [LICENSE](LICENSE).

Third-party notes: integrates with
[MeshCore](https://github.com/meshcore-dev/MeshCore) (MIT),
[Meshtastic](https://github.com/meshtastic) (GPL-3.0),
[Reticulum](https://github.com/markqvist/Reticulum) / LXMF (Reticulum
License). Those projects are not vendored here; install them from upstream.
