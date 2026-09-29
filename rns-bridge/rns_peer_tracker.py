#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""rns_peer_tracker.py — RNS peer tracker subprocess (SQLite-backed).

Turns every discovered RNS peer hash into a unique token ID stored in SQLite,
dedupes by hash, maintains a rolling "unique peers found in the last <period>"
count, and writes throttled notices (configurable period: seconds/minutes/
hours/days) for the notification automation to deliver.

Modes
-----
  (default)  run as a daemon: tail peer logs, maintain DB, write status +
             notice files continuously.
  --once     process pending log data once, then exit (for manual runs/tests).
  --status   print JSON status (same as /tmp/rns_peer_tracker_status.json).
  --check    print the pending notice file (if any) and remove it; else print
             nothing. Used by the notification cron so notices are delivered
             exactly once.

Config: state/rns_peer_tracker.json
  {"enabled": true, "period_value": 1, "period_unit": "days"}
  period_unit in {seconds, minutes, hours, days}. Changes apply on the next
  daemon loop — no restart needed.

DB: state/rns_peers.db
  peers(token_id INTEGER PK AUTOINCREMENT, peer_hash TEXT UNIQUE,
        hash_short TEXT, lxmf INT, first_seen REAL, last_seen REAL,
        seen_count INT)
  meta(key TEXT PK, value TEXT)   — source offsets + last_notify_ts

Status: /tmp/rns_peer_tracker_status.json
Notice: /tmp/rns_peer_notice.txt  (one line; consumed by --check)
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from email.utils import formatdate
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
STATE_DIR = BASE / "state"
LOG_DIR = BASE / "logs"
CONFIG_FILE = STATE_DIR / "rns_peer_tracker.json"
DB_FILE = STATE_DIR / "rns_peers.db"
STATUS_FILE = Path("/tmp/rns_peer_tracker_status.json")
NOTICE_FILE = Path("/tmp/rns_peer_notice.txt")

SOURCES = [
    str(LOG_DIR / "rns_new_peers.jsonl"),
    "/tmp/rns_peers.jsonl",  # legacy compatibility log
]
TZ = ZoneInfo(os.environ.get("RNS_TRACKER_TZ", "UTC"))
UNIT_SECONDS = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}

# Daily email delivery (run `--daily` from cron at your preferred hour).
EMAIL_TO = os.environ.get("RNS_TRACKER_EMAIL", "you@example.com")
EMAIL_FROM = os.environ.get("RNS_TRACKER_EMAIL_FROM", "RNS Node Monitor <you@example.com>")
EMAIL_FROM_ADDR = os.environ.get("RNS_TRACKER_FROM_ADDR", "you@example.com")
EMAIL_SUBJECT = "RNS Node update"
SENDMAIL = "/usr/sbin/sendmail"

