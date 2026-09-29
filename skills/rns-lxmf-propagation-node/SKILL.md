---
name: "rns-lxmf-propagation-node"
description: "Run an LXMF propagation node on a Reticulum (RNS) relay so Sideband clients mint stamps; fix TCP interface \"Network is unreachable\" flaps."
---

# rns-lxmf-propagation-node

Run an LXMF propagation node on a Reticulum (RNS) relay so Sideband clients can mint stamps locally; pin TCP client interfaces to IPv4 to stop "Network is unreachable" flaps.

## When to use
- Sideband/LXMF errors: "Propagation node stamp cost still unavailable after path request", "Could not send due to a stamp generation failure", "cannot generate propagation stamp".
- Relay is transport-only; LXMF messages need a reachable propagation node.
- rnsd journal shows TCP client reconnect loops with "Connection reset by peer" / "Network is unreachable" (Errno 101).

## Setup (RNS venv)
1. Check LXMF: `.venv/bin/python -c "import LXMF; print(LXMF.__version__)"`. Import is `LXMF` (uppercase); `import lxmf` fails.
2. Write service script:
   - `RNS.Reticulum()` first, before touching LXMRouter or Destination.
   - Persistent identity: load `RNS.Identity.from_bytes(open("~/.reticulum/lxmf_propagation_identity","rb").read())` if present, else `RNS.Identity()` and save `get_private_key()`. Fresh identity = new destination hash = Sideband config breaks.
   - `router = LXMRouter(identity=ident, storagepath="~/.reticulum/lxmf_propagation_store", name="...", enforce_stamps=False)`
   - `router.enable_propagation()`; daemon thread re-announces via `router.announce_propagation_node()` every ~300 s; main loop sleeps.
3. systemd unit: `After=rnsd.service`, `Requires=rnsd.service`, `Restart=on-failure`, `RestartSec=10`, run as user with venv python. daemon-reload, enable, start.
4. Verify: `systemctl is-active rns-propagation`; journal shows `Peered with <hex>` lines; `rnstatus -j` shows interfaces status=True with txb/rxb flowing.

## Destination hash for Sideband config
- Compute inside a process that already ran `RNS.Reticulum()`:
  `RNS.Destination(ident, RNS.Destination.IN, RNS.Destination.SINGLE, 'lxmf', 'propagation').hash.hex()`
- Without the RNS instance this crashes: `AttributeError: type object 'Transport' has no attribute 'owner'`. Run via the venv python, not system python.

## Stabilize public TCP leg
- "Network is unreachable" (Errno 101) on TCPClientInterface = IPv6 route flap. Check `getent ahosts <host>` and `ip -6 route show default`.
- Pin IPv4: add `prefer_ipv6 = False` under the `[[Interface Name]]` block in `~/.reticulum/config`. Backup first: `cp ~/.reticulum/config ~/.reticulum/config.bak-$(date +%Y%m%d-%H%M%S)`. Restart rnsd.

## Pitfalls
- Upstream LXMF example paths (raw.githubusercontent.com/markqvist/lxmf/.../examples/propagation_node.py, api.github.com contents) return 404. Discover API/config options by grepping installed package sources instead:
  - `grep -nE "def enable_propagation" LXMF/LXMRouter.py`
  - `grep -nE "prefer_ipv6" RNS/Interfaces/TCPInterface.py`
- Starting the propagation node while rnsd is mid-restart logs `Shared instance RPC failed while setting destination data use: [Errno 111] Connection refused`; recover by restarting rns-propagation once rnsd is active.
- Single LXMRouter instance supports one delivery identity only.

## Verification
- Sideband send succeeds without stamp error after adding the printed destination hash as a Propagation Node.
- `journalctl -u rns-propagation.service` shows peering and no RPC errors.
- `rnstatus -j`: Sideband Public Server interface status=True, txb/rxb increasing.
