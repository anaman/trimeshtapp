# Using this repo with an LLM

This project is designed to be handed to a capable LLM/agent. Give it the
whole repo (or this GitHub project) and this guide, and it has everything
needed to set up, operate, debug, or extend the bridge.

## 1. How to hand it over

**Option A — repo + agent:** point your coding agent at the repository and
tell it to read `README.md`, `docs/LLM-GUIDE.md`, `docs/ARCHITECTURE.md`,
then the `skills/` playbooks relevant to the task.

**Option B — chat LLM:** paste this guide plus the files you're working on.
For setup, also paste `docs/SETUP.md` and the `systemd/` units. For
debugging, paste the relevant component source + the skill for the failing
subsystem.

A compact context block you can paste as a system prompt:

```text
Project: LoRa bridge server merging MeshCore, Meshtastic and Reticulum into
one Linux host, with a web dashboard. Core rules:
- mesh_gateway.py is the SINGLE owner of the MeshCore+Meshtastic serial
  ports. All outbound radio sends go through /tmp/meshgw_send.txt
  (helper: meshgw reply). Never open a second serial connection.
- rnsd owns the RNode port; EEPROM writers must stop rnsd first.
- IPC is file-based (see docs/ARCHITECTURE.md §3). Don't change file
  formats without updating every reader.
- Dedup guard: HOLD_S >= LOOP_WINDOW_S (currently 3600/3600, max 120).
  Never lower HOLD_S below the window.
- mesh-gateway.service has a 120s boot delay; restart blocks ~2 min.
  Never restart twice.
- RNS payload protocol: byte0 target 0x01 MC / 0x02 MT / 0x03 both /
  0x04 status / 0x05 direct-RNS; rest UTF-8.
- LXMF default route is chat-only; only MC:/MT:/BOTH: prefixes transmit.
- Dashboard auth = mosquitto MQTT credentials; service restarts go through
  the allowlist in app.py.
- Never commit identities/keys/passwords; state lives outside the repo.
```

## 2. Repo map (where to look for what)

```text
README.md                  overview + quick start
docs/ARCHITECTURE.md       components, data flow, IPC files, principles
docs/SETUP.md              full standalone install procedure
docs/CONFIGURATION.md      every config file + env var + tunable
docs/PROTOCOLS.md          wire formats (RNS, RS232, WiFi, LXMF, Meshtastic)
docs/OPERATIONS.md         day-2 ops + troubleshooting table
docs/CHANGES.md            build journal: what changed and why
docs/BUILD-DIRECTION.md    phased build order (observe → route → UI …)
skills/                    task playbooks (setup, debugging, firmware, RNS)
mesh-relay/                gateway + watcher + mqtt + live feed + tools
rns-bridge/                RNS bridge + peer tracker + propagation + status
lora-dashboard/            web control panel
esp32/                     C6 WiFi hub firmware + V3 wifi env + RS232 spec
systemd/                   unit files
```

## 3. Suggested tasks and prompts

- **Set it up:** "Follow docs/SETUP.md for this repo on Ubuntu. Ask me for
  the hardware details (ports, region) first; produce a step-by-step plan
  with verification after each step."
- **Debug "no messages arriving":** "Use docs/OPERATIONS.md §4 and
  skills/mesh-relay-flush-restart-safety. Check: gateway active? watcher job
  running? event file growing? dedup suppressing? Report findings before
  changing anything."
- **Debug a MeshCore radio link:** "Follow skills/meshcore-node-link-debug.
  Don't touch the production port without stopping the owner service; use
  the probes in mesh-relay/tools/."
- **Extend the RNS side:** "Read skills/reticulum-rns-integration and
  skills/rns-lxmf-debugging first; keep the target-byte protocol and the
  never-self-deliver rule intact."
- **Add a new consumer:** "Write a new tailer for /tmp/meshgw_events.jsonl
  following the offset+dedup pattern in mesh_watch.py / mqtt_bridge.py.
  Don't modify the gateway."

## 4. Invariants the LLM must preserve

1. One owner per serial port (see context block).
2. File IPC formats — append-only JSONL for events; parse tolerantly.
3. Loop prevention: radio-side, gateway-side, MQTT-side guards all stay.
4. Never self-deliver on RNS; peer-gate outbound.
5. Dedup `HOLD_S >= LOOP_WINDOW_S`; states persist across restarts.
6. Default-deny for radio transmission from untrusted sources (LXMF
   unprefixed, MQTT multi-network).
7. Localhost binding for HTTP surfaces; auth before action; audit changes.

## 5. Guardrails / don'ts

- **Don't** commit or paste secrets, private keys, identity files, or
  network specifics into the repo.
- **Don't** probe the production MeshCore port with `esptool` (ROM
  bootloader); don't open a second serial session (wedge risk — see
  `skills/lora-display-sleep-config` for the recovery sequence).
- **Don't** combine `rnodeconf --config` with `-t` (the blanking write is
  skipped silently).
- **Don't** restart the gateway twice after a blocked restart.
- **Don't** "fix" duplicate-looking messages by disabling dedup — fix the
  window/hold relationship instead.

## 6. Verification expectations

For any change, ask the LLM to state how it verified: compile check
(`python3 -m py_compile`), service state, endpoint JSON, a synthetic message
through `/tmp/meshgw_send.txt`, or an RNS test send
(`rns_send_test.py <target> "text"`). "It should work" is not evidence.
