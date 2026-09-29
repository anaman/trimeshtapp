// SPDX-License-Identifier: GPL-3.0-or-later
// mdns_discovery.cpp — discover MeshCore V3 nodes via mDNS
//
// The Heltec V3 nodes advertise their WiFi interface as a `_meshcore._tcp`
// mDNS service (see the MeshCore patch: companion_radio advertises
// MDNS.addService("meshcore", "tcp", TCP_PORT) when MDNS_ADVERTISE is defined).
//
// This module periodically queries for those services and registers the
// discovered IPs with the mesh client, so node addresses do not need to be
// hard-coded.
#include <Arduino.h>
#include <ESPmDNS.h>
#include "config.h"

extern bool mesh_client_add_node(const char* ip);
extern int mesh_client_node_count();

static unsigned long last_scan = 0;

void mdns_setup() {
  if (!MDNS.begin(MDNS_NAME)) {
    DBG_PRINTLN("[mdns] responder failed to start");
    return;
  }
  MDNS.addService("http", "tcp", STATUS_PORT);
  DBG_PRINT("[mdns] responder started as ");
  DBG_PRINT(MDNS_NAME);
  DBG_PRINTLN(".local");
  // first discovery pass shortly after boot
  last_scan = millis() - MDNS_SCAN_MS;
}

void mdns_loop() {
  if (millis() - last_scan < MDNS_SCAN_MS) return;
  last_scan = millis();

  int n = MDNS.queryService("meshcore", "tcp");
  if (n <= 0) return;

  int added = 0;
  for (int i = 0; i < n; i++) {
    IPAddress ip = MDNS.address(i);
    if (ip == INADDR_NONE) continue;
    String s = ip.toString();
    if (mesh_client_add_node(s.c_str())) added++;
  }
  DBG_PRINT("[mdns] scan: ");
  DBG_PRINT(n);
  DBG_PRINT(" service(s), ");
  DBG_PRINT(added);
  DBG_PRINTLN(" new");
}
