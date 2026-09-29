#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
mesh_live.py — unified MeshCore + Meshtastic live feed for the browser.

Tails /tmp/meshgw_events.jsonl (written by mesh_gateway.py, one JSON object
per line: {"net": "mc"|"mt", "sender": <int>, "text": "...", "ts": "ISO"})
and serves:
   GET /            -> live dashboard (HTML, auto-updates via SSE)
   GET /api/history -> last N events as JSON
   GET /api/stream  -> Server-Sent Events stream (new events as they land)
   GET /api/names   -> sender-name mapping (JSON)
   POST /api/names  -> {"key": "mc:123456", "name": "..."} to label a sender

Names persist in names.json next to this file. Key format: <net>:<sender>.
"""

import json
import os
import time
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
EVENT_FILE = "/tmp/meshgw_events.jsonl"
NAMES_FILE = os.path.join(BASE, "mesh_live_names.json")
HISTORY = 300            # events kept in memory
SSE_KEEPALIVE_S = 20

# ------------------------------------------------------------------ state
_lock = threading.Lock()
_events = []                 # newest last, bounded to HISTORY
_names = {}                  # "mc:123" -> "ALPHA", "mt:456" -> "BRAVO"
_last_pos = 0                # byte offset in EVENT_FILE
_listeners = set()           # set of queue.Queue for SSE


def load_names():
    global _names
    try:
        with open(NAMES_FILE) as f:
            _names = json.load(f)
    except Exception:
        _names = {}


def save_names():
    tmp = NAMES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_names, f, indent=2, sort_keys=True)
    os.replace(tmp, NAMES_FILE)


def fmt_time(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M:%S")
    except Exception:
        return iso


_mt_names = {}          # meshtastic node id ("!hex") -> long name (gateway cache)
_cache_mtime = 0.0


def _load_mt_cache():
    global _mt_names, _cache_mtime
    try:
        m = os.path.getmtime("/tmp/mesh_names_cache.json")
        if m == _cache_mtime:
            return
        with open("/tmp/mesh_names_cache.json") as f:
            d = json.load(f)
        _mt_names = d.get("mt", {}) or {}
        _cache_mtime = m
    except FileNotFoundError:
        pass
    except Exception:
        pass


def callsign(text: str) -> str:
    """MeshCore identity rides in the text prefix: 'N0CALL: hello' -> 'N0CALL'."""
    if ":" not in text:
        return ""
    pre = text.split(":", 1)[0].strip()
    if len(pre) < 2 or len(pre) > 28 or pre.count(" ") > 3:
        return ""
    return pre


def display_name(ev):
    """Resolve a display label: manual override > gateway-resolved name >
    Meshtastic node cache > MeshCore text-prefix callsign > raw sender."""
    net = ev.get("net")
    sender = ev.get("sender")
    key = f"{net}:{sender}"
    if key in _names:
        return _names[key]
    n = ev.get("name")
    if n:
        return n
    _load_mt_cache()
    if net == "mt":
        sid = ev.get("sender_id") or sender
        if sid in _mt_names:
            return _mt_names[sid]
    if net == "mc":
        p = callsign(ev.get("text", ""))
        if p:
            return p
    return str(sender) if sender is not None else "?"


def tail_events():
    """Read new lines appended to EVENT_FILE and broadcast them."""
    global _last_pos
    while True:
        try:
            with open(EVENT_FILE) as f:
                f.seek(_last_pos)
                lines = f.readlines()
                _last_pos = f.tell()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                ev["_t"] = fmt_time(ev.get("ts", ""))
                ev["_name"] = display_name(ev)
                with _lock:
                    _events.append(ev)
                    if len(_events) > HISTORY:
                        del _events[:-HISTORY]
                    payload = json.dumps(ev)
                    for q in list(_listeners):
                        q.put(payload)
        except FileNotFoundError:
            pass
        except Exception:
            pass
        time.sleep(0.5)


# ---------------------------------------------------------------- http
PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mesh Live — MeshCore + Meshtastic</title>
<style>
 :root{--bg:#0d1117;--panel:#161b22;--line:#21262d;--ink:#e6edf3;--mut:#8b949e;
       --mc:#58a6ff;--mt:#3fb950;--accent:#d29922}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);
   font:14px/1.5 ui-monospace,Menlo,Consolas,monospace}
 header{position:sticky;top:0;background:var(--panel);border-bottom:1px solid var(--line);
   padding:10px 16px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
 header h1{font-size:16px;margin:0} .dot{width:9px;height:9px;border-radius:50%;
   background:#f85149;display:inline-block} .dot.ok{background:#3fb950}
 .btn{border:1px solid var(--line);background:#21262d;color:var(--ink);border-radius:6px;
   padding:3px 10px;cursor:pointer;font:inherit;font-size:12px}
 .btn.on{background:var(--accent);color:#111} .cnt{color:var(--mut);font-size:12px}
 #feed{max-width:1000px;margin:14px auto;padding:0 12px 60px}
 .msg{border:1px solid var(--line);border-left:3px solid var(--mut);border-radius:8px;
   background:var(--panel);padding:8px 12px;margin:8px 0}
 .msg.mc{border-left-color:var(--mc)} .msg.mt{border-left-color:var(--mt)}
 .msg .top{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
 .tag{font-size:10px;font-weight:700;padding:1px 7px;border-radius:999px;letter-spacing:.06em}
 .tag.mc{background:#1f3a5f;color:var(--mc)} .tag.mt{background:#173a24;color:var(--mt)}
 .who{font-weight:700;color:var(--ink)} .when{color:var(--mut);font-size:11px;margin-left:auto}
 .text{margin-top:4px;white-space:pre-wrap;word-break:break-word}
 .empty{color:var(--mut);text-align:center;margin-top:60px}
</style></head><body>
<header>
 <h1>📡 Mesh Live</h1><span id="gw" class="cnt">gateway: …</span>
 <span id="conn" class="cnt">feed: connecting…</span>
 <span class="cnt" id="counts"></span>
 <span style="flex:1"></span>
 <button class="btn" id="fAll">all</button>
 <button class="btn on" id="fMC">MeshCore</button>
 <button class="btn on" id="fMT">Meshtastic</button>
 <button class="btn" id="pause">⏸ pause</button>
 <button class="btn" id="clear">clear view</button>
</header>
<div id="feed"><div class="empty">waiting for traffic…</div></div>
<script>
const feed=document.getElementById('feed');let paused=false;const filt={mc:true,mt:true};
let lastTs=0;
function el(e){const d=document.createElement('div');d.className='msg '+e.net;
 const top=document.createElement('div');top.className='top';
 const t=document.createElement('span');t.className='tag '+(e.net==='mc'?'mc':'mt');
 t.textContent=e.net==='mc'?'MESHCORE':'MESHTASTIC';
 const w=document.createElement('span');w.className='who';w.textContent=e._name||e.sender;
 const wh=document.createElement('span');wh.className='when';wh.textContent=e._t||'';
 top.append(t,w,wh);
 const tx=document.createElement('div');tx.className='text';tx.textContent=e.text||'';
 d.append(top,tx);return d;}
function add(e){lastTs=Date.now();
 const old=feed.querySelector('.empty');if(old)old.remove();
 if(!filt[e.net])return;feed.appendChild(el(e));
 while(feed.children.length>500)feed.removeChild(feed.firstChild);
 document.getElementById('counts').textContent=feed.children.length+' shown';}
async function hist(){try{const r=await fetch('/api/history?n=100');const a=await r.json();
 a.forEach(e=>{const old=feed.querySelector('.empty');if(old)old.remove();
  if(filt[e.net])feed.appendChild(el(e));});
 while(feed.children.length>500)feed.removeChild(feed.firstChild);
 document.getElementById('counts').textContent=feed.children.length+' shown';
 feed.scrollTop=feed.scrollHeight;}catch(e){}}
const es=new EventSource('/api/stream');
es.onopen=()=>{document.getElementById('conn').textContent='feed: live ✅'};
es.onmessage=(m)=>{if(!paused)add(JSON.parse(m.data));};
es.onerror=()=>{document.getElementById('conn').textContent='feed: reconnecting…'};
fetch('/api/gw').then(r=>r.json()).then(j=>{
 const d=document.getElementById('gw');d.textContent='gateway: '+j.status;
 d.style.color=j.status==='active'?'#3fb950':'#f85149';}).catch(()=>{});
setInterval(()=>fetch('/api/gw').then(r=>r.json()).then(j=>{
 const d=document.getElementById('gw');d.textContent='gateway: '+j.status;
 d.style.color=j.status==='active'?'#3fb950':'#f85149';}).catch(()=>{}),10000);
document.getElementById('fMC').onclick=()=>{filt.mc=!filt.mc;filt.mc?
 document.getElementById('fMC').classList.add('on'):document.getElementById('fMC').classList.remove('on');};
document.getElementById('fMT').onclick=()=>{filt.mt=!filt.mt;filt.mt?
 document.getElementById('fMT').classList.add('on'):document.getElementById('fMT').classList.remove('on');};
document.getElementById('pause').onclick=()=>{paused=!paused;const b=document.getElementById('pause');
 b.textContent=paused?'▶ resume':'⏸ pause';};
document.getElementById('clear').onclick=()=>{feed.innerHTML='';
 document.getElementById('counts').textContent='';};
hist();setInterval(hist,15000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            self._send(200, PAGE.encode(), "text/html")
        elif p == "/api/history":
            try:
                n = int(self.path.split("n=")[1].split("&")[0])
            except Exception:
                n = HISTORY
            with _lock:
                body = json.dumps(list(_events[-n:])).encode()
            self._send(200, body)
        elif p == "/api/gw":
            try:
                import urllib.request
                with urllib.request.urlopen("http://127.0.0.1:8082/", timeout=2) as r:
                    body = r.read().decode()
                self._send(200, body.encode())
            except Exception:
                self._send(200, b'{"status":"down"}')
        elif p == "/api/names":
            with _lock:
                self._send(200, json.dumps(_names).encode())
        elif p == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            import queue
            q = queue.Queue()
            with _lock:
                _listeners.add(q)
            try:
                while True:
                    try:
                        data = q.get(timeout=SSE_KEEPALIVE_S)
                        self.wfile.write(b"data: " + data.encode() + b"\n\n")
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except Exception:
                pass
            finally:
                with _lock:
                    _listeners.discard(q)
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        if self.path.split("?")[0] == "/api/names":
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n) or b"{}")
                key = str(d.get("key", "")).strip()
                name = str(d.get("name", "")).strip()
                if key and name:
                    with _lock:
                        _names[key] = name
                    save_names()
                    self._send(200, b'{"ok":true}')
                    return
            except Exception:
                pass
            self._send(400, b'{"error":"bad request"}')
        else:
            self._send(404, b'{"error":"not found"}')


def main():
    load_names()
    t = threading.Thread(target=tail_events, daemon=True)
    t.start()
    port = int(os.environ.get("MESH_LIVE_PORT", "8083"))
    print(f"mesh_live on :{port} — http://127.0.0.1:{port}/")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


if __name__ == "__main__":
    main()
