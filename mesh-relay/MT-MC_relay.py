#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Relay for Meshtastic and MeshCore public channels.
Source: https://meshnard.com/mesh/mt-mc_relay (Aug 2025).
Fixes: dedup cache now actually records processed IDs; bounded trimming.
Deployed 2026-08-11.
"""

import asyncio
import os
import sys
from meshtastic import serial_interface
from meshtastic.protobuf.portnums_pb2 import PortNum
from meshcore import MeshCore, EventType
from pubsub import pub
import serial

# --- Configuration ---
# Set stable by-id paths for your radios (use mesh_relay_setup.py scan):
MESHTASTIC_PORT = os.environ.get("MESHGW_MT_PORT", "/dev/ttyACM1")
MESHCORE_PORT = os.environ.get("MESHGW_MC_PORT", "/dev/ttyACM0")
RELAY_ID_PREFIX = "[MT<>MC]"
MAX_ID_CACHE_SIZE = 200

# --- State Management ---
meshtastic_relayed_ids = set()
meshcore_relayed_ids = set()


def cache_add(cache, item):
    """Add to a bounded set, trimming when it exceeds MAX_ID_CACHE_SIZE."""
    if len(cache) >= MAX_ID_CACHE_SIZE:
        cache.clear()
    cache.add(item)


# --- Meshtastic-to-MeshCore Logic ---
async def meshtastic_to_meshcore_relay(meshtastic_interface, meshcore_instance):
    """Listens for Meshtastic messages and forwards them to MeshCore."""
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()

    def on_meshtastic_message(packet, interface=None):
        loop.call_soon_threadsafe(queue.put_nowait, packet)

    pub.subscribe(on_meshtastic_message, "meshtastic.receive")
    print("Meshtastic listener is active.")

    while True:
        try:
            packet = await queue.get()
            decoded = packet.get('decoded', {})
            portnum = decoded.get('portnum')
            is_text = (portnum == PortNum.TEXT_MESSAGE_APP or str(portnum) == 'TEXT_MESSAGE_APP')

            if is_text:
                message_text = decoded.get('text')
                message_id = packet.get('id')

                if not message_text or message_text.startswith(RELAY_ID_PREFIX) or message_id in meshtastic_relayed_ids:
                    continue

                from_id = packet.get('fromId')
                sender_name = from_id
                if from_id in meshtastic_interface.nodes:
                    node_info = meshtastic_interface.nodes[from_id]
                    if 'user' in node_info and 'longName' in node_info['user']:
                        sender_name = node_info['user']['longName']

                full_message = f"{sender_name}: {message_text}"
                print(f"Meshtastic -> MeshCore: '{full_message}'")

                cache_add(meshtastic_relayed_ids, message_id)
                relayed_text = f"{RELAY_ID_PREFIX} {full_message}"
                await meshcore_instance.commands.send_chan_msg(chan=0, msg=relayed_text)
                print("DEBUG: MeshCore send_chan_msg command completed.")

        except Exception as e:
            print(f"Error in Meshtastic-to-MeshCore relay: {e}", file=sys.stderr)
            await asyncio.sleep(1)


# --- MeshCore-to-Meshtastic Logic ---
async def meshcore_to_meshtastic_relay(meshtastic_interface, meshcore_instance):
    """Listens for MeshCore messages and forwards them to Meshtastic."""
    loop = asyncio.get_running_loop()

    async def handle_meshcore_message(event):
        msg = event.payload
        message_text = msg.get("text", "")
        sender_timestamp = msg.get("sender_timestamp")
        unique_id = f"{sender_timestamp}-{message_text}"

        if not message_text or message_text.startswith(RELAY_ID_PREFIX) or unique_id in meshcore_relayed_ids:
            return

        print(f"MeshCore -> Meshtastic: '{message_text}'")
        cache_add(meshcore_relayed_ids, unique_id)
        relayed_text = f"{RELAY_ID_PREFIX} {message_text}"
        await loop.run_in_executor(
            None,
            lambda: meshtastic_interface.sendText(relayed_text),
        )

    meshcore_instance.subscribe(EventType.CHANNEL_MSG_RECV, handle_meshcore_message)
    await meshcore_instance.start_auto_message_fetching()
    print("MeshCore listener is active.")

    await asyncio.Event().wait()


# --- Main Execution ---
async def main():
    meshtastic_interface = None
    meshcore_instance = None
    loop = asyncio.get_running_loop()

    try:
        print("--- Mesh Relay ---")

        print(f"Connecting to Meshtastic radio on {MESHTASTIC_PORT}...")
        meshtastic_interface = await loop.run_in_executor(
            None, serial_interface.SerialInterface, MESHTASTIC_PORT
        )
        print("Meshtastic radio connected.")

        print(f"Connecting to MeshCore radio on {MESHCORE_PORT}...")
        meshcore_instance = await MeshCore.create_serial(MESHCORE_PORT)
        print("MeshCore radio connected.")

        print("Waiting for radios to settle...")
        await asyncio.sleep(5)

        print("\nStarting relay bridges... Press Ctrl+C to stop.")
        await asyncio.gather(
            meshtastic_to_meshcore_relay(meshtastic_interface, meshcore_instance),
            meshcore_to_meshtastic_relay(meshtastic_interface, meshcore_instance),
        )

    except (serial.serialutil.SerialException, FileNotFoundError) as e:
        print(f"\nFATAL ERROR: Could not connect to a radio. Please check your port configuration.", file=sys.stderr)
        print(f" Details: {e}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nShutting down relay...")
    except Exception as e:
        print(f"\nAn unexpected error occurred: {e}", file=sys.stderr)
    finally:
        if meshtastic_interface:
            meshtastic_interface.close()
        print("Relay shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
