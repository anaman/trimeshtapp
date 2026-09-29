#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# mesh_cache_cleanup.sh — clear LoRa mesh bridge cache buffers that have been
# idle for >= 1 hour (matches the gateway dedup hold window, LOOP_WINDOW_S=3600).
# Run hourly via the "Mesh Cache Clear" automation.
#
# Clears:
#   /tmp/meshgw_events.jsonl    (inbound event log tailed by mesh_watch.py)
#   /tmp/meshgw_outbound.jsonl  (notify queue consumed by the watcher cron job)
#   /tmp/meshgw_send.txt        (radio outbound queue)
#   /tmp/meshgw_dedup.json      (gateway rolling dedup state)
# plus resets /tmp/meshwatch_state.json offset when the event log is cleared.
#
# Files modified within the last hour are left alone so in-flight/in-window
# messages are never lost — this is the "an hour after the held transmission
# watch is sent, clear the cache" policy.

NOW=$(date +%s)
HOLD=3600
CHANGED=0

for f in /tmp/meshgw_events.jsonl /tmp/meshgw_outbound.jsonl /tmp/meshgw_send.txt /tmp/meshgw_dedup.json; do
  if [ -f "$f" ]; then
    MT=$(stat -c %Y "$f" 2>/dev/null)
    if [ -n "$MT" ]; then
      AGE=$(( NOW - MT ))
      if [ "$AGE" -ge "$HOLD" ]; then
        : > "$f"
        echo "cleared $f (idle ${AGE}s)"
        CHANGED=1
      fi
    fi
  fi
done

# Reset watcher tail offset only when the event log was cleared
if [ -f /tmp/meshgw_events.jsonl ]; then
  MT=$(stat -c %Y /tmp/meshgw_events.jsonl 2>/dev/null)
  if [ -n "$MT" ] && [ $(( NOW - MT )) -ge "$HOLD" ]; then
    printf '{"offset": 0}' > /tmp/meshwatch_state.json
    echo "reset /tmp/meshwatch_state.json offset"
    CHANGED=1
  fi
fi

if [ "$CHANGED" -eq 0 ]; then
  echo "nothing to clear (all buffers fresh)"
fi
