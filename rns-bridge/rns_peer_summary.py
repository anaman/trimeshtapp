#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Emit a periodic Reticulum new-peer count notice, or nothing if there are no new peers.

Sources (both are written ONLY for genuinely first-seen peers):
  - logs/rns_new_peers.jsonl                (bridge, persistent)
  - /tmp/rns_peers.jsonl                    (legacy compatibility log)

The bridge owns "new" detection via its persistent registry; this script just
delivers whatever new-peer events have accumulated since the last run, deduped
by hash. No registry reads here.
"""
from __future__ import annotations
import json, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
LOG_DIR = BASE / "logs"
STATE_DIR = BASE / "state"
NEW_LOG = LOG_DIR / "rns_new_peers.jsonl"
LEGACY_LOG = Path("/tmp/rns_peers.jsonl")
SUMMARY_STATE = STATE_DIR / "rns_hourly_peer_summary_state.json"
TZ = ZoneInfo(__import__("os").environ.get("RNS_TRACKER_TZ", "UTC"))
SOURCES = [str(NEW_LOG), str(LEGACY_LOG)]


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def atomic_write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    tmp.replace(path)


def parse_events_from(path: Path, offset: int):
    if not path.exists():
        return [], 0
    size = path.stat().st_size
    if offset > size:
        offset = 0  # truncated/rotated
    events = []
    with path.open() as f:
        f.seek(offset)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            h = ev.get("hash")
            if not h:
                continue
            ev.setdefault("ts", time.time())
            ev["hash"] = str(h)
            ev.setdefault("hash_short", str(h)[:16])
            ev.setdefault("lxmf", bool(ev.get("lxmf")))
            ev.setdefault("name", None)
            events.append(ev)
        return events, f.tell()


def main():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    state = read_json(SUMMARY_STATE, {"offsets": {}})
    offsets = state.get("offsets") if isinstance(state.get("offsets"), dict) else {}

    new_events = []
    new_offsets = dict(offsets)
    for src in SOURCES:
        path = Path(src)
        events, eof = parse_events_from(path, int(offsets.get(src, 0) or 0))
        new_offsets[src] = eof
        new_events.extend(events)

    atomic_write_json(SUMMARY_STATE, {"updated_ts": time.time(), "offsets": new_offsets})

    # Deduplicate by hash inside the batch (same peer may appear in both logs).
    seen = set()
    unique = []
    for ev in new_events:
        h = ev["hash"]
        if h in seen:
            continue
        seen.add(h)
        unique.append(ev)
    if not unique:
        return

    lxmf = sum(1 for ev in unique if ev.get("lxmf"))
    plain = len(unique) - lxmf
    # Deliberately minimal: the notice carries only the new-peer count.
    print(f"Reticulum: {len(unique)} new peer(s) since last notice ({lxmf} LXMF / {plain} RNS).")


if __name__ == "__main__":
    main()
