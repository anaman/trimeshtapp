#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""rns_bridge.py — RNS <-> MeshCore/Meshtastic bridge (skeleton, 2026-08-18).

Design: see docs/ARCHITECTURE.md
- RNS side: connects to the local RNS transport (rnsd on the RNode leg),
  announces destination "meshbridge.bridge", listens for packets.
- Mesh side: tails /tmp/meshgw_events.jsonl (mesh gateway inbound) -> RNS packets;
  writes /tmp/meshgw_send.txt (mesh gateway outbound) for RNS -> mesh.
- No serial ports owned here: RNS transport (rnsd) owns ttyUSB0, mesh gateway owns
  ACM0/ACM1. This process only uses files + the RNS API.

Payload protocol (RNS): first byte = target (0x01 MC, 0x02 MT, 0x03 both,
0x04 status/echo, 0x05 direct-RNS user message); rest = UTF-8 text.

User-originated sends (rt: / both: from mesh_watch.py) arrive via
/tmp/rns_send.txt; the bridge relays them to RNS peers.

Run: .venv/bin/python rns_bridge.py   (requires rnsd running)
"""
import json, os, sys, time, threading
from pathlib import Path

EVENT_FILE = "/tmp/meshgw_events.jsonl"
SEND_FILE  = "/tmp/meshgw_send.txt"
RNS_SEND_FILE = "/tmp/rns_send.txt"   # user-originated rt:/both: sends from mesh_watch.py
LXMF_INBOUND_FILE = "/tmp/rns_lxmf_inbound.jsonl"  # unprefixed LXMF -> Telegram-only (no radio TX)
OFFSET_FILE = "/tmp/rns_bridge_offset"

# Persistent peer discovery state.  Keep this outside /tmp so service restarts
# do not turn already-seen nodes into fresh notifications again.
BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
LOG_DIR = BASE_DIR / "logs"
PEER_REGISTRY_FILE = STATE_DIR / "rns_peer_registry.json"
PEER_ANNOUNCE_LOG = LOG_DIR / "rns_peer_announces.jsonl"
NEW_PEER_LOG = LOG_DIR / "rns_new_peers.jsonl"
LEGACY_PEER_LOG = Path("/tmp/rns_peers.jsonl")

TARGET_MC, TARGET_MT, TARGET_BOTH, TARGET_STATUS, TARGET_RT = 0x01, 0x02, 0x03, 0x04, 0x05

# Optional JSON config (lora dashboard writes this). All keys fall back to
# the historical defaults below, so an absent file changes nothing.
CONFIG_FILE = Path(__file__).resolve().parent / "rns_bridge.json"
DEFAULT_CFG = {
    "announce_app": "meshbridge",
    "announce_name": "bridge",
    "reannounce_interval_s": 60,
    "announce_enabled": True,
    "lxmf_enabled": True,
}

def load_bridge_config():
    cfg = dict(DEFAULT_CFG)
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text())
            for k in DEFAULT_CFG:
                if k in data:
                    cfg[k] = data[k]
    except Exception as e:
        print(f"[rns] config load failed ({CONFIG_FILE}): {e}", file=sys.stderr)
    return cfg

# ---------------------------------------------------------------- RNS side
try:
    import RNS
except ImportError:
    RNS = None


class _AnnounceHandler:
    """RNS 1.4.2 announce handler object (aspect_filter=None = receive all announces)."""
    aspect_filter = None

    def __init__(self, owner):
        self.owner = owner

    def received_announce(self, destination_hash, announced_identity, app_data=None, **kwargs):
        self.owner.on_peer_announce(destination_hash, announced_identity, app_data)


class BridgeApp:
    def __init__(self):
        self.destination = None
        self.peers = {}  # hash_hex -> (RNS.Identity, app, aspects, last_seen)
        self.lxmf_peers = {}  # hash_hex -> (identity, last_seen) — Sideband/LXMF users
        self.lxmf = None
        self.lxmf_delivery_hash = None
        self.last_offset = self._load_offset()
        self.known_peer_hashes = self._load_known_peer_hashes()

    def _load_offset(self):
        try:
            return int(open(OFFSET_FILE).read().strip() or 0)
        except Exception:
            return 0

    def _save_offset(self, off):
        open(OFFSET_FILE, "w").write(str(off))

    def _load_known_peer_hashes(self):
        """Load persistent peer registry, seeding it from the legacy /tmp log
        on first run so old peers do not generate fresh notifications after a
        bridge restart."""
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        known = set()
        try:
            data = json.loads(PEER_REGISTRY_FILE.read_text())
            if isinstance(data, dict):
                known.update(str(h) for h in data.get("known_hashes", []))
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[rns] peer registry load failed: {e}", file=sys.stderr)
        if not known and LEGACY_PEER_LOG.exists():
            try:
                with LEGACY_PEER_LOG.open() as f:
                    for line in f:
                        try:
                            ev = json.loads(line)
                            h = ev.get("hash")
                            if h:
                                known.add(str(h))
                        except Exception:
                            continue
                if known:
                    self._save_known_peer_hashes(known)
                    print(f"[rns] seeded persistent peer registry from legacy log: {len(known)} peers")
            except Exception as e:
                print(f"[rns] legacy peer registry seed failed: {e}", file=sys.stderr)
        return known

    def _save_known_peer_hashes(self, known=None):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        hashes = sorted(known if known is not None else self.known_peer_hashes)
        tmp = PEER_REGISTRY_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"updated_ts": time.time(), "known_hashes": hashes}, indent=1) + "\n")
        tmp.replace(PEER_REGISTRY_FILE)

    def _append_peer_event(self, path, event):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(event, sort_keys=True) + "\n")

    # ---- RNS callbacks -------------------------------------------------
    def start_rns(self):
        if RNS is None:
            print("RNS not importable — run inside the rns-bridge venv with rns installed")
            return False
        RNS.Reticulum()
        cfg = load_bridge_config()
        announce_app = str(cfg.get("announce_app", "meshbridge"))[:32]
        announce_name = str(cfg.get("announce_name", "bridge"))[:32]
        reannounce_s = max(5, min(int(cfg.get("reannounce_interval_s", 60)), 86400))
        announce_enabled = bool(cfg.get("announce_enabled", True))
        lxmf_enabled = bool(cfg.get("lxmf_enabled", True))
        # persistent identity so the bridge keeps a stable hash across restarts
        import os
        ID_FILE = os.path.expanduser("~/.reticulum/meshbridge_identity")
        identity = None
        if os.path.exists(ID_FILE):
            try:
                identity = RNS.Identity.from_bytes(open(ID_FILE, "rb").read())
            except Exception as e:
                print(f"[rns] identity load failed: {e}")
        if identity is None:
            identity = RNS.Identity()
            open(ID_FILE, "wb").write(identity.get_private_key())
        self.identity = identity
        self.destination = RNS.Destination(
            identity, RNS.Destination.IN, RNS.Destination.SINGLE, announce_app, announce_name
        )
        self.destination.set_link_established_callback(self.link_established)
        self.destination.set_packet_callback(self.packet_callback)
        # announce so peers can find the bridge (and re-announce periodically)
        if announce_enabled:
            self.destination.announce()
        def _reannounce():
            while True:
                time.sleep(reannounce_s)
                try:
                    if announce_enabled:
                        self.destination.announce()
                    if lxmf_enabled and self.lxmf is not None and self.lxmf_delivery_hash is not None:
                        self.lxmf.announce(self.lxmf_delivery_hash)
                except Exception:
                    pass
        threading.Thread(target=_reannounce, daemon=True).start()
        RNS.Transport.register_announce_handler(_AnnounceHandler(self))
        print(f"[rns] destination announced: {self.destination.hash.hex()}")

        # Periodic status dump for the lora dashboard (in-process transport
        # state is invisible to external tools like rnstatus).
        def _status_loop():
            while True:
                time.sleep(30)
                try:
                    ifaces = []
                    for i in getattr(RNS.Transport, "interfaces", []) or []:
                        ifaces.append({
                            "name": str(getattr(i, "name", "?")),
                            "type": type(i).__name__,
                            "online": bool(getattr(i, "online", False)),
                            "rxb": int(getattr(i, "rxb", 0)),
                            "txb": int(getattr(i, "txb", 0)),
                        })
                    status = {
                        "ts": time.time(),
                        "destination_hash": self.destination.hash.hex(),
                        "peers": len(self.peers),
                        "lxmf_peers": len(self.lxmf_peers),
                        "interfaces": ifaces,
                    }
                    tmp = Path("/tmp/rns_bridge_status.json.tmp")
                    tmp.write_text(json.dumps(status) + "\n")
                    tmp.replace(Path("/tmp/rns_bridge_status.json"))
                except Exception:
                    pass
        threading.Thread(target=_status_loop, daemon=True).start()
        # LXMF layer (Sideband compatibility): the bridge is an LXMF node
        if lxmf_enabled:
            try:
                from LXMF import LXMRouter
                lxmf_dir = os.path.expanduser("~/.reticulum/lxmf")
                os.makedirs(lxmf_dir, exist_ok=True)
                self.lxmf = LXMRouter(identity=identity, storagepath=lxmf_dir, name="MeshBridge")
                self.lxmf.register_delivery_identity(identity, display_name="MeshBridge")
                self.lxmf.register_delivery_callback(self.on_lxmf_message)
                self.lxmf_delivery_hash = next(iter(self.lxmf.delivery_destinations))
                if announce_enabled:
                    self.lxmf.announce(self.lxmf_delivery_hash)
                print(f"[lxmf] delivery identity registered (Sideband address: {self.lxmf_delivery_hash.hex()})")
            except Exception as e:
                print(f"[lxmf] init failed: {e}")
                self.lxmf = None
        else:
            self.lxmf = None
            print("[rns] LXMF disabled by config")
        return True

    def link_established(self, link):
        print(f"[rns] link established: {link}")

    def announce_handler(self, aspect_filter, destination_hash, remote_identity, app_data):
        """Legacy plain-callback form (NOT registered by RNS 1.4.2 — kept for clarity).
        Real registration uses _AnnounceHandler below."""
        pass

    def on_peer_announce(self, destination_hash, announced_identity, app_data=None):
        """Called by _AnnounceHandler for every announce received by the transport.

        Every announce is logged. Only first-ever peer hashes are written to the
        new-peer log consumed by the hourly notification summary.
        """
        h = destination_hash.hex()
        known_runtime = h in self.peers
        known_persistent = h in self.known_peer_hashes
        app, aspects = "meshbridge", ["bridge"]
        app_data_preview = bytes(app_data or b"")[:48]
        print(f"[rns] announce raw: {h[:16]}... type={type(announced_identity).__name__} idhash={announced_identity.hexhash[:12] if hasattr(announced_identity, 'hexhash') else announced_identity} app_data={app_data_preview!r}")
        # LXMF delivery destinations announce with msgpack app_data (display name)
        lxmf_peer = False
        peer_name = None
        try:
            if isinstance(app_data, (bytes, bytearray)) and app_data:
                import msgpack
                ad = msgpack.unpackb(bytes(app_data), raw=False)
                peer_name = ad[0] if isinstance(ad, (list, tuple)) and ad else (
                    ad.get("name") if isinstance(ad, dict) else None)
                if isinstance(peer_name, (bytes, bytearray)):
                    peer_name = peer_name.decode("utf-8", "replace")
                if peer_name:
                    lxmf_peer = True
        except Exception:
            pass
        try:
            if isinstance(app_data, (bytes, bytearray)) and not lxmf_peer:
                ad = json.loads(app_data.decode("utf-8", "replace"))
                if isinstance(ad, dict) and ad.get("app") and isinstance(ad.get("aspects"), list):
                    app, aspects = ad["app"], ad["aspects"]
                    peer_name = ad.get("name") or peer_name
        except Exception:
            pass
        self.peers[h] = (announced_identity, app, aspects, time.time())
        if lxmf_peer:
            self.lxmf_peers[h] = (announced_identity, time.time())
            print(f"[lxmf] peer (Sideband/LXMF): {h[:16]}...")
        else:
            print(f"[rns] announce (peer): {h[:16]}...")

        event = {
            "ts": time.time(),
            "hash": h,
            "hash_short": h[:16],
            "lxmf": lxmf_peer,
            "app": app,
            "aspects": aspects,
            "name": peer_name,
            "known_runtime": known_runtime,
            "known_persistent": known_persistent,
        }

        # Only genuinely first-seen peers are persisted: appended to the new-peer
        # log (the "log of found nodes") and the legacy /tmp file for old tailers.
        # The hourly summary script consumes these logs at its own offsets, so a
        # hash already in the registry never gets re-notified.
        if not known_persistent:
            self.known_peer_hashes.add(h)
            try:
                self._save_known_peer_hashes()
                self._append_peer_event(NEW_PEER_LOG, event | {"new": True})
                # Preserve the old lightweight file for any existing tailers, but
                # only for genuinely first-seen peers.
                with LEGACY_PEER_LOG.open("a") as f:
                    f.write(json.dumps({"ts": event["ts"], "hash": h, "lxmf": lxmf_peer}) + "\n")
            except Exception as e:
                print(f"[rns] new peer notice write failed: {e}", file=sys.stderr)

    def on_lxmf_message(self, message):
        """Inbound LXMF (Sideband) message -> route.

        Design rule (2026-08-25): unprefixed LXMF traffic defaults to
        bot/automation replies -> deliver to Telegram ONLY, never transmit on
        MeshCore/Meshtastic (and never re-propagate on RNS). Explicit
        "MC:" / "MT:" / "BOTH:" prefixes still route to the radios
        (deliberate human addressing).
        """
        try:
            content = message.content or b""
            if isinstance(content, (bytes, bytearray)):
                content = content.decode("utf-8", "replace")
            src = message.source_hash.hex() if message.source_hash else "?"
            self.lxmf_peers[src] = (message.source, time.time())
            # routing prefixes: "MC:" / "MT:" / "BOTH:" (default: telegram-only)
            net = "tg"
            text = content.strip()
            for prefix, n in (("MC:", "mc"), ("MT:", "mt"), ("BOTH:", "both")):
                if text.upper().startswith(prefix):
                    net = n
                    text = text[len(prefix):].strip()
                    break
            print(f"[lxmf] <- from {src[:16]}... net={net} text={text[:80]}")
            if not text:
                return
            if net == "tg":
                self._deliver_telegram_only(src, text)
            else:
                self._send_to_mesh(net, text)
        except Exception as e:
            print(f"[lxmf] message handling failed: {e}", file=sys.stderr)

    def _deliver_telegram_only(self, src, text):
        """Unprefixed LXMF: append to the Telegram-delivery log (mesh_watch
        tails this file and announces to Telegram). NO radio / RNS TX."""
        try:
            with open(LXMF_INBOUND_FILE, "a") as f:
                f.write(json.dumps({"ts": time.time(), "src": src, "text": text}) + "\n")
            print(f"[bridge] -> telegram-only (no radio TX): {text[:60]}")
        except Exception as e:
            print(f"[bridge] telegram-only write failed: {e}", file=sys.stderr)

    def packet_callback(self, *args):
        payload = args[0]
        if hasattr(payload, "data"):
            payload = payload.data
        if not isinstance(payload, (bytes, bytearray)) or len(payload) == 0:
            return
        target = payload[0]
        text = payload[1:].decode("utf-8", "replace") if len(payload) > 1 else ""
        print(f"[rns] <- packet target=0x{target:02x} text={text[:80]}")
        net = {TARGET_MC: "mc", TARGET_MT: "mt", TARGET_BOTH: "both"}.get(target)
        if net and text:
            self._send_to_mesh(net, text)

    def _send_to_mesh(self, net, text):
        line = json.dumps({"net": net, "text": text}) + "\n"
        with open(SEND_FILE, "a") as f:
            f.write(line)
        print(f"[bridge] -> meshgw_send.txt: {net}: {text[:60]}")

    # ---- Mesh -> RNS ---------------------------------------------------
    def mesh_watcher(self):
        seen = {}
        while True:
            try:
                size = os.path.getsize(EVENT_FILE)
                if size < self.last_offset:
                    self.last_offset = 0  # file truncated/rotated
                with open(EVENT_FILE) as f:
                    f.seek(self.last_offset)
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except Exception:
                            continue
                        key = f"{ev.get('ts','')}|{ev.get('net','')}|{ev.get('text','')}"
                        if key in seen:
                            continue
                        seen[key] = 1
                        if len(seen) > 500:
                            seen.pop(next(iter(seen)))
                        self._mesh_event_to_rns(ev)
                    self.last_offset = f.tell()
                self._save_offset(self.last_offset)
            except FileNotFoundError:
                pass
            except Exception as e:
                print(f"[bridge] watcher error: {e}")
            time.sleep(2)

    def _mesh_event_to_rns(self, ev):
        net = ev.get("net")
        text = ev.get("text", "")
        sender = ev.get("sender") or ev.get("from") or ""
        target = TARGET_MC if net == "mc" else TARGET_MT if net == "mt" else None
        if target is None or not text:
            return
        prefix = "MC" if target == TARGET_MC else "MT"
        payload = bytes([target]) + f"{prefix}:{sender}: {text}".encode()
        self._send_rns(payload)

    def _send_rns(self, payload):
        """Deliver a mesh-originated packet to RNS peers (never to ourselves —
        that would loop back into the mesh). Prefers LXMF (Sideband) peers."""
        # LXMF path: send as a Sideband-compatible message to the latest LXMF peer
        if self.lxmf is not None and self.lxmf_peers:
            try:
                from LXMF import LXMessage
                h, (identity, ts) = max(self.lxmf_peers.items(), key=lambda kv: kv[1][1])
                text = payload[1:].decode("utf-8", "replace")
                print(f"[lxmf] peer identity type={type(identity).__name__} hash={identity.hash.hex()[:12] if hasattr(identity,'hash') else '?'}")
                dest = RNS.Destination(identity, RNS.Destination.OUT,
                                       RNS.Destination.SINGLE, "lxmf", "delivery")
                print(f"[lxmf] dest ok {dest.hash.hex()[:12]}...")
                src = next(iter(self.lxmf.delivery_destinations.values()))
                print(f"[lxmf] src ok {src.hash.hex()[:12]}...")
                msg = LXMessage(dest, src, text)
                print("[lxmf] LXMessage ok")
                self.lxmf.handle_outbound(msg)
                print(f"[lxmf] -> peer {h[:16]}...: {text[:60]}")
                return
            except Exception as e:
                print(f"[lxmf] send failed at: {e}", file=sys.stderr)
        if not self.peers:
            print("[bridge] mesh->RNS: no RNS peers known; packet held for next peer")
            return
        h, (identity, app, aspects, ts) = max(self.peers.items(), key=lambda kv: kv[1][3])
        dest = RNS.Destination(identity, RNS.Destination.OUT, RNS.Destination.SINGLE,
                               app, *aspects)
        if not RNS.Transport.has_path(dest.hash):
            RNS.Transport.request_path(dest.hash)
        RNS.Packet(dest, payload).send()
        print(f"[bridge] -> rns peer {h[:16]}...: {payload[:60]!r}")

    # ---- User sends (rt: / both:) -> RNS ----------------------------------
    def rns_send_watcher(self):
        """Poll /tmp/rns_send.txt for user-originated sends (mesh_watch.py
        writes {"net": "rt"|"both", "text": ...}); relay to RNS peers."""
        processed = set()
        while True:
            try:
                try:
                    with open(RNS_SEND_FILE) as f:
                        lines = [l for l in f.read().splitlines() if l.strip()]
                except FileNotFoundError:
                    lines = []
                for line in lines:
                    h = hash(line)
                    if h in processed:
                        continue
                    processed.add(h)
                    if len(processed) > 500:
                        processed = set(list(processed)[-500:])
                    try:
                        cmd = json.loads(line)
                    except Exception:
                        continue
                    net = cmd.get("net", "rt")
                    text = (cmd.get("text") or "").strip()
                    if not text:
                        continue
                    target = TARGET_BOTH if net == "both" else TARGET_RT
                    self._send_rns(bytes([target]) + text.encode("utf-8"))
                    print(f"[bridge] user send -> RNS (net={net}): {text[:60]}")
                if lines:
                    open(RNS_SEND_FILE, "w").close()  # consumed
            except Exception as e:
                print(f"[bridge] rns-send watcher err: {e}", file=sys.stderr)
            time.sleep(2)

    # ---- main loop -----------------------------------------------------
    def run(self):
        if not self.start_rns():
            sys.exit(1)
        t = threading.Thread(target=self.mesh_watcher, daemon=True)
        t.start()
        t2 = threading.Thread(target=self.rns_send_watcher, daemon=True)
        t2.start()
        print("[bridge] running (RNS listener + mesh watcher + rns-send watcher). Ctrl-C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[bridge] stopped")


if __name__ == "__main__":
    BridgeApp().run()
