# systemd units

Install (as root):

```bash
sudo cp *.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mesh-gateway.service rnsd.service rns-bridge.service \
  rns-status-server.service lora-dashboard.service mqtt-bridge.service
```

Notes
- Units assume the repo is installed at `/opt/mesh-bridge` and run as user
  `mesh`. Adjust `User=` / paths for your install.
- `mesh-gateway.service` has a deliberate 120s boot delay
  (`ExecStartPre=/bin/sleep 120`) so the radios finish re-enumerating; expect
  `activating` during that window.
- `rns-propagation.service` and `rns-peer-tracker.service` are optional extras.
- Restarting `mesh-gateway.service` blocks for up to ~120s — see
  `skills/mesh-relay-flush-restart-safety`.
