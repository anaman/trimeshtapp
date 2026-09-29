# Setup guide

A complete, standalone walkthrough. Adjust names/paths to taste — the only
hard requirements are: one Linux host with systemd, USB ports for the
radios, and (optionally) a reverse proxy + VPN for remote dashboard access.

Reference install layout used throughout:

```
/opt/mesh-bridge/
├── mesh-relay/        # gateway, watcher, mqtt bridge, mesh live, tools
│   └── .venv/
├── rns-bridge/        # rns bridge, tracker, propagation, status server
│   └── .venv/
├── lora-dashboard/    # web dashboard
├── esp32/             # optional ESP32 firmware projects
├── skills/            # agent playbooks
├── systemd/           # unit files
└── docs/
```

## 1. Prerequisites

**Hardware (reference set):**

| Radio | Role | Example |
|---|---|---|
| MeshCore companion node | MeshCore leg (USB serial) | Heltec V3 / ESP32-S3 with `companion_radio` USB firmware |
| Meshtastic radio | Meshtastic leg (USB serial) | Heltec Wireless Tracker |
| RNode | Reticulum leg (USB serial, KISS) | Heltec V3 running RNode firmware |
| Host | runs everything | any small always-on Linux box |

**Host requirements:**
- Linux with systemd (Debian/Ubuntu family assumed)
- Python 3.11+ (3.12 recommended), `python3-venv`
- `mosquitto` (the dashboard authenticates against it; also the local MQTT leg)
- Optional: Tailscale/other VPN for remote access; nginx/Caddy for TLS
- A user for the services (reference: `mesh`) with `dialout` group access

```bash
sudo adduser --system --group --home /nonexistent mesh || sudo useradd -r -g mesh mesh
sudo usermod -aG dialout mesh
```

## 2. Install the repo

```bash
sudo mkdir -p /opt/mesh-bridge
sudo chown mesh:mesh /opt/mesh-bridge
git clone <this repo> /opt/mesh-bridge
cd /opt/mesh-bridge
```

## 3. Python environments

Two venvs (keeps the Meshtastic/MeshCore pins away from RNS):

```bash
# Relay venv (used by gateway, watcher, mqtt bridge, mesh live, dashboard)
python3 -m venv mesh-relay/.venv
mesh-relay/.venv/bin/pip install -U pip
mesh-relay/.venv/bin/pip install -r mesh-relay/requirements.txt
mesh-relay/.venv/bin/pip install -r lora-dashboard/requirements.txt

# RNS venv (bridge, tracker, propagation, rnsd, rnodeconf)
python3 -m venv rns-bridge/.venv
rns-bridge/.venv/bin/pip install -U pip
rns-bridge/.venv/bin/pip install -r rns-bridge/requirements.txt
```

Notes:
- `rns-bridge/.venv/bin/` must contain the `rnsd` and `rnodeconf` entry
  points (they come with the `rns` package).
- The peer tracker and status server run on system `python3` (standard
  library only).

## 4. Radios & firmware

Flash each radio with its reference firmware (upstream projects):

- **MeshCore** companion radio firmware — https://github.com/meshcore-dev/MeshCore
  (`examples/companion_radio`; USB serial role — the companion Bluetooth
  role is not supported by the meshcore python library).
- **Meshtastic** firmware — https://github.com/meshtastic/firmware (set your
  region; LongFast preset is the reference).
- **RNode** firmware — https://github.com/markqvist/RNode_Firmware or
  `rnodeconf --autoinstall` from the rns package (the RNode becomes a
  transparent KISS modem for Reticulum).

Discover which `/dev` path is which (with services stopped):

```bash
cd /opt/mesh-bridge
mesh-relay/.venv/bin/python mesh-relay/mesh_relay_setup.py scan
# then persist the discovered ports:
mesh-relay/.venv/bin/python mesh-relay/mesh_relay_setup.py apply --yes
# -> writes mesh-relay/ports.json; restart the gateway afterwards
```

If auto-detection can't decide, set explicit ports in `ports.json`
(`{"meshcore": "/dev/ttyACM0", "meshtastic": "/dev/ttyACM1"}`) or via env
(`MESHGW_MC_PORT`, `MESHGW_MT_PORT`).

## 5. Mosquitto (broker + dashboard auth)

A local broker on `127.0.0.1:1883` serves two purposes: the dashboard login
check and the local MQTT leg.

```bash
sudo apt install mosquitto mosquitto-clients
sudo mosquitto_passwd -c /etc/mosquitto/passwd meshuser   # set a password
```

Minimal `/etc/mosquitto/conf.d/local.conf`:

```conf
listener 1883 127.0.0.1
allow_anonymous false
password_file /etc/mosquitto/passwd
```

```bash
sudo systemctl restart mosquitto
# verify your credentials (this is what the dashboard does):
mosquitto_pub -h 127.0.0.1 -u meshuser -P 'yourpass' -t test -m ok
```

