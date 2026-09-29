// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef CONFIG_H
#define CONFIG_H

// ─── WiFi ──────────────────────────────────────────────
#ifndef WIFI_SSID
  #define WIFI_SSID "CHANGEME"
#endif
#ifndef WIFI_PWD
  #define WIFI_PWD "CHANGEME"
#endif

// ─── MQTT ──────────────────────────────────────────────
#ifndef MQTT_BROKER
  #define MQTT_BROKER "192.168.1.100"
#endif
#ifndef MQTT_PORT
  #define MQTT_PORT 1883
#endif
#ifndef MQTT_TOPIC
  #define MQTT_TOPIC "mesh/c6/frames"
#endif
#ifndef MQTT_CLIENT_ID
  #define MQTT_CLIENT_ID "c6-mesh-server"
#endif
#ifndef MQTT_USER
  #define MQTT_USER "mesh"
#endif
#ifndef MQTT_PASS
  #define MQTT_PASS ""
#endif

// ─── V3 Nodes ─────────────────────────────────────────
#ifndef NODE_ADDRESSES
  #define NODE_ADDRESSES "192.168.1.50"
#endif
#ifndef NODE_PORT
  #define NODE_PORT 5000
#endif
#define MAX_NODES 8

// ─── Status Server ────────────────────────────────────
#ifndef STATUS_PORT
  #define STATUS_PORT 80
#endif
#ifndef MDNS_NAME
  #define MDNS_NAME "c6-mesh-server"
#endif

// ─── Mesh Frame Protocol ──────────────────────────────
// Matches MeshCore SerialWifiInterface framing
#define FRAME_TYPE_OUT  '>'   // 0x3e — node → client
#define FRAME_TYPE_IN   '<'   // 0x3c — client → node
#define MAX_FRAME_SIZE  255
#define FRAME_HEADER_LEN 3   // 1 byte type + 2 bytes LE length

// ─── Frame ring buffer (served via /frames) ───────────
#define FRAME_RING_SIZE 32

struct FrameRecord {
  uint32_t seq;
  char source[16];
  uint16_t len;
  uint8_t data[MAX_FRAME_SIZE];
  unsigned long ts;
};

// ─── Timing ───────────────────────────────────────────
#define WIFI_RECONNECT_MS    10000
#define NODE_RECONNECT_MS    5000
#define MQTT_RECONNECT_MS    10000
#define STATUS_LOOP_MS       1000
#define HEARTBEAT_MS         30000
#define MDNS_SCAN_MS         60000

// ─── Debug ────────────────────────────────────────────
// NOTE: do not call this DEBUG_SERIAL — Adafruit BusIO defines that macro.
#ifdef C6_DEBUG_LOG
  #define DBG_PRINT(...)    Serial.print(__VA_ARGS__)
  #define DBG_PRINTLN(...)  Serial.println(__VA_ARGS__)
#else
  #define DBG_PRINT(...)
  #define DBG_PRINTLN(...)
#endif

#endif // CONFIG_H
