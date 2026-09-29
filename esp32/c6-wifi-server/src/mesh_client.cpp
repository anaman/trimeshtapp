// SPDX-License-Identifier: GPL-3.0-or-later
// mesh_client.cpp — TCP client to MeshCore V3 nodes, frame parser
#include <Arduino.h>
#include <WiFi.h>
#include "config.h"

struct NodeConnection {
  String ip;
  WiFiClient client;
  bool connected;
  unsigned long last_reconnect;
  unsigned long last_frame_ts;
  uint32_t frame_count;
  // frame parser state
  bool has_header;
  uint8_t frame_type;
  uint16_t frame_length;
  uint8_t frame_buf[MAX_FRAME_SIZE];
};

static NodeConnection nodes[MAX_NODES];
static int node_count = 0;

// ─── Frame ring buffer (served via HTTP /frames) ──────
static FrameRecord frame_ring[FRAME_RING_SIZE];
static uint32_t frame_total = 0;   // monotonic sequence counter

// Parse comma-separated node addresses from NODE_ADDRESSES
void mesh_client_init() {
  String addrs = String(NODE_ADDRESSES);
  int start = 0;
  while (start < addrs.length() && node_count < MAX_NODES) {
    int comma = addrs.indexOf(',', start);
    String ip;
    if (comma == -1) {
      ip = addrs.substring(start);
      start = addrs.length();
    } else {
      ip = addrs.substring(start, comma);
      start = comma + 1;
    }
    ip.trim();
    if (ip.length() > 0) {
      nodes[node_count].ip = ip;
      nodes[node_count].connected = false;
      nodes[node_count].last_reconnect = 0;
      nodes[node_count].last_frame_ts = 0;
      nodes[node_count].frame_count = 0;
      nodes[node_count].has_header = false;
      nodes[node_count].frame_type = 0;
      nodes[node_count].frame_length = 0;
      DBG_PRINT("[mesh] node ");
      DBG_PRINT(node_count);
      DBG_PRINT(": ");
      DBG_PRINTLN(ip.c_str());
      node_count++;
    }
  }
  DBG_PRINT("[mesh] registered ");
  DBG_PRINT(node_count);
  DBG_PRINTLN(" nodes");
}

static void connect_node(int i) {
  DBG_PRINT("[mesh] connecting to ");
  DBG_PRINT(nodes[i].ip.c_str());
  DBG_PRINT(":");
  DBG_PRINTLN(NODE_PORT);

  if (nodes[i].client.connect(nodes[i].ip.c_str(), NODE_PORT, 5000)) {
    nodes[i].connected = true;
    nodes[i].has_header = false;
    DBG_PRINT("[mesh] connected to ");
    DBG_PRINTLN(nodes[i].ip.c_str());
  } else {
    DBG_PRINT("[mesh] failed to connect to ");
    DBG_PRINTLN(nodes[i].ip.c_str());
  }
  nodes[i].last_reconnect = millis();
}

// Callback for received frames
typedef void (*FrameCallback)(const char* source_ip, uint8_t* payload, size_t len);
static FrameCallback frame_callback = nullptr;

void mesh_client_set_callback(FrameCallback cb) {
  frame_callback = cb;
}

// Parse incoming data from a node
static void parse_node(int i) {
  WiFiClient& c = nodes[i].client;

  if (!c.available()) return;

  // Read frame header if we don't have one yet
  if (!nodes[i].has_header) {
    if (c.available() < FRAME_HEADER_LEN) return;

    nodes[i].frame_type = c.read();
    c.readBytes((uint8_t*)&nodes[i].frame_length, 2);  // 16-bit LE

    if (nodes[i].frame_length > MAX_FRAME_SIZE) {
      DBG_PRINT("[mesh] frame too large (");
      DBG_PRINT(nodes[i].frame_length);
      DBG_PRINTLN("), skipping");
      // drain
      while (nodes[i].frame_length > 0 && c.available()) {
        c.read();
        nodes[i].frame_length--;
      }
      return;
    }

    if (nodes[i].frame_type != FRAME_TYPE_OUT) {
      DBG_PRINT("[mesh] unexpected frame type 0x");
      DBG_PRINTLN(nodes[i].frame_type, HEX);
      // skip this frame's data
      while (nodes[i].frame_length > 0 && c.available()) {
        c.read();
        nodes[i].frame_length--;
      }
      return;
    }

    nodes[i].has_header = true;
  }

  // Read frame payload
  if (nodes[i].has_header) {
    size_t available = c.available();
    if (available < nodes[i].frame_length) return;

    c.readBytes(nodes[i].frame_buf, nodes[i].frame_length);

    // Deliver frame
    if (frame_callback) {
      frame_callback(nodes[i].ip.c_str(), nodes[i].frame_buf, nodes[i].frame_length);
    }

    // Record into ring buffer for HTTP /frames consumers
    {
      uint32_t idx = frame_total % FRAME_RING_SIZE;
      frame_ring[idx].seq = frame_total;
      strncpy(frame_ring[idx].source, nodes[i].ip.c_str(), sizeof(frame_ring[idx].source) - 1);
      frame_ring[idx].source[sizeof(frame_ring[idx].source) - 1] = 0;
      frame_ring[idx].len = nodes[i].frame_length;
      memcpy(frame_ring[idx].data, nodes[i].frame_buf, nodes[i].frame_length);
      frame_ring[idx].ts = millis();
      frame_total++;
    }

    nodes[i].frame_count++;
    nodes[i].last_frame_ts = millis();
    nodes[i].has_header = false;
    nodes[i].frame_type = 0;
    nodes[i].frame_length = 0;
  }
}