Then create `mesh-relay/mqtt_bridge.json` from
`mesh-relay/mqtt_bridge.json.example` and configure brokers as desired
(community brokers should use mode `in` only).

## 6. Reticulum configuration (RNode leg)

`rnsd` owns the RNode. Minimal `~/.reticulum/config` (adjust the port):

```ini
[reticulum]
  enable_transport = No
  share_instance = Yes

[logging]
  loglevel = 4

[interfaces]

  [[RNode LoRa Interface]]
    type = KISSInterface
    interface_enabled = True
    outgoing = True
    port = /dev/ttyUSB0
    speed = 115200
    # databits = 8, parity = none, stopbits = 1 (defaults)
```

Notes:
- Plain `KISSInterface` (not `RNodeInterface`) is the reference setup for
  ESP32 RNode firmware whose detect reply differs from RNS's expectation.
- Radio parameters live in the RNode EEPROM — set them once with
  `rnodeconf` (e.g. 915 MHz class / 125 kHz / SF8 / CR5 / 14 dBm for US).
- To accept network peers, add a `TCPServerInterface` (e.g. port 4242) and
  point remote clients at it with `TCPClientInterface`. Pin
  `prefer_ipv6 = False` on TCP client interfaces to avoid "Network is
  unreachable" flaps (see `skills/rns-lxmf-propagation-node`).

## 7. Services

```bash
cd /opt/mesh-bridge
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload

# core
sudo systemctl enable --now mesh-gateway.service rnsd.service rns-bridge.service \
  rns-status-server.service lora-dashboard.service mqtt-bridge.service

# optional
sudo systemctl enable --now rns-peer-tracker.service rns-propagation.service
```

The mesh gateway waits 120 s at boot (radios re-enumerate) — `activating`
during that window is normal.

## 8. Schedulers (the "always-on" glue)

The system expects three recurring jobs (cron or systemd timers — any
scheduler works). Reference crontab:

```cron
# announce new mesh messages to chat (cheap; run every 5 seconds via your
# scheduler of choice — a command job, no model cost)
* * * * * for i in 0 5 10 15 20 25 30 35 40 45 50 55; do \
  (sleep $i; cd /opt/mesh-bridge/mesh-relay && .venv/bin/python mesh_watch.py) & done

# gateway health check (30 min) — alert if the gateway is down
*/30 * * * * systemctl is-active --quiet mesh-gateway.service || echo "mesh-gateway DOWN" | logger -t mesh-health

# cache cleanup (hourly) — clears only buffers idle >= 1h
0 * * * * /opt/mesh-bridge/mesh-relay/mesh_cache_cleanup.sh
```

If you run an LLM agent as the delivery layer (reference implementation),
have it consume `/tmp/meshgw_outbound.jsonl` and deliver via its chat
channels; reply commands go back through `/tmp/meshgw_inbox.jsonl`
(see `docs/ARCHITECTURE.md` §2.2).

## 9. Dashboard

- Local: `http://127.0.0.1:8452/` — sign in with the mosquitto credentials
  from step 5.
- Remote access: put it behind a reverse proxy or VPN. Example nginx slice:

```nginx
location / {
    proxy_pass http://127.0.0.1:8452;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

- The dashboard's *Devices* tab scans your LAN /24 (set `LAN_SUBNET` env or
  edit the scan settings) and overlay-network peers for MeshCore (`:5000`)
  and Meshtastic (`:4403`) endpoints.

## 10. Verification checklist

```bash
systemctl is-active mesh-gateway.service rnsd.service rns-bridge.service \
  rns-status-server.service lora-dashboard.service mosquitto.service
curl -s http://127.0.0.1:8082/            # {"status":"active","dedup":{...}}
curl -s http://127.0.0.1:8451/status.json # RNS leg state
```

- [ ] Send a message on MeshCore → appears in your chat channel within ~5 s
- [ ] Send a message on Meshtastic → same
- [ ] Send the same text+node on both networks → second copy suppressed
      (`[dedup] suppressed repeat` in the gateway log)
- [ ] `meshgw reply mc "test"` (or a chat reply command) → radio transmits
- [ ] RNS: `rns-bridge` log shows announce + packets; `rnstatus` shows the
      KISS interface Up
- [ ] Dashboard reachable; login works; Bridge/Protocols/Devices tabs live

## 11. Optional add-ons

- **ESP32-C6 WiFi hub** — `esp32/c6-wifi-server/` (WiFi TCP link to Heltec
  nodes → MQTT).
- **LXMF propagation node** — enabled above; give the destination hash to
  Sideband clients (compute via the venv python; see the skill).
- **Peer-tracker email digest** — set `RNS_TRACKER_EMAIL` +
  `RNS_TRACKER_TZ`, wire sendmail, run `--daily` from cron.
- **Mesh Live** — `mesh-live.service` (SSE feed of both mesh legs).
- **Display sleep / burn-in** — see `skills/lora-display-sleep-config`.
