# Skills (agent playbooks)

These are reusable operational playbooks ("skills") written for an AI agent
while building and running this bridge. Each `SKILL.md` is a compact,
task-oriented procedure with evidenced pitfalls and a verification step.

Hand them to your LLM/agent as reference material, or follow them yourself:

| Skill | Use when |
|---|---|
| `mesh-relay-setup` | Deploying/validating the MeshCore↔Meshtastic gateway; discovering radios; flush/restart |
| `mesh-relay-flush-restart-safety` | Running flush or restarting mesh-gateway (120s boot delay hazards) |
| `meshcore-node-link-debug` | A MeshCore node won't link (empty neighbors, stuck RTC, console wedge) |
| `meshcore-heltec-custom-firmware` | Building custom MeshCore firmware for Heltec LoRa32 V3 |
| `lora-display-sleep-config` | Setting/checking display sleep on RNode / Meshtastic / MeshCore units |
| `reticulum-rns-integration` | Building/debugging RNS apps (identities, destinations, callbacks) |
| `rns-lxmf-propagation-node` | Running an LXMF propagation node; fixing TCP interface flaps |
| `rns-lxmf-debugging` | Debugging LXMF sends / announce handlers / delivery_destinations |

License: GPL-3.0 (same as the repository). They are documentation — adapt
paths to your install.
