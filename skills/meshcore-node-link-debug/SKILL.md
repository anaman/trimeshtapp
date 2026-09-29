---
name: "meshcore-node-link-debug"
description: "Debug MeshCore node RF link failure: empty neighbors, stuck RTC, deaf nodes; CP2102 console wedge recovery."
---

# MeshCore node RF link debug

When to use: a MeshCore node (Heltec V3 / ESP32) won't link to the mesh — empty `neighbors`, two close nodes mutually deaf, or console date years behind (node never received a packet). Also use for any pyserial console session on /dev/ttyUSB0 (CP2102).

## Console access: CP2102 wedge rule
- One pyserial session per hub-cycle. Put ALL commands in a single run:
  `serial.Serial("/dev/ttyUSB0", 115200, timeout=0.3)`, write `cmd\r`, sleep ~1.5s, read, repeat, close.
- Second open wedges the port: open raises `OSError: [Errno 5]` (wrapped in SerialException). Do not retry — recover instead.
- Wedge recovery (sysfs unbind/rebind, ~11s) — find your hub path with
  `lsusb -t` / `usb-devices` (example shape: `1-4.2`):
  `sudo sh -c "echo '<hub-path>' > /sys/bus/usb/drivers/usb/unbind"`; sleep 4; `sudo sh -c "echo '<hub-path>' > /sys/bus/usb/drivers/usb/bind"`; sleep 7; verify `/dev/ttyUSB0` exists.
- Hub cycle disconnects ALL devices on that hub, incl. production radios. Pause mesh-gateway.service before cycling; restore after (below).

## RF diagnostics via CLI
- `powersaving` → want `off` (always listening); set with `powersaving off`.
- `get dutycycle` → 50% = fine; low values (EU-style ~1-10%) throttle TX. `get af` → 1.0 = no airtime throttle. `region` → region map.
- RX alive proof: debug build prints `DEBUG: RadioLibWrapper: noise_floor = -1xx` after each command (healthy ~-100).
- TX test: `advert` → `OK - Advert sent`; bridge build logs `BRIDGE: TX, len=NNN`.
- RTC unsynced (e.g. `15/5/2024`) = node has never received a single network packet.
- Firmware freq check: `pio run -v -e <env>`, then on the g++ command line compare positions of both `-D LORA_FREQ=...` occurrences — LAST define wins (GCC last-wins; base PlatformIO flags precede env overrides). Also confirms via flash read-back hash match.
- All software layers OK yet nodes still deaf at close range → physical RF path: antenna loose/missing, RF switch, PA. Do NOT keep forcing TX at high dBm with the antenna off (PA damage risk). Ask the human to check antenna + OLED.

## Gateway restore after probing
- `sudo systemctl start mesh-gateway.service`; `systemctl is-active` → active; `curl -s http://127.0.0.1:8082/` → `"status":"active","bridge":true` (dedup 3600/3600/120); `journalctl -u mesh-gateway.service` → `Both radios connected`. Systemd may self-restart it mid-probe — re-verify rather than assume it is down.

## Verification
- Peer node hears the forced advert (new pending contact or fresh `last_advert` in probe output), OR the physical-layer blocker is confirmed and reported. After any hub cycling, gateway status endpoint healthy.
