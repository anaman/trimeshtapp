#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
mesh_watch.py — channel-delivery watcher for the Mesh Gateway.

Runs every ~5s via your scheduler. Responsibilities:
  1. Tail /tmp/meshgw_events.jsonl; forward NEW mesh messages to Telegram
     (primary) / WhatsApp (failover) via the delivery agent's channel layer.
  2. Process the reply-command inbox and send over the radios through the
     gateway:
         mt <text>        -> send over Meshtastic
         mc <text>        -> send over MeshCore
         mt mc <text>  or mc mt <text>  -> send over BOTH networks

State (last-read byte offset, pending commands, per-message dedup) lives in
/tmp/meshwatch_state.json so the job is idempotent across cron runs.

Delivery is delegated: this script only WRITES outbound directives to
/tmp/meshgw_outbound.jsonl; the delivery agent consumes them and sends via
its channel layer (Telegram/WhatsApp) — the script itself has no SDK
credentials.
"""

import json
import os
import sys
import time

EVENT_FILE = "/tmp/meshgw_events.jsonl"
EMIT_FILE = "/tmp/meshgw_outbound.jsonl"   # notify directives for agent delivery
SEND_FILE = "/tmp/meshgw_send.txt"           # radio outbound -> gateway-owned queue
RNS_SEND_FILE = "/tmp/rns_send.txt"          # RNS outbound -> rns_bridge.py queue
RNS_PEER_FILE = "/tmp/rns_peers.jsonl"       # new RNS peer notices from rns_bridge.py
LXMF_INBOUND_FILE = "/tmp/rns_lxmf_inbound.jsonl"  # unprefixed LXMF -> Telegram-only notices
INBOX_FILE = "/tmp/meshgw_inbox.jsonl"   # the delivery agent writes user reply commands here
STATE_FILE = "/tmp/meshwatch_state.json"
NOTICE_FILE = "/tmp/meshgw_notice.jsonl"     # gateway operational notices -> Telegram
CONTACTS_FILE = "/opt/mesh-bridge/mesh-relay/state/meshgw_contacts.json"

# Since a cron job is a fresh process each run, keep bounded state.
MAX_FORWARD_PER_RUN = 50


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"offset": 0, "dedup": []}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[watch] state save failed: {e}", file=sys.stderr)


def tail_new_events(state):
    """Return events appended after the last-read offset; advance offset."""
    new_events = []
    try:
        size = os.path.getsize(EVENT_FILE)
        offset = state.get("offset", 0)
        if size < offset:
            offset = 0  # file rotated/truncated
        with open(EVENT_FILE, "rb") as f:
            f.seek(offset)
            chunk = f.read()
        if not chunk:
            return new_events
        state["offset"] = offset + len(chunk)
        for line in chunk.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
                new_events.append(ev)
            except Exception:
                continue
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[watch] tail error: {e}", file=sys.stderr)
    return new_events


def dedup_key(ev):
    return f"{ev.get('net','?')}:{ev.get('sender','?')}:{ev.get('text','?')}:{ev.get('ts','')}"


def emit_outbound(kind, payload):
    """Write a directive for the delivery agent to act on."""
    try:
        with open(EMIT_FILE, "a") as f:
            f.write(json.dumps({"kind": kind, "payload": payload}) + "\n")
    except Exception as e:
        print(f"[watch] outbound write failed: {e}", file=sys.stderr)


def queue_radio_send(net, text):
    """Write a send command to the gateway-owned queue (bypasses the port
    conflict: the gateway itself holds the radios and polls this file)."""
    try:
        with open(SEND_FILE, "a") as f:
            f.write(json.dumps({"net": net, "text": text}) + "\n")
    except Exception as e:
        print(f"[watch] send-queue write failed: {e}", file=sys.stderr)


def queue_dm_send(net, text, target=None):
    """Write a DIRECT-message reply command to the gateway-owned queue."""
    try:
        rec = {"net": net, "dm": True, "text": text}
        if target:
            rec["target"] = target
        with open(SEND_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        print(f"[watch] dm send-queue write failed: {e}", file=sys.stderr)


def queue_contact_cmd(action, key, net=None):
    """Queue a contact save/forget/list command for the gateway."""
    try:
        rec = {"cmd": "contact", "action": action, "key": key}
        if net:
            rec["net"] = net
        with open(SEND_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        print(f"[watch] contact cmd write failed: {e}", file=sys.stderr)


def contacts_listing():
    """Human-readable contact list (read-only)."""
    try:
        with open(CONTACTS_FILE) as f:
            d = json.load(f)
    except Exception:
        return "no contacts recorded"
    out = []
    now = time.time()
    for net in ("mc", "mt"):
        for cid, e in sorted((d.get(net) or {}).items(),
                             key=lambda kv: kv[1].get("last_seen", 0), reverse=True):
            age = int((now - float(e.get("last_seen", 0))) / 86400)
            flag = "SAVED" if e.get("saved") else f"{age}d"
            out.append(f"{net} {e.get('name') or '-'} [{cid[:12]}] {flag}")
    return "; ".join(out) if out else "no contacts recorded"


def queue_rns_send(net, text):
    """Write a send command to the RNS bridge queue (rns_bridge.py polls
    /tmp/rns_send.txt and delivers over Reticulum to peers)."""
    try:
        with open(RNS_SEND_FILE, "a") as f:
            f.write(json.dumps({"net": net, "text": text}) + "\n")
    except Exception as e:
        print(f"[watch] rns send-queue write failed: {e}", file=sys.stderr)


def process_inbox():
    """Read reply commands the delivery agent parked for us, forward to radios."""
    if not os.path.exists(INBOX_FILE):
        return
    try:
        with open(INBOX_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    cmd = json.loads(line)
                except Exception:
                    continue
                handle_command(cmd)
        # clear inbox after processing
        os.remove(INBOX_FILE)
    except Exception as e:
        print(f"[watch] inbox error: {e}", file=sys.stderr)


def handle_command(cmd):
    text = cmd.get("text", "").strip()
    raw = text
    # normalize: strip leading bot mention or slash
    lower = text.lower()
    net = None
    body = text

    # command forms: "mt msg", "mc msg", "rt msg", "mt mc msg", "mc mt msg",
    # "rt mc msg", "both msg", "all msg"  (both/all = all 3 networks)
    parts = text.split(None, 1)
    if not parts:
        return
    first = parts[0].lower().rstrip(":").lstrip("/").lstrip("@")
    rest = parts[1] if len(parts) > 1 else ""
    rest = rest.strip().lstrip(":")
    # DM reply routes, with optional explicit target:
    #   dmc <text>                    -> last MeshCore DM sender
    #   dmc to <name|id> <text>       -> explicit MeshCore target
    #   dmt <text> / dmt to <t> <text>-> Meshtastic equivalent
    #   dm mc <text> / dm mt <text>
    def _dm(net, s):
        s = s.strip()
        toks = s.split(None, 2)
        if len(toks) >= 1 and toks[0].lower() in ("to", "@"):
            if len(toks) >= 3:
                queue_dm_send(net, toks[2], target=toks[1])
                print(f"[watch] queued {net} DM -> {toks[1]}: {toks[2]}")
            else:
                print("[watch] DM: missing target or text")
        elif s:
            queue_dm_send(net, s)
            print(f"[watch] queued {net} DM reply: {s}")

    if first in ("dmc", "dmmc"):
        _dm("mc", rest)
        return
    if first in ("dmt", "dmmt"):
        _dm("mt", rest)
        return
    if first in ("dm", "reply"):
        sub = rest.split(None, 1)
        if len(sub) == 2 and sub[0].lower() in ("mc", "mt"):
            _dm(sub[0].lower(), sub[1])
        return

    # contact registry commands
    if first in ("contacts", "contactlist", "listcontacts"):
        emit_outbound("notify", {"net": "sys", "sender": "contacts",
                                 "text": contacts_listing()})
        print(f"[watch] contacts: {contacts_listing()}")
        return
    if first in ("savecontact", "save", "keep"):
        sub = rest.split(None, 1)
        net = None
        key = rest
        if len(sub) == 2 and sub[0].lower() in ("mc", "mt"):
            net, key = sub[0].lower(), sub[1]
        if key:
            queue_contact_cmd("save", key, net)
            print(f"[watch] queued contact save: {key} ({net or 'both'})")
        return
    if first in ("forgetcontact", "forget", "drop", "removecontact", "delcontact"):
        sub = rest.split(None, 1)
        net = None
        key = rest
        if len(sub) == 2 and sub[0].lower() in ("mc", "mt"):
            net, key = sub[0].lower(), sub[1]
        if key:
            queue_contact_cmd("forget", key, net)
            print(f"[watch] queued contact forget: {key} ({net or 'both'})")
        return

    if first in ("mt", "mc", "rt"):
        net = first
        body = rest
    elif first in ("mtmc", "mcmt", "mt/mc", "mc/mt", "mt rt", "rt mt",
                   "mc rt", "rt mc", "both", "all"):
        net = "both"
        body = rest
    else:
        # maybe "mt mc text" / "mc mt text" / "rt mc text" etc.
        sub = text.split()
        if len(sub) >= 3 and sub[0].lower() in ("mt", "mc", "rt") \
                and sub[1].lower() in ("mt", "mc", "rt"):
            net = "both"
            body = " ".join(sub[2:])
            # order doesn't matter -> all networks
        else:
            return

    if not body:
        print(f"[watch] empty command body: {raw!r}")
        return

    if net == "mc":
        queue_radio_send("mc", body)
        print(f"[watch] queued mc send: {body}")
    elif net == "mt":
        queue_radio_send("mt", body)
        print(f"[watch] queued mt send: {body}")
    elif net == "rt":
        queue_rns_send("rt", body)
        print(f"[watch] queued rt (RNS) send: {body}")
    elif net == "both":
        # all three networks: MeshCore + Meshtastic + Reticulum
        queue_radio_send("mc", body)
        queue_radio_send("mt", body)
        queue_rns_send("both", body)
        print(f"[watch] queued all-3 send (mc+mt+rns): {body}")


def main():
    state = load_state()
    changed = False
    pending_print = []

    # 1) forward new inbound mesh messages
    new_events = tail_new_events(state)
    if new_events:
        for ev in new_events[:MAX_FORWARD_PER_RUN]:
            key = dedup_key(ev)
            if key in state.get("dedup", []):
                continue
            # dedup bounded
            state.setdefault("dedup", []).append(key)
            if len(state["dedup"]) > 500:
                state["dedup"] = state["dedup"][-500:]
            is_dm = bool(ev.get("dm"))
            emit_outbound("notify", {
                "net": ev.get("net", "?"),
                "dm": is_dm,
                "sender": ev.get("sender", "?"),
                "text": ev.get("text", ""),
            })
            label = "mc" if ev.get("net") == "mc" else "mt"
            if is_dm:
                label = label + " DM"
            line = f"[{label}] {ev.get('sender', '?')}: {ev.get('text', '')}"
            pending_print.append(line)
            print(f"[watch] queued notify [{ev.get('net')}] {ev.get('sender')}: {ev.get('text')}", file=sys.stderr)
        changed = True

    # 2) RNS peer recording + daily summary now handled by rns_peer_tracker.py
    #    (SQLite peers table keyed by peer hash; one summary notice per day,
    #    delivered by the "Reticulum peer tracker notice" cron). Per-peer
    #    "[rns] RNS peer found" relay removed per design decision 2026-09-03: hash
    #    records yes, per-peer Telegram pings no.
    pass

    # 2b) new Telegram-only LXMF notices (rns_bridge.py on_lxmf_message:
    #     unprefixed Sideband messages -> Telegram, never transmitted)
    try:
        lxmf_size = os.path.getsize(LXMF_INBOUND_FILE)
        lxmf_off = state.get("lxmf_offset", 0)
        if lxmf_size < lxmf_off:
            lxmf_off = 0
        with open(LXMF_INBOUND_FILE, "rb") as f:
            f.seek(lxmf_off)
            chunk = f.read()
        if chunk:
            state["lxmf_offset"] = lxmf_off + len(chunk)
            for line in chunk.decode("utf-8", "replace").splitlines():
                if not line.strip():
                    continue
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                src = (m.get("src") or "?")[:12]
                text = m.get("text", "")
                line_out = f"[rns] LXMF {src}: {text}"
                pending_print.append(line_out)
                print(line_out, file=sys.stderr)
                changed = True
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[watch] lxmf tail error: {e}", file=sys.stderr)

    # 2c) gateway operational notices (contact ops / DM sends) -> Telegram
    try:
        nsize = os.path.getsize(NOTICE_FILE)
        noff = state.get("notice_offset", 0)
        if nsize < noff:
            noff = 0
        with open(NOTICE_FILE, "rb") as f:
            f.seek(noff)
            chunk = f.read()
        if chunk:
            state["notice_offset"] = noff + len(chunk)
            for line in chunk.decode("utf-8", "replace").splitlines():
                if not line.strip():
                    continue
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                txt = m.get("text", "")
                emit_outbound("notify", {"net": "sys", "sender": m.get("net", "sys"),
                                         "text": txt})
                pending_print.append(f"[notice] {txt}")
                changed = True
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[watch] notice tail error: {e}", file=sys.stderr)

    # 3) process reply commands
    process_inbox()

    save_state(state)

    # 4) announce mode: only print the deliverable line(s) if there was new
    #    traffic; otherwise print the silent token so cron announce posts nothing.
    if pending_print:
        for line in pending_print:
            print(line)
    else:
        print("NO_REPLY")


if __name__ == "__main__":
    main()
