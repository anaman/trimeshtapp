---
name: "mesh-relay-flush-restart-safety"
description: "Mesh-relay flush/restart safety: systemctl restart mesh-gateway.service blocks 120s and exec gets SIGKILLed; verify side effects, poll until active."
---

# Mesh Relay Flush / Restart Recovery

Use when running `python3 /opt/mesh-bridge/mesh-relay/mesh_relay_setup.py flush`,
or any command whose last step is `systemctl restart mesh-gateway.service`.
These block on the service's 120s boot delay and command runners may kill
the exec mid-restart (timeout).

## Why it hangs
mesh-gateway.service runs a 2-min boot delay (ExecStartPre sleep;
TimeoutStartSec=300; unit shows `activating` / SubState `start-pre` during
it). A synchronous `systemctl restart` waits for full activation, so the
exec tool kills the command (~30s) while the unit is still coming up.

## Procedure
1. Snapshot buffers first:
   `wc -c /tmp/meshgw_events.jsonl /tmp/meshgw_outbound.jsonl /tmp/meshgw_send.txt /tmp/meshgw_dedup.json`
2. Run the flush (or restart). Expect the runner to hang and get killed — normal.
3. Do NOT rerun the whole command. Buffer clearing completes BEFORE the
   restart blocks; rerunning just queues another 120s wait. Verify instead.
4. Verify side effects: buffer files are 0 bytes; `meshwatch_state.json`
   re-inits (~26 bytes, `{"offset": 0}`).
5. Poll until up (cap ~3 min) instead of one long sleep:
   ```bash
   for i in $(seq 1 18); do st=$(systemctl is-active mesh-gateway.service); [ "$st" = "active" ] && break; sleep 10; done
   ```
6. Confirm healthy: `systemctl is-active mesh-gateway.service` -> active;
   `curl -s http://127.0.0.1:8082/` -> `{"status":"active", "dedup":{...}}`
   with window_s/hold_s/max_tracked matching the LOOP_WINDOW_S/HOLD_S/
   MAX_TRACKED constants in /opt/mesh-bridge/mesh-relay/mesh_gateway.py
   (as of 2026-08: 3600/3600/120).
7. If your command runner supports timeouts, give the wait command a
   generous timeout (~200s) so it returns once active.

## Pitfalls
- `activating` + SubState `start-pre` during the delay is normal, not a
  fault — do not restart again.
- After SIGKILL, verify state before assuming failure; never blindly retry
  a restart (non-idempotent: doubles the wait).
- `flush` clears gateway-side history only (events/outbound/send/dedup/
  watcher state files). On-device radio history is NOT wiped — MeshCore has
  no message-delete primitive.

## Verify
After the restart, the :8082 endpoint returns status active and dedup
values equal to the gateway constants (window_s/hold_s/max_tracked).
