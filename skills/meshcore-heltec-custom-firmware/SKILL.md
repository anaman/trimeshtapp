---
name: "meshcore-heltec-custom-firmware"
description: "Custom MeshCore firmware build for Heltec LoRa32 V3: add feature, rebuild via venv pio, verify, stage, flash"
---

# Custom MeshCore firmware builds (Heltec LoRa32 V3)

Use when asked to build a custom MeshCore firmware image for the Heltec V3
nodes (add a config key/feature, or rebuild after source edits).

## Layout (non-obvious)
- Repo: a clone of upstream meshcore-dev/MeshCore (e.g. under `third_party/`).
- Build envs (Heltec_v3_repeater, Heltec_v3_repeater_bridge_rs232) live in
  variants/heltec_v3/platformio.ini, pulled in by the main platformio.ini
  via `[platformio] extra_configs = variants/*/platformio.ini`.
  `extends = esp32_base` resolves from the MAIN ini. Do not hunt for env
  sections in the main ini or lab-notes/known-good-* copies.
- Variant ini carries the known-good board config (esp32-s3, US 910.525/SF7
  defaults, SX1262 pins, DISPLAY_CLASS=SSD1306Display).

## Build
- cd <your meshcore clone>
- .venv/bin/pio run -e Heltec_v3_repeater   # a venv holding PlatformIO; ~3 min cold, incremental after
- Also build Heltec_v3_repeater_bridge_rs232 if any unit may run that env.
- Output: .pio/build/<env>/firmware.bin — merged factory image (flash offset 0x0).

## Verify + stage
- strings .pio/build/<env>/firmware.bin | grep '<feature string>' — must hit.
- sha256sum firmware.bin.
- Stage: git diff -- <changed files> > patch; copy bin + sha256 + README into
  lab-notes/custom-<feature>/.

## Flash (only with explicit user go-ahead; gateway node is production)
- Gateway owns /dev/ttyACM0: stop mesh-gateway.service first.
- .venv/bin/python -m esptool --port /dev/ttyACM0 write_flash 0x0 <bin> (--after hard_reset).
- NEVER probe/read ACM0 with esptool — kicks node into ROM bootloader.
- Flashing does not wipe the data partition: prefs/name/radio/contacts persist.
- Restart gateway (120 s boot), verify via CLI `get <key>`.

## Adding a config key (verified pattern)
- NodePrefs: add uint16 field with default; PowerPrefs::structure() def("short_key", field).
  Missing key in prefs.json → in-memory default applies; existing devices unaffected.
- CommonCLI.cpp: set chain memcmp "key " (len incl. space) + range-check +
  savePrefs(); get chain memcmp "key" (no space). Reply "OK"/"ERROR: ...".
- UITask.cpp: begin() must assign _node_prefs BEFORE computing _auto_off
  (original code computed it first — reorder). 0 → ULONG_MAX (never off) is
  safe: millis() is uint32, never exceeds ULONG_MAX on 32-bit.
- Wake sources used: user button click + Serial.available() (CLI/companion).

## Verification
- pio run ends SUCCESS; strings grep hits; sha256 recorded; after flash,
  CLI `get <key>` returns the configured value.
