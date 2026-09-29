#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Mesh Gateway — drains MeshCore + Meshtastic public channels and routes
messages to Telegram (primary) / WhatsApp (failover), with a reply path
back over the radios. Replaces MT-MC_relay.py as the single owner of both
serial ports. Built 2026-08-11.

Architecture
------------
- One asyncio process owns BOTH radios (they cannot be shared).
- Subscribes to MeshCore CHANNEL_MSG_RECV and Meshtastic text messages.
- Delivers each inbound message to Telegram; if Telegram send fails,
  falls back to WhatsApp.
- Reply path: user sends "mt <text>" or "mc <text>" to the bot; this
  process polls a reply queue (consumed via a small watch loop in the
  delivery agent) and sends over the matching radio.
- Optional MT<->MC auto-bridge toggle (like the old relay), controlled by
  BRIDGE_BOTH_WAYS.
- Headless status endpoint on PORT_HTTP for remote visibility.

Delivery is intentionally NOT done with direct provider SDKs here — it
delegates to an external “delivery agent” (any framework that can watch a file
and send chat messages — an LLM agent works well) which tails the JSON events
this process writes and delivers them. See ./meshgw in this directory.
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

import serial
from meshcore import MeshCore, EventType
from meshtastic import serial_interface
from meshtastic.protobuf.portnums_pb2 import PortNum

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# --- Radio serial ports -----------------------------------------------------
# Resolution order: env override > ports.json (written by mesh_relay_setup.py)
# > /dev/serial/by-id match by USB descriptor prefix > plain-device fallback.

