---
name: "lora-display-sleep-config"
description: "Set/check display sleep on RNode, Meshtastic, MeshCore LoRa units; burn-in protection"
---

# LoRa display sleep config (RNode / Meshtastic / MeshCore)

Use when setting or checking display sleep/blanking on the three LoRa units on this rig. Trigger: user asks to "set display to sleep after N minutes" (backlight burn-in protection) for Reticulum/Meshtastic/MeshCore units. Timeout in seconds (240 = 4 min).

## Service ownership — stop owner before touching ports
- rns-bridge owns /dev/ttyUSB0 (RNode/Reticulum, Heltec V3).
- mesh-gateway owns /dev/ttyACM0 + /dev/ttyACM1 (the MeshCore + Meshtastic radios).
- Stop owner → config → restart owner → verify active. Use gateway window for both ACM ports at once.

## RNode (ttyUSB0)
Write: `/opt/mesh-bridge/rns-bridge/.venv/bin/rnodeconf -t 240 /dev/ttyUSB0`
- PITFALL: never combine with --config. `rnodeconf --config` ends with graceful_exit() BEFORE the -t handler, so `--config -t 240` prints config and exits WITHOUT setting. Run `-t` alone.
Verify: `rnodeconf --config /dev/ttyUSB0` → expect "Display blanking: 240s" (was "Disabled" = always on).
Wedge rule (common CP2102 clone quirk): after one port open per USB hub power
cycle, further opens fail (kernel -110). Unbind/bind between EVERY open (example
hub path; use yours):
  `echo 7-4 > /sys/bus/usb/drivers/usb/unbind; sleep 4; echo 7-4 > /sys/bus/usb/drivers/usb/bind; sleep 7`
Sequence: cycle → -t write → cycle → --config verify → cycle → start rns-bridge → confirm KISS online in /tmp/rns_bridge_status.json.

## Meshtastic (ACM1)
Gateway must be stopped. CLI: `/opt/mesh-bridge/mesh-relay/.venv/bin/meshtastic`
Set: `meshtastic --port /dev/ttyACM1 --set display.screen_on_secs 240`
Verify: `meshtastic --port /dev/ttyACM1 --get display.screen_on_secs`
- PITFALL: immediate --get right after --set can show the OLD value (flash write still flushing). Wait ~5 s and re-read; a stale 600 right after "Set ... to 240" is normal, not a failure.

## MeshCore (ACM0)
No display-sleep config exists in MeshCore firmware (checked the complete current command/config-key reference — no display/backlight/screen key — and firmware source: OLED only powers off in the deep-sleep path; `powersaving` throttles the radio, not display-only). Report honestly; offer upstream feature request or custom firmware build. Re-verify against the current command reference before asserting (firmware may gain the feature).
Probing the node: raw pyserial CLI writes get NO reply — firmware requires the appstart handshake. With gateway stopped use the meshcore lib: `/opt/mesh-bridge/mesh-relay/tools/probe_meshcore.py` (read-only, by-id port path, prints self_info + contacts).

## Restore + verify
- Start mesh-gateway, poll `systemctl is-active` up to ~3 min (120 s boot delay). Don't re-run start while it boots.
- All 5 services active: mesh-gateway, rns-bridge, rns-status-server, lora-dashboard, mosquitto.
- Gateway healthy: `curl -s http://127.0.0.1:8082/` → "active"; dedup window 3600/hold 3600/max 120 intact.
- RNS LoRa leg: /tmp/rns_bridge_status.json → KISS "online" with tx bytes growing.

## Evidence of correctness
- rnodeconf --config read-back prints the exact seconds value (240s).
- meshtastic --get after ~5 s prints the new value.
- All 5 services active; KISS online; gateway status active.