void mesh_client_loop() {
  for (int i = 0; i < node_count; i++) {
    if (!nodes[i].connected) {
      if (millis() - nodes[i].last_reconnect > NODE_RECONNECT_MS) {
        connect_node(i);
      }
      continue;
    }

    if (!nodes[i].client.connected()) {
      DBG_PRINT("[mesh] lost connection to ");
      DBG_PRINTLN(nodes[i].ip.c_str());
      nodes[i].connected = false;
      nodes[i].client.stop();
      continue;
    }

    parse_node(i);
  }
}

// Send a frame to a specific node (client → node)
bool mesh_client_send(const char* ip, uint8_t* payload, size_t len) {
  for (int i = 0; i < node_count; i++) {
    if (nodes[i].ip == ip && nodes[i].connected && nodes[i].client.connected()) {
      uint8_t header[FRAME_HEADER_LEN];
      header[0] = FRAME_TYPE_IN;
      header[1] = len & 0xFF;
      header[2] = (len >> 8) & 0xFF;
      nodes[i].client.write(header, FRAME_HEADER_LEN);
      nodes[i].client.write(payload, len);
      return true;
    }
  }
  return false;
}

// Send a frame to all connected nodes (broadcast)
void mesh_client_broadcast(uint8_t* payload, size_t len) {
  for (int i = 0; i < node_count; i++) {
    if (nodes[i].connected && nodes[i].client.connected()) {
      uint8_t header[FRAME_HEADER_LEN];
      header[0] = FRAME_TYPE_IN;
      header[1] = len & 0xFF;
      header[2] = (len >> 8) & 0xFF;
      nodes[i].client.write(header, FRAME_HEADER_LEN);
      nodes[i].client.write(payload, len);
    }
  }
}

int mesh_client_node_count() { return node_count; }

// Add a node dynamically (e.g. discovered via mDNS). No-op if already known.
// Returns true if a new node was added.
bool mesh_client_add_node(const char* ip) {
  if (!ip || !ip[0]) return false;
  for (int i = 0; i < node_count; i++) {
    if (nodes[i].ip == ip) return false;  // already tracked
  }
  if (node_count >= MAX_NODES) return false;
  nodes[node_count].ip = String(ip);
  nodes[node_count].connected = false;
  nodes[node_count].last_reconnect = 0;
  nodes[node_count].last_frame_ts = 0;
  nodes[node_count].frame_count = 0;
  nodes[node_count].has_header = false;
  nodes[node_count].frame_type = 0;
  nodes[node_count].frame_length = 0;
  DBG_PRINT("[mesh] discovered node: ");
  DBG_PRINTLN(ip);
  node_count++;
  return true;
}

bool mesh_client_is_connected(int i) {
  if (i < 0 || i >= node_count) return false;
  return nodes[i].connected && nodes[i].client.connected();
}

String mesh_client_node_ip(int i) {
  if (i < 0 || i >= node_count) return "";
  return nodes[i].ip;
}

uint32_t mesh_client_frame_count(int i) {
  if (i < 0 || i >= node_count) return 0;
  return nodes[i].frame_count;
}

unsigned long mesh_client_last_frame(int i) {
  if (i < 0 || i >= node_count) return 0;
  return nodes[i].last_frame_ts;
}

// ─── Ring buffer accessors (for HTTP /frames) ─────────
uint32_t mesh_frames_total() { return frame_total; }

// Return up to max_n records with seq >= since_seq, oldest first.
// Returns the number written into out[].
int mesh_frames_since(uint32_t since_seq, int max_n, FrameRecord* out) {
  int n = 0;
  uint32_t start = (frame_total > FRAME_RING_SIZE) ? (frame_total - FRAME_RING_SIZE) : 0;
  for (uint32_t s = start; s < frame_total && n < max_n; s++) {
    if (s < since_seq) continue;
    out[n++] = frame_ring[s % FRAME_RING_SIZE];
  }
  return n;
}