def _find_serial(env_name, ports_key, by_id_prefix, fallback):
    val = os.environ.get(env_name)
    if val:
        return val
    try:
        cfg = json.loads(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ports.json")).read())
        if cfg.get(ports_key):
            return cfg[ports_key]
    except Exception:
        pass
    import glob
    hits = sorted(glob.glob(f"/dev/serial/by-id/{by_id_prefix}*"))
    return hits[0] if hits else fallback

MESHTASTIC_PORT = _find_serial(
    "MESHGW_MT_PORT", "meshtastic", "usb-Espressif_Systems_Heltec_Wireless_Tracker", "/dev/ttyACM1")
MESHCORE_PORT = _find_serial(
    "MESHGW_MC_PORT", "meshcore", "usb-Espressif_USB_JTAG_serial_debug_unit", "/dev/ttyACM0")

# Channel labels for inbound routing
MC_LABEL = "mc"   # MeshCore
MT_LABEL = "mt"   # Meshtastic

# Whether to also auto-bridge MT<->MC (the old relay behavior), on top of
# messaging-gateway delivery. Set "0" to disable bridging (pure gateway).
BRIDGE_BOTH_WAYS = os.environ.get("MESHGW_BRIDGE", "1") == "1"

# Flag on relayed/bridged messages to avoid loops
BRIDGE_PREFIX = "[MT<>MC]"

# JSON event output file the delivery agent tails
EVENT_FILE = "/tmp/meshgw_events.jsonl"

# Optional headless status endpoint port (0 = disabled)
PORT_HTTP = int(os.environ.get("MESHGW_HTTP_PORT", "8082"))

# Dedup window config: a message is treated as a duplicate if the same
# sender+text was seen within LOOP_WINDOW_S. Each entry is retained for
# HOLD_S (must be >= window_s so entries are not evicted before the reject
# window passes — the entry must survive long enough to suppress a repeat
# inside the whole window). The rolling buffer is bounded at MAX_TRACKED
# entries. State persists to DEDUP_FILE so a gateway restart does not lose
# the recent history. This prevents a message that appears on BOTH networks
# (MT<->MC) from being relayed/delivered twice within the window.
LOOP_WINDOW_S = 3600       # 1 hour: reject repeats inside this window
HOLD_S = 3600              # 1 hour: hold entries for the full reject window
MAX_TRACKED = 120          # track the last 120 messages (more, since held 1h)
DEDUP_FILE = "/tmp/meshgw_dedup.json"

# Contact registry: anyone who DMs us is recorded here. Entries with no
# contact for CONTACTS_TTL_DAYS are purged, EXCEPT entries explicitly saved
# (`saved: true`), which persist until removed. State lives on disk (not /tmp)
# so it survives reboots.
CONTACTS_FILE = "/opt/mesh-bridge/mesh-relay/state/meshgw_contacts.json"
CONTACTS_TTL_DAYS = 90
NOTICE_FILE = "/tmp/meshgw_notice.jsonl"

# Advert names to watch for on MeshCore. When one appears in the contact
# table it is recorded + pinned (saved) once, then never re-notified.
WATCH_ADVERTS = [n.strip() for n in
                 os.environ.get("MESHGW_WATCH_ADVERTS", "").split(",")
                 if n.strip()]


class MessageDedup:
    """Time-windowed, persistent, bounded dedup.

    Rejects an inbound message if the same (sender, text) was seen within
    the last LOOP_WINDOW_S seconds. Retains at most MAX_TRACKED entries,
    each aged out after HOLD_S (hold_s >= window_s so the entry survives the
    full reject window). State is saved to DEDUP_FILE so restarts
    preserve the rolling window (prevents loops across gateway restarts).
    Dedup is GLOBAL across both networks: a message first seen on MeshCore
    will suppress the same message arriving on Meshtastic within the window,
    and vice versa — stopping the bridge double-reaching the user.
    """

    def __init__(self, window_s=LOOP_WINDOW_S, hold_s=HOLD_S, max_tracked=MAX_TRACKED):
        self.window_s = window_s
        self.hold_s = hold_s
        self.max_tracked = max_tracked
        self.entries = []  # list of [key, timestamp]
        self._load()

    def _load(self):
        try:
            with open(DEDUP_FILE) as f:
                data = json.load(f)
            now = time.time()
            self.entries = [
                (k, t)
                for k, t in data
                if (now - t) < self.hold_s  # drop entries older than the hold window
            ]
        except Exception:
            self.entries = []

    def _save(self):
        try:
            with open(DEDUP_FILE, "w") as f:
                json.dump(self.entries, f)
        except Exception as e:
            print(f"[dedup] save failed: {e}", file=sys.stderr)

    def _prune(self, now):
        # remove entries outside the hold window
        self.entries = [(k, t) for k, t in self.entries if (now - t) < self.hold_s]
        # bound to the last max_tracked
        if len(self.entries) > self.max_tracked:
            self.entries = self.entries[-self.max_tracked:]

    def is_duplicate(self, sender, text):
        now = time.time()
        self._prune(now)
        key = f"{sender}\x1f{text}"  # sender and text separated by unit separator
        for k, t in self.entries:
            if k == key and (now - t) < self.window_s:
                return True
        return False

    def note(self, sender, text):
        """Record a seen message (call after is_duplicate returns False)."""
        now = time.time()
        key = f"{sender}\x1f{text}"
        # if a matching key already exists, refresh its timestamp
        for i, (k, t) in enumerate(self.entries):
            if k == key:
                self.entries[i] = (key, now)
                self._prune(now)
                self._save()
                return
        self.entries.append((key, now))
        self._prune(now)
        self._save()


# ---------------------------------------------------------------------------
# Event plumbing
# ---------------------------------------------------------------------------

def emit(event: dict):
    event["ts"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(EVENT_FILE, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as e:
        print(f"[gateway] event write failed: {e}", file=sys.stderr)


class ContactStore:
    """Per-network contact registry with idle-expiry (saved entries exempt)."""

    def __init__(self, path=CONTACTS_FILE, ttl_days=CONTACTS_TTL_DAYS):
        self.path = path
        self.ttl = ttl_days * 86400
        self.data = {"mc": {}, "mt": {}}
        self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            if isinstance(d, dict):
                self.data = {"mc": d.get("mc", {}), "mt": d.get("mt", {})}
        except Exception:
            self.data = {"mc": {}, "mt": {}}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self.data, f, indent=1)
        except Exception as e:
            print(f"[contacts] save failed: {e}", file=sys.stderr)

    def touch(self, net, cid, name=None):
        """Record activity for a contact (create if new)."""
        if net not in self.data or not cid:
            return
        now = time.time()
        e = self.data[net].get(str(cid))
        if e is None:
            e = {"name": name, "first_seen": now, "last_seen": now,
                 "saved": False, "seen": 0}
            self.data[net][str(cid)] = e
        e["last_seen"] = now
        e["seen"] = int(e.get("seen", 0)) + 1
        if name:
            e["name"] = name
        self._save()

    def purge(self):
        """Drop non-saved contacts idle longer than the TTL."""
        now = time.time()
        removed = []
        for net in ("mc", "mt"):
            for cid, e in list(self.data.get(net, {}).items()):
                if e.get("saved"):
                    continue
                if (now - float(e.get("last_seen", 0))) > self.ttl:
                    del self.data[net][cid]
                    removed.append(f"{net}:{cid}")
        if removed:
            self._save()
        return removed

    def resolve(self, net, key):
        """Resolve a contact by EXACT id or EXACT name (case-insensitive).

        No prefix or substring matching: an approximate match must never
        route a DM to the wrong peer. A leading "@" is ignored.
        """
        key = str(key or "").strip().lstrip("@")
        if not key:
            return None
        table = self.data.get(net, {})
        if key in table:
            return key
        lk = key.lower()
        for cid in table:
            if cid.lower() == lk:
                return cid
        for cid, e in table.items():
            if (e.get("name") or "").strip().lower() == lk:
                return cid
        return None

    def set_saved(self, net, key, saved=True):
        cid = self.resolve(net, key)
        if not cid:
            return None
        self.data[net][cid]["saved"] = bool(saved)
        self._save()
        return cid

    def remove(self, net, key):
        cid = self.resolve(net, key)
        if not cid:
            return None
        del self.data[net][cid]
        self._save()
        return cid

    def listing(self):
        lines = []
        now = time.time()
        for net in ("mc", "mt"):
            for cid, e in sorted(self.data.get(net, {}).items(),
                                 key=lambda kv: kv[1].get("last_seen", 0),
                                 reverse=True):
                age_d = int((now - float(e.get("last_seen", 0))) / 86400)
                flag = "SAVED" if e.get("saved") else f"{age_d}d"
                lines.append(f"{net} {e.get('name') or '-'} [{cid[:12]}] {flag}")
        return lines


class RadioBridge:
    """Owns both radios; exposes inbound callbacks and outbound send."""

    def __init__(self):
        self.meshcore = None
        self.meshtastic = None
        # time-windowed, persistent dedup (prevents relay loops)
        self.dedup = MessageDedup()
        # contact registry (auto-record + 90d expiry + save/forget)
        self.contacts = ContactStore()

    # -- lifecycle ----------------------------------------------------------
    async def connect(self):
        loop = asyncio.get_running_loop()
        print("Connecting MeshCore...")
        self.meshcore = await MeshCore.create_serial(MESHCORE_PORT)
        for _ in range(30):
            if getattr(self.meshcore, "is_connected", False):
                break
            if getattr(self.meshcore, "is_connected", None):
                break
            await asyncio.sleep(0.5)
        try:
            await self.meshcore.ensure_contacts(follow=True)
        except Exception as e:
            print(f"[gateway] ensure_contacts warn: {e}")
        print("Connecting Meshtastic...")
        self.meshtastic = await loop.run_in_executor(
            None, serial_interface.SerialInterface, MESHTASTIC_PORT
        )
        print("Both radios connected.")

    # -- MeshCore inbound ---------------------------------------------------
    @staticmethod
    def callsign_prefix(text: str) -> str:
        """MeshCore channel msgs are anonymous by protocol; identity rides in
        the text prefix, e.g. 'ALPHA 02: msg' / 'BRAVO T114: msg'. Return the
        prefix before the first ':' when it looks like a tag, else ''."""
        if ":" not in text:
            return ""
        pre = text.split(":", 1)[0].strip()
        if len(pre) < 2 or len(pre) > 28 or pre.count(" ") > 3:
            return ""
        return pre

    def mc_callback(self, event):
        try:
            msg = event.payload
            text = msg.get("text", "")
            if not text:
                return
            sender_ts = msg.get("sender_timestamp")
            sender = msg.get("sender_name") or sender_ts or "mc"
            name = msg.get("sender_name") or self.callsign_prefix(text) or (str(sender_ts) if sender_ts else "")
            if not text or text.startswith(BRIDGE_PREFIX):
                return
            # time-windowed loop prevention
            if self.dedup.is_duplicate(sender, text):
                print(f"[mc][dedup] suppressed repeat: {sender}: {text}")
                return
            self.dedup.note(sender, text)
            print(f"[mc] {name or sender}: {text}")
            emit({"net": "mc", "dm": False, "sender": sender, "name": name or None, "text": text})
            if BRIDGE_BOTH_WAYS and self.meshtastic:
                relayed = f"{BRIDGE_PREFIX} {sender}: {text}"
                loop = asyncio.get_event_loop()
                loop.run_in_executor(None, self.meshtastic.sendText, relayed)
                print(f"[bridge] mc->mt: {relayed}")
        except Exception as e:
            print(f"[gateway] mc_cb err: {e}", file=sys.stderr)

    # -- MeshCore direct messages -------------------------------------------
    def _mc_name_for(self, prefix):
        """Best-effort contact name for a MeshCore pubkey prefix."""
        try:
            contacts = getattr(self.meshcore, "contacts", None)
            if isinstance(contacts, dict):
                items = list(contacts.items())
            elif isinstance(contacts, list):
                items = [(c.get("public_key", ""), c) for c in contacts if isinstance(c, dict)]
            else:
                return None
            for pk, c in items:
                if isinstance(c, dict) and str(pk).startswith(str(prefix)):
                    return c.get("adv_name") or c.get("name")
        except Exception:
            pass
        return None

    def mc_dm_callback(self, event):
        """MeshCore DIRECT message (CONTACT_MSG_RECV) -> Telegram only.
        DMs are never bridged to Meshtastic (privacy)."""
        try:
            msg = event.payload
            text = msg.get("text", "")
            if not text:
                return
            sender = msg.get("pubkey_prefix") or "mc-dm"
            name = self._mc_name_for(sender)
            if self.dedup.is_duplicate(f"dm:{sender}", text):
                print(f"[mc:dm][dedup] suppressed repeat: {sender}: {text}")
                return
            self.dedup.note(f"dm:{sender}", text)
            print(f"[mc:dm] {name or sender}: {text}")
            emit({"net": "mc", "dm": True, "sender": sender, "name": name, "text": text})
            self._remember_dm("mc", sender, name)
        except Exception as e:
            print(f"[gateway] mc_dm_cb err: {e}", file=sys.stderr)

    # -- Meshtastic inbound -------------------------------------------------
    def mt_callback(self, packet, interface=None):
        try:
            decoded = packet.get("decoded", {})
            if decoded.get("portnum") != PortNum.TEXT_MESSAGE_APP:
                return
            text = decoded.get("text", "")
            if not text or text.startswith(BRIDGE_PREFIX):
                return
            from_id = packet.get("fromId")
            sender = from_id
            name = from_id
            if self.meshtastic and from_id in self.meshtastic.nodes:
                ni = self.meshtastic.nodes[from_id]
                if "user" in ni and "longName" in ni.get("user", {}):
                    sender = ni["user"]["longName"]
                    name = sender
            mid = packet.get("id")
            # time-windowed loop prevention (key on sender+text, mesh-wide)
            if self.dedup.is_duplicate(sender, text):
                print(f"[mt][dedup] suppressed repeat: {sender}: {text}")
                return
            self.dedup.note(sender, text)
            # Direct message (addressed to us) vs channel broadcast:
            # Meshtastic marks broadcast as toId="^all" / to=0xFFFFFFFF.
            to_id = str(packet.get("toId") or "")
            to_num = packet.get("to")
            is_dm = to_id not in ("", "^all") or (
                isinstance(to_num, int) and to_num not in (0xFFFFFFFF, 0)
            )
            tag = "mt:dm" if is_dm else "mt"
            print(f"[{tag}] {name or sender}: {text}")
            emit({"net": "mt", "dm": is_dm, "sender": sender, "name": name or None, "sender_id": from_id, "text": text})
            if is_dm:
                self._remember_dm("mt", from_id, name)
            # DMs are never bridged to the other network (privacy).
            if BRIDGE_BOTH_WAYS and self.meshcore and not is_dm:
                relayed = f"{BRIDGE_PREFIX} {sender}: {text}"
                asyncio.get_event_loop().create_task(
                    self.meshcore.commands.send_chan_msg(chan=0, msg=relayed)
                )
                print(f"[bridge] mt->mc: {relayed}")
        except Exception as e:
            print(f"[gateway] mt_cb err: {e}", file=sys.stderr)

    # -- subscribe ----------------------------------------------------------
    async def start(self):
        await self.connect()
        self.meshcore.subscribe(
            EventType.CHANNEL_MSG_RECV, self.mc_callback
        )
        self.meshcore.subscribe(
            EventType.CONTACT_MSG_RECV, self.mc_dm_callback
        )
        await self.meshcore.start_auto_message_fetching()
        from pubsub import pub
        loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()

        async def _name_cache_writer():
            """Snapshot Meshtastic node names for mesh_live every 60 s."""
            while True:
                try:
                    mt = {}
                    if self.meshtastic and getattr(self.meshtastic, "nodes", None):
                        for nid, node in list(self.meshtastic.nodes.items())[:800]:
                            u = node.get("user", {}) if isinstance(node, dict) else {}
                            if u.get("longName"):
                                mt[nid] = u["longName"]
                    with open("/tmp/mesh_names_cache.json", "w") as f:
                        json.dump({"mt": mt}, f)
                except Exception:
                    pass
                await asyncio.sleep(60)

        asyncio.create_task(_name_cache_writer())

        async def _contact_purge_task():
            """Purge contacts idle beyond the TTL (saved ones exempt)."""
            while True:
                try:
                    removed = self.contacts.purge()
                    if removed:
                        self._notice(
                            f"purged {len(removed)} stale contact(s) (>{CONTACTS_TTL_DAYS}d idle)"
                        )
                except Exception as e:
                    print(f"[gateway] purge task err: {e}", file=sys.stderr)
                await asyncio.sleep(3600)

        try:
            removed0 = self.contacts.purge()
            if removed0:
                self._notice(f"purged {len(removed0)} stale contact(s) at startup")
        except Exception:
            pass
        asyncio.create_task(_contact_purge_task())
        asyncio.create_task(self._advert_watch_task())

        def _mt_wrap(packet, interface=None):
            loop.call_soon_threadsafe(self.queue.put_nowait, packet)

        pub.subscribe(_mt_wrap, "meshtastic.receive")
        print("[gateway] listeners active.")

        # drain meshtastic queue
        while True:
            pkt = await self.queue.get()
            try:
                self.mt_callback(pkt)
            except Exception as e:
                print(f"[gateway] mt drain err: {e}", file=sys.stderr)

    # -- outbound -----------------------------------------------------------
    async def send_mc(self, text):
        await self.meshcore.commands.send_chan_msg(chan=0, msg=text)
        return True

    async def send_mt(self, text):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.meshtastic.sendText, text)
        return True

    async def send_radio(self, net, text):
        """Send over one or both radios via the gateway's OWN connections."""
        if net == "mc":
            await self.send_mc(text)
        elif net == "mt":
            await self.send_mt(text)
        elif net == "both":
            await self.send_mc(text)
            await self.send_mt(text)

    # -- direct-message reply routing ---------------------------------------
    LAST_DM_FILE = "/tmp/meshgw_last_dm.json"

    def _load_last_dm(self):
        try:
            with open(self.LAST_DM_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

    def _remember_dm(self, net, peer_id, name=None):
        """Record the most recent DM peer per network (for reply routing)."""
        try:
            d = self._load_last_dm()
            d[net] = {"id": peer_id, "name": name, "ts": time.time()}
            with open(self.LAST_DM_FILE, "w") as f:
                json.dump(d, f)
            print(f"[gateway] last DM peer {net} = {peer_id} ({name})")
            try:
                self.contacts.touch(net, peer_id, name)
            except Exception as e:
                print(f"[gateway] contacts touch failed: {e}", file=sys.stderr)
        except Exception as e:
            print(f"[gateway] remember_dm failed: {e}", file=sys.stderr)

    async def send_dm(self, net, text, target=None):
        """Reply as a DIRECT message to the last DM peer (or explicit target)."""
        d = self._load_last_dm()
        if net == "mc":
            tgt = target or (d.get("mc") or {}).get("id")
            if tgt:
                tgt = self.contacts.resolve("mc", tgt) or tgt
            if not tgt:
                print("[gateway] no MeshCore DM peer known; dropped", file=sys.stderr)
                self._notice("MeshCore DM dropped: no target (unknown contact)")
                return False
            await self.meshcore.commands.send_msg(tgt, text)
            print(f"[gateway] mc DM -> {tgt}: {text}")
            self._notice(f"sent MeshCore DM to {tgt}: {text}")
            return True
        if net == "mt":
            tgt = target or (d.get("mt") or {}).get("id")
            if tgt:
                tgt = self.contacts.resolve("mt", tgt) or tgt
            if not tgt:
                print("[gateway] no Meshtastic DM peer known; dropped", file=sys.stderr)
                self._notice("Meshtastic DM dropped: no target (unknown contact)")
                return False
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, lambda: self.meshtastic.sendText(text, destinationId=tgt)
            )
            print(f"[gateway] mt DM -> {tgt}: {text}")
            self._notice(f"sent Meshtastic DM to {tgt}: {text}")
            return True
        print(f"[gateway] send_dm: unknown net {net}", file=sys.stderr)
        return False

    def _notice(self, text):
        """Write an operational notice for the Telegram watcher to forward."""
        try:
            with open(NOTICE_FILE, "a") as f:
                f.write(json.dumps({"net": "sys", "text": text,
                                    "ts": datetime.now(timezone.utc).isoformat()}) + "\n")
        except Exception as e:
            print(f"[gateway] notice write failed: {e}", file=sys.stderr)
        print(f"[notice] {text}")

    def _handle_contact_cmd(self, cmd):
        """Apply a save/forget/list contact command from Telegram."""
        action = (cmd.get("action") or "").lower()
        key = cmd.get("key") or ""
        nets = [cmd["net"]] if cmd.get("net") in ("mc", "mt") else ["mc", "mt"]
        hits = []
        if action in ("save", "keep"):
            for n in nets:
                cid = self.contacts.set_saved(n, key, True)
                if cid:
                    hits.append(f"{n}:{cid}")
            self._notice(f"saved contact(s): {', '.join(hits)}" if hits
                         else f"save: no contact matched '{key}'")
        elif action in ("forget", "remove", "drop"):
            for n in nets:
                cid = self.contacts.remove(n, key)
                if cid:
                    hits.append(f"{n}:{cid}")
            self._notice(f"removed contact(s): {', '.join(hits)}" if hits
                         else f"forget: no contact matched '{key}'")
        elif action in ("list", "contacts"):
            self._notice("contacts: " + (" | ".join(self.contacts.listing()) or "none"))
        else:
            self._notice(f"unknown contact action '{action}'")

    async def _cmd_advert(self, cmd):
        """Send a MeshCore advertisement (flood=True for a flood-route advert)."""
        flood = bool(cmd.get("flood"))
        try:
            await self.meshcore.commands.send_advert(flood=flood)
            self._notice(f"sent MeshCore advert (flood={flood})")
        except Exception as e:
            self._notice(f"MeshCore advert failed: {e}")

    async def _cmd_adverts(self, cmd):
        """Dump MeshCore contacts by most recent advert (answers 'did we hear <node>')."""
        try:
            try:
                await self.meshcore.ensure_contacts(follow=True)
            except Exception:
                pass
            contacts = getattr(self.meshcore, "contacts", {}) or {}
            items = []
            for key, c in contacts.items():
                if not isinstance(c, dict):
                    continue
                try:
                    la = float(c.get("last_advert", 0) or 0)
                except Exception:
                    la = 0.0
                items.append((la, c.get("adv_name"), str(key)[:12]))
            items.sort(key=lambda x: x[0], reverse=True)
            limit = int(cmd.get("limit", 12) or 12)
            now = time.time()
            lines = []
            for la, nm, key in items[:limit]:
                if la > 0:
                    mins = int((now - la) / 60)
                    age = f"{mins}m ago" if mins >= 0 else f"{(int(now-la)//3600)}h ago"
                    if mins > 180:
                        age = f"{mins//60}h ago"
                else:
                    age = "?"
                lines.append(f"{nm or '-'} [{key}] {age}")
            self._notice("MeshCore adverts newest-first: " + (" | ".join(lines) if lines else "none"))
        except Exception as e:
            self._notice(f"adverts dump failed: {e}")

    async def _cmd_find_advert(self, cmd):
        """Find a MeshCore node by advert name (substring, ci) and optionally save it.

        Saving records the node in the contact registry (keyed by its 6-byte
        pubkey prefix, same form DMs use) and pins it (exempt from 90d expiry).
        """
        name = (cmd.get("name") or "").strip().lower()
        do_save = bool(cmd.get("save"))
        try:
            try:
                await self.meshcore.ensure_contacts(follow=True)
            except Exception:
                pass
            contacts = getattr(self.meshcore, "contacts", {}) or {}
            matches = []
            for key, c in contacts.items():
                if not isinstance(c, dict):
                    continue
                nm = c.get("adv_name") or ""
                if name and name in nm.lower():
                    matches.append((nm, str(key)))
            if not matches:
                self._notice(f"advert search '{name}': NOT FOUND among {len(contacts)} contacts")
                return
            for nm, key in matches:
                cid = key[:12]
                if do_save:
                    try:
                        self.contacts.touch("mc", cid, nm)
                        self.contacts.set_saved("mc", cid, True)
                    except Exception as e:
                        self._notice(f"advert save failed for {nm}: {e}")
                        continue
                self._notice(f"advert {'found+saved' if do_save else 'found'}: {nm} [{cid}]")
        except Exception as e:
            self._notice(f"advert search failed: {e}")

    async def _advert_watch_task(self):
        """Watch the MeshCore contact table for WATCH_ADVERTS; save+notify once."""
        if not WATCH_ADVERTS:
            return
        while True:
            try:
                await self.meshcore.ensure_contacts(follow=True)
                contacts = getattr(self.meshcore, "contacts", {}) or {}
                for want in WATCH_ADVERTS:
                    lw = want.lower()
                    for key, c in contacts.items():
                        if not isinstance(c, dict):
                            continue
                        nm = c.get("adv_name") or ""
                        if lw not in nm.lower():
                            continue
                        cid = str(key)[:12]
                        cur = self.contacts.resolve("mc", cid)
                        already = bool(cur and self.contacts.data["mc"].get(cur, {}).get("saved"))
                        if not already:
                            self.contacts.touch("mc", cid, nm)
                            self.contacts.set_saved("mc", cid, True)
                            self._notice(f"advert found+saved on watch: {nm} [{cid}]")
            except Exception as e:
                print(f"[gateway] advert watch err: {e}", file=sys.stderr)
            await asyncio.sleep(300)

    async def watch_send_queue(self):
        """Poll /tmp/meshgw_send.txt for outbound radio commands.
        Format per line: JSON {"net": "mc"|"mt"|"both", "text": "..."}
        The gateway owns the ports, so sends MUST go through here, not via a
        second connection (which would hit a port-lock conflict).
        """
        SEND_FILE = "/tmp/meshgw_send.txt"
        processed = set()  # line-hash dedup
        while True:
            try:
                try:
                    with open(SEND_FILE) as f:
                        lines = [l for l in f.read().splitlines() if l.strip()]
                except FileNotFoundError:
                    lines = []
                for line in lines:
                    h = hash(line)
                    if h in processed:
                        continue
                    processed.add(h)
                    if len(processed) > 500:
                        # drop oldest
                        processed = set(list(processed)[-500:])
                    try:
                        cmd = json.loads(line)
                        if cmd.get("cmd") == "contact":
                            self._handle_contact_cmd(cmd)
                            continue
                        if cmd.get("cmd") == "advert":
                            await self._cmd_advert(cmd)
                            continue
                        if cmd.get("cmd") == "adverts":
                            await self._cmd_adverts(cmd)
                            continue
                        if cmd.get("cmd") == "find_advert":
                            await self._cmd_find_advert(cmd)
                            continue
                        net = cmd.get("net", "")
                        text = cmd.get("text", "")
                        if net and text:
                            if cmd.get("dm"):
                                print(f"[gateway] outbound DM over {net}: {text}")
                                await self.send_dm(net, text, cmd.get("target"))
                            else:
                                print(f"[gateway] outbound send over {net}: {text}")
                                await self.send_radio(net, text)
                    except Exception as e:
                        print(f"[gateway] send-queue parse err: {e}", file=sys.stderr)
                # clear the file after processing so we don't resend
                if lines:
                    open(SEND_FILE, "w").close()
            except Exception as e:
                print(f"[gateway] send-queue err: {e}", file=sys.stderr)
            await asyncio.sleep(2)


# ---------------------------------------------------------------------------
# Minimal headless status endpoint (optional)
# ---------------------------------------------------------------------------

async def status_server(active_since, bridge=None):
    if not PORT_HTTP:
        return
    loop = asyncio.get_running_loop()
    async def handler(reader, writer):
        info = {
            "status": "active",
            "since": active_since.isoformat(),
            "bridge": BRIDGE_BOTH_WAYS,
        }
        if bridge is not None and bridge.dedup is not None:
            info["dedup"] = {
                "tracked": len(bridge.dedup.entries),
                "window_s": bridge.dedup.window_s,
                "hold_s": bridge.dedup.hold_s,
                "max_tracked": bridge.dedup.max_tracked,
            }
        body = json.dumps(info).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body))
        writer.write(body)
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(handler, "127.0.0.1", PORT_HTTP)
    print(f"[gateway] status endpoint http://127.0.0.1:{PORT_HTTP}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    active_since = datetime.now(timezone.utc)
    print("--- Mesh Gateway ---")
    bridge = RadioBridge()
    await asyncio.gather(
        bridge.start(),
        bridge.watch_send_queue(),
        status_server(active_since, bridge),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGateway shutdown.")
    except serial.serialutil.SerialException as e:
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
