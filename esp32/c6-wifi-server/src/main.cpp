// SPDX-License-Identifier: GPL-3.0-or-later
// main.cpp — ESP32-C6 WiFi Mesh Server entry point
//
// Connects to local WiFi, maintains TCP connections to MeshCore V3 nodes,
// relays mesh frames to MQTT broker, serves HTTP status endpoint.
//
// See DESIGN.md for full architecture.
#include <Arduino.h>
#include "config.h"

// wifi_client.cpp
void wifi_setup();
void wifi_loop();
bool wifi_is_connected();
String wifi_get_ip();

// mesh_client.cpp
void mesh_client_init();
void mesh_client_loop();
void mesh_client_set_callback(void (*cb)(const char*, uint8_t*, size_t));
int mesh_client_node_count();
bool mesh_client_is_connected(int i);

// mqtt_relay.cpp
void mqtt_setup();
void mqtt_loop();
bool mqtt_is_connected();
void mqtt_publish_frame(const char* source_ip, uint8_t* payload, size_t len);

// mdns_discovery.cpp
void mdns_setup();
void mdns_loop();

// status_server.cpp
void status_setup();
void status_loop();

// lcd_status.cpp (optional, -D HAS_LCD=1)
void lcd_setup();
void lcd_loop();
bool lcd_is_ok();

static unsigned long last_heartbeat = 0;

// Callback: called when a mesh frame arrives from a V3 node
static void on_mesh_frame(const char* source_ip, uint8_t* payload, size_t len) {
  DBG_PRINT("[frame] from ");
  DBG_PRINT(source_ip);
  DBG_PRINT(" len=");
  DBG_PRINTLN(len);

  // Relay to MQTT
  mqtt_publish_frame(source_ip, payload, len);
}

void setup() {
  Serial.begin(115200);
  delay(200);

  DBG_PRINTLN("============================================");
  DBG_PRINTLN("  ESP32-C6 WiFi Mesh Server");
  DBG_PRINTLN("  MeshBridge — 2026-09-07");
  DBG_PRINTLN("============================================");

  // WiFi
  wifi_setup();

  // Wait for WiFi (max 30s)
  unsigned long wifi_timeout = millis() + 30000;
  while (!wifi_is_connected() && millis() < wifi_timeout) {
    delay(500);
    wifi_loop();
  }

  if (!wifi_is_connected()) {
    DBG_PRINTLN("[main] WiFi not connected yet, continuing anyway (auto-reconnect enabled)");
  } else {
    DBG_PRINT("[main] WiFi ready, IP: ");
    DBG_PRINTLN(wifi_get_ip().c_str());
  }

  // mDNS (responder + node discovery)
  mdns_setup();

  // Mesh client (TCP to V3 nodes)
  mesh_client_init();
  mesh_client_set_callback(on_mesh_frame);

  // MQTT
  mqtt_setup();

  // HTTP status server
  status_setup();

  // Optional LCD status display
  lcd_setup();

  DBG_PRINTLN("[main] all modules initialized");
  last_heartbeat = millis();
}

void loop() {
  wifi_loop();
  mesh_client_loop();
  mqtt_loop();
  mdns_loop();
  status_loop();
  lcd_loop();

  // Periodic heartbeat log
  if (millis() - last_heartbeat > HEARTBEAT_MS) {
    last_heartbeat = millis();
    DBG_PRINT("[hb] wifi=");
    DBG_PRINT(wifi_is_connected() ? "ok" : "down");
    DBG_PRINT(" mqtt=");
    DBG_PRINT(mqtt_is_connected() ? "ok" : "down");
    DBG_PRINT(" lcd=");
    DBG_PRINT(lcd_is_ok() ? "ok" : "no");
    DBG_PRINT(" nodes=");
    for (int i = 0; i < mesh_client_node_count(); i++) {
      DBG_PRINT(i > 0 ? "," : "");
      DBG_PRINT(mesh_client_is_connected(i) ? "1" : "0");
    }
    DBG_PRINTLN();
  }

  yield();
}