DEFAULT_CONFIG = {"enabled": True, "period_value": 1, "period_unit": "days"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS peers (
    token_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_hash  TEXT NOT NULL UNIQUE,
    hash_short TEXT NOT NULL,
    lxmf       INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    seen_count INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# ---------------------------------------------------------------- helpers

def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def atomic_write_json(path: Path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    os.chmod(tmp, mode)
    tmp.replace(path)


def period_seconds(cfg: dict) -> int:
    unit = str(cfg.get("period_unit", "days")).lower()
    if unit not in UNIT_SECONDS:
        unit = "days"
    value = max(1, int(cfg.get("period_value", 1) or 1))
    return value * UNIT_SECONDS[unit]


def period_label(cfg: dict) -> str:
    unit = str(cfg.get("period_unit", "days")).lower()
    if unit not in UNIT_SECONDS:
        unit = "days"
    value = max(1, int(cfg.get("period_value", 1) or 1))
    return f"{value} {unit if value == 1 else unit}"


# ---------------------------------------------------------------- db

def connect() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_FILE), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(_SCHEMA)
    db.commit()
    return db


def meta_get(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(db, key, value):
    db.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    db.commit()


def upsert_peer(db, peer_hash: str, lxmf: bool, ts: float | None = None) -> bool:
    """Insert or touch a peer. Returns True if it was a NEW unique peer."""
    peer_hash = peer_hash.strip().lower()
    if not peer_hash:
        return False
    now = time.time()
    first_seen = float(ts or now)
    cur = db.execute(
        "INSERT INTO peers(peer_hash, hash_short, lxmf, first_seen, last_seen, seen_count) "
        "VALUES(?, ?, ?, ?, ?, 1) "
        "ON CONFLICT(peer_hash) DO UPDATE SET "
        "last_seen=excluded.last_seen, seen_count=peers.seen_count+1",
        (peer_hash, peer_hash[:16], 1 if lxmf else 0, first_seen, now),
    )
    db.commit()
    return cur.rowcount == 1  # 1 => inserted (new); 0 => updated (seen)


def ingest_events(db, cfg) -> int:
    """Consume new events from all sources (offset-tracked). Returns # new peers."""
    new_count = 0
    for src in SOURCES:
        path = Path(src)
        if not path.exists():
            continue
        key = f"offset:{src}"
        try:
            offset = int(meta_get(db, key, 0) or 0)
        except Exception:
            offset = 0
        size = path.stat().st_size
        if offset > size:
            offset = 0  # truncated/rotated
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
                if upsert_peer(db, str(h), bool(ev.get("lxmf", False)), ev.get("ts")):
                    new_count += 1
            meta_set(db, key, f.tell())
    return new_count


# ---------------------------------------------------------------- status / notice

def build_status(db, cfg) -> dict:
    psec = period_seconds(cfg)
    since = time.time() - psec
    total = db.execute("SELECT COUNT(*) c FROM peers").fetchone()["c"]
    unique_period = db.execute(
        "SELECT COUNT(*) c FROM peers WHERE first_seen >= ?", (since,)
    ).fetchone()["c"]
    lxmf_period = db.execute(
        "SELECT COUNT(*) c FROM peers WHERE first_seen >= ? AND lxmf=1", (since,)
    ).fetchone()["c"]
    last_notify = float(meta_get(db, "last_notify_ts", 0) or 0)
    return {
        "updated_ts": time.time(),
        "enabled": bool(cfg.get("enabled", True)),
        "period_seconds": psec,
        "period_label": period_label(cfg),
        "total_unique": total,
        "unique_last_period": unique_period,
        "lxmf_last_period": lxmf_period,
        "rns_last_period": unique_period - lxmf_period,
        "last_notify_ts": last_notify,
        "notice_due": (
            bool(cfg.get("enabled", True))
            and (time.time() - last_notify) >= psec
            and unique_period > 0
        ),
    }


def write_notice_if_due(db, cfg) -> bool:
    """Write the throttled notice file once per period. Returns True if written."""
    if not cfg.get("enabled", True):
        return False
    psec = period_seconds(cfg)
    last_notify = float(meta_get(db, "last_notify_ts", 0) or 0)
    if (time.time() - last_notify) < psec:
        return False
    since = time.time() - psec
    n = db.execute(
        "SELECT COUNT(*) c, SUM(lxmf) l FROM peers WHERE first_seen >= ?", (since,)
    ).fetchone()
    unique = int(n["c"] or 0)
    lxmf = int(n["l"] or 0)
    if unique <= 0:
        return False
    label = period_label(cfg)
    line = (
        f"Reticulum: {unique} unique peer(s) found in the last {label} "
        f"({lxmf} LXMF / {unique - lxmf} RNS)."
    )
    NOTICE_FILE.write_text(line + "\n")
    meta_set(db, "last_notify_ts", time.time())
    return True


def send_email(subject: str, body: str) -> bool:
    """Send via local postfix relay. Returns True on success."""
    try:
        msg = (
            f"From: {EMAIL_FROM}\n"
            f"To: {EMAIL_TO}\n"
            f"Subject: {subject}\n"
            f"Date: {formatdate(localtime=True)}\n\n"
            f"{body}\n"
        )
        subprocess.run(
            [SENDMAIL, "-f", EMAIL_FROM_ADDR, EMAIL_TO],
            input=msg.encode("utf-8"), timeout=60, check=True,
        )
        return True
    except Exception as e:
        print(f"[tracker] email send failed: {e}", file=sys.stderr)
        return False


def cmd_daily(cfg):
    """Forced daily digest: count new unique peers in the last period, email
    the summary (Subject: 'RNS Node update'), print it for the Telegram cron,
    and reset the notify clock so the daemon does not double-fire. Prints
    nothing when no new peers were seen (silent day -> no Telegram, no email).
    Set env RNS_DAILY_NOEMAIL=1 to suppress the email (dry run)."""
    db = connect()
    try:
        NOTICE_FILE.unlink(missing_ok=True)  # drop any stale daemon notice
        psec = period_seconds(cfg)
        since = time.time() - psec
        row = db.execute(
            "SELECT COUNT(*) c, SUM(lxmf) l FROM peers WHERE first_seen >= ?",
            (since,),
        ).fetchone()
        unique = int(row["c"] or 0)
        lxmf = int(row["l"] or 0)
        if unique <= 0:
            return  # quiet day
        label = period_label(cfg)
        ts = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime())
        line = (
            f"RNS Node update ({ts}): {unique} new unique peer(s) seen in "
            f"the last {label} ({lxmf} LXMF / {unique - lxmf} RNS)."
        )
        if os.environ.get("RNS_DAILY_NOEMAIL") != "1":
            send_email(EMAIL_SUBJECT, line)
        meta_set(db, "last_notify_ts", time.time())
        print(line)
    finally:
        db.close()


# ---------------------------------------------------------------- modes

def run_once(cfg):
    db = connect()
    try:
        new = ingest_events(db, cfg)
        write_notice_if_due(db, cfg)
        st = build_status(db, cfg)
        st["new_ingested"] = new
        atomic_write_json(STATUS_FILE, st)
        return st
    finally:
        db.close()


def run_daemon(cfg):
    db = connect()
    try:
        print(f"[tracker] daemon start: {DB_FILE} (period={period_label(cfg)})", flush=True)
        while True:
            try:
                cfg = read_json(CONFIG_FILE, DEFAULT_CONFIG)
                ingest_events(db, cfg)
                write_notice_if_due(db, cfg)
                atomic_write_json(STATUS_FILE, build_status(db, cfg))
            except Exception as e:
                print(f"[tracker] loop error: {e}", file=sys.stderr, flush=True)
            time.sleep(10)
    finally:
        db.close()


def cmd_status(cfg):
    db = connect()
    try:
        st = build_status(db, cfg)
        print(json.dumps(st, indent=1, sort_keys=True))
    finally:
        db.close()


def cmd_check(cfg):
    """Print pending notice once, then remove it. Silent when nothing pending."""
    if NOTICE_FILE.exists():
        text = NOTICE_FILE.read_text().strip()
        if text:
            NOTICE_FILE.unlink(missing_ok=True)
            print(text)


def main():
    cfg = read_json(CONFIG_FILE, DEFAULT_CONFIG)
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--once":
        run_once(cfg)
    elif arg == "--status":
        cmd_status(cfg)
    elif arg == "--check":
        cmd_check(cfg)
    elif arg == "--daily":
        cmd_daily(cfg)
    else:
        run_daemon(cfg)


if __name__ == "__main__":
    main()
