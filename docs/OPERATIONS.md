# Operations & troubleshooting

Day-2 operations for a running bridge. Task-specific playbooks live in
`skills/` — this doc is the day-to-day reference.

## 1. Service control

```bash
systemctl status mesh-gateway.service      # the radio owner
systemctl status rnsd.service rns-bridge.service
systemctl status lora-dashboard.service mqtt-bridge.service mosquitto.service

journalctl -u mesh-gateway.service -n 50 --no-pager
journalctl -u rns-bridge.service -n 50 --no-pager
```

Health probes:

```bash
curl -s http://127.0.0.1:8082/            # gateway: {"status":"active","dedup":{...}}
curl -s http://127.0.0.1:8451/status.json # RNS leg
rnstatus                                  # RNS transport state (needs the rns venv)
```

## 2. Restart safety (mesh gateway — read first)

`mesh-gateway.service` has a deliberate **120 s boot delay**
(`ExecStartPre=/bin/sleep 120`; `TimeoutStartSec=300`). A synchronous
`systemctl restart` therefore blocks ~2 min and command runners will often
kill the exec mid-restart — **this is normal**.

Procedure when using `mesh_relay_setup.py flush` (which ends in a restart):

1. Snapshot the buffers:
   `wc -c /tmp/meshgw_events.jsonl /tmp/meshgw_outbound.jsonl /tmp/meshgw_send.txt /tmp/meshgw_dedup.json`
2. Run the flush/restart. Expect the runner to time out / get killed.
3. **Do not re-run it** — buffer clearing completes before the restart
   blocks; re-running just queues another 2-minute wait.
4. Verify side effects: buffers recreated empty; `/tmp/meshwatch_state.json`
   re-inits (`{"offset": 0}`).
5. Poll until up (≤3 min): `for i in $(seq 1 18); do st=$(systemctl is-active mesh-gateway.service); [ "$st" = "active" ] && break; sleep 10; done`
6. Confirm healthy: `:8082` returns `active` with dedup `3600/3600/120`.

`activating` + SubState `start-pre` during the delay is normal — **never
restart a second time**. (Full playbook: `skills/mesh-relay-flush-restart-safety`.)

## 3. Serial-port ownership rules

| Port | Owner | Notes |
|---|---|---|
| MeshCore (by-id USB_JTAG…) | `mesh-gateway.service` | exclusive lock |
| Meshtastic (by-id Heltec_Wireless_Tracker…) | `mesh-gateway.service` | exclusive lock |
| RNode (`/dev/ttyUSB0`) | `rnsd.service` (via the bridge) | rnsd owns it; bridge talks to RNS, not the port |

- **Any** radio send must go through the gateway's queue file
  (`meshgw reply …`), never a second serial connection.
- Before probing ports manually, stop the owning service; restart after.
- CP2102 clone quirk: after one port open per USB hub power cycle, further
  opens fail (`kernel -110`). Recover with a sysfs unbind/bind (find your
  hub path via `lsusb -t`):

  ```bash
  sudo sh -c "echo '1-4.2' > /sys/bus/usb/drivers/usb/unbind"; sleep 4
  sudo sh -c "echo '1-4.2' > /sys/bus/usb/drivers/usb/bind"; sleep 7
  ```

## 4. Common failure playbook

| Symptom | First checks | Fix |
|---|---|---|
| No messages in chat | 5s watcher job running? `mesh_watch.py` exists? gateway active? | restart watcher; see §2 |
| Gateway won't start; "Could not exclusively lock port" | another process on the serial port (`fuser /dev/ttyACM*`) | stop the other owner; restore single ownership |
| MeshCore node silent / empty neighbors | console date stuck years back = node never received a packet; `powersaving off`; antenna seated | see `skills/meshcore-node-link-debug` |
| Duplicate messages appearing twice | check dedup values on `:8082` | ensure `HOLD_S ≥ LOOP_WINDOW_S`; restart gateway |
| RNS peers never see the bridge | `journalctl -u rns-bridge` — announce logged? | check `rns_bridge.json` announce fields; `rnstatus` shows interface Up? |
| RNS TCP interface reconnect loop ("Network is unreachable") | IPv6 route flap | pin `prefer_ipv6 = False` (see propagation skill) |
| Sideband stamp errors | propagation node running? | start `rns-propagation.service`; give clients the propagation hash |
| Dashboard login fails | mosquitto up? credentials valid? | `mosquitto_pub` test; reset password via `mosquitto_passwd` |
| Dashboard can't restart a service | `sudo -n systemctl` allowlist / sudoers rule | grant the allowlist (`app.py` `SERVICE_ALLOWLIST`) via sudoers |
| Device scan finds nothing | companions on network? correct /24? | set scan settings / `LAN_SUBNET`; companions must be on TCP 5000/4403 |

## 5. Message flow debugging (end-to-end)

```bash
# watch inbound events live (all consumers read this file; one JSON per line):
tail -f /tmp/meshgw_events.jsonl

# send a test over MeshCore / Meshtastic / both (through the queue):
printf '{"net":"mc","text":"hello"}\n' >> /tmp/meshgw_send.txt

# send a test over RNS (from the rns venv):
cd /opt/mesh-bridge/rns-bridge && .venv/bin/python rns_send_test.py 3 "hello both meshes"

# watch what the watcher queues for delivery:
tail -f /tmp/meshgw_outbound.jsonl
```

Log markers to grep for: `[dedup] suppressed repeat`, `[bridge] mc->mt`,
`[rns] <- packet`, `[lxmf] -> peer`, `[gateway] outbound send`.

## 6. Cache / state hygiene

- `mesh_cache_cleanup.sh` clears only buffers idle ≥ 1 h (matches the dedup
  hold window). Run hourly. It never deletes fresh data.
- `.bak` reasoning: if you edit `mesh_gateway.py`, keep a copy; the service
  restarts are slow enough that mistakes are expensive.
- State that survives reboots: contacts (`state/meshgw_contacts.json`), RNS
  peer registry + DB (`rns-bridge/state/`), dashboard config (`config/`).

## 7. Updating the stack

1. Stop consumers of a changed file's IPC (e.g. stop the watcher job for
   gateway changes).
2. Edit, run `python3 -m py_compile <file>` as a syntax gate.
3. Restart the owning service (mind §2 for the gateway).
4. Re-run the verification checklist (`docs/SETUP.md` §10).

## 8. Monitoring ideas

- Gateway `:8082` JSON is cheap to poll; alert if status ≠ active.
- `rns-peer-tracker.service` + a daily notice job gives you "people are
  discovering the mesh" telemetry.
- Dashboard audit log (`lora-dashboard/audit.log`) is JSONL — easy to grep
  for logins/actions.
