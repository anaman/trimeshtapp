#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
rns_status_server.py — tiny HTTP status endpoint for the Reticulum (RNS) leg.

Serves on 127.0.0.1:8451. Expose it however you like (reverse proxy,
overlay network, or keep it local-only).

Endpoints:
  /            HTML summary (rnsd + bridge services, RNS transport, interfaces)
  /status.json live rnstatus -j output
  /health      {"ok":true} liveness
"""
import json
import os
import shutil
import subprocess
import http.server
import socketserver

HOST = "127.0.0.1"
PORT = 8451
RNSTATUS = os.environ.get("RNSTATUS_BIN") or shutil.which("rnstatus") or "/opt/mesh-bridge/rns-bridge/.venv/bin/rnstatus"


def rnstatus_json():
    try:
        out = subprocess.run(
            [RNSTATUS, "-j"], capture_output=True, timeout=20
        )
        return json.loads(out.stdout.decode(errors="replace"))
    except Exception as e:
        return {"error": str(e)}


def service_state(name):
    try:
        out = subprocess.run(
            ["systemctl", "is-active", name], capture_output=True, timeout=5
        )
        return out.stdout.decode().strip()
    except Exception as e:
        return f"error: {e}"


def build_status():
    d = rnstatus_json()
    ifaces = d.get("interfaces", [])
    lo = next((i for i in ifaces if "KISS" in i.get("name", "")), None)
    return {
        "services": {
            "rnsd": service_state("rnsd.service"),
            "rns-bridge": service_state("rns-bridge.service"),
            "mesh-gateway": service_state("mesh-gateway.service"),
        },
        "rns": {
            "transport_id": d.get("transport_id"),
            "transport_uptime_s": round(d.get("transport_uptime", 0), 1),
            "rxb": d.get("rxb"),
            "txb": d.get("txb"),
            "interfaces": [
                {
                    "name": i.get("name"),
                    "type": i.get("type"),
                    "status": i.get("status"),
                    "rxb": i.get("rxb"),
                    "txb": i.get("txb"),
                }
                for i in ifaces
            ],
            "lora_interface": {
                "name": lo.get("name") if lo else None,
                "status": lo.get("status") if lo else None,
                "bitrate": lo.get("bitrate") if lo else None,
                "rxb": lo.get("rxb") if lo else None,
                "txb": lo.get("txb") if lo else None,
            },
        },
    }


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path.startswith("/status.json"):
                body = json.dumps(build_status(), indent=1).encode()
                self._send(200, body, "application/json")
            elif self.path.startswith("/health"):
                self._send(200, b'{"ok": true}', "application/json")
            else:
                s = build_status()
                html = (
                    "<html><head><title>Reticulum (RNS) leg</title></head>"
                    "<body><h1>Reticulum (RNS) status</h1>"
                    "<p>Services: <b>%s</b></p>"
                    "<p>Transport: %s (uptime %ss)</p>"
                    "<p>LoRa interface: %s — %s</p>"
                    "<p><a href='/status.json'>status.json</a></p></body></html>"
                ) % (
                    ", ".join(f"{k}={v}" for k, v in s["services"].items()),
                    s["rns"]["transport_id"],
                    s["rns"]["transport_uptime_s"],
                    (s["rns"]["lora_interface"] or {}).get("name"),
                    (s["rns"]["lora_interface"] or {}).get("status"),
                )
                self._send(200, html.encode(), "text/html")
        except Exception as e:
            self._send(500, str(e).encode(), "text/plain")

    def log_message(self, *a):
        pass


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    print(f"RNS status server on http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
