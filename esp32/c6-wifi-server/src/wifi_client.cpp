// SPDX-License-Identifier: GPL-3.0-or-later
// wifi_client.cpp — WiFi station connect with auto-reconnect
#include <WiFi.h>
#include "config.h"

static unsigned long last_reconnect = 0;
static bool wifi_connected = false;

void wifi_setup() {
  WiFi.setAutoReconnect(true);
  WiFi.mode(WIFI_STA);

  WiFi.onEvent([](WiFiEvent_t event, WiFiEventInfo_t info) {
    if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
      DBG_PRINTLN("[wifi] disconnected, will reconnect");
      wifi_connected = false;
    } else if (event == ARDUINO_EVENT_WIFI_STA_GOT_IP) {
      DBG_PRINT("[wifi] connected, IP: ");
      DBG_PRINTLN(WiFi.localIP().toString().c_str());
      wifi_connected = true;
    }
  }, WiFiEvent_t::ARDUINO_EVENT_WIFI_STA_DISCONNECTED);

  WiFi.onEvent([](WiFiEvent_t event, WiFiEventInfo_t info) {
    DBG_PRINT("[wifi] got IP: ");
    DBG_PRINTLN(WiFi.localIP().toString().c_str());
    wifi_connected = true;
  }, WiFiEvent_t::ARDUINO_EVENT_WIFI_STA_GOT_IP);

  WiFi.begin(WIFI_SSID, WIFI_PWD);
  DBG_PRINT("[wifi] connecting to ");
  DBG_PRINTLN(WIFI_SSID);
}

void wifi_loop() {
  if (!wifi_connected && (millis() - last_reconnect > WIFI_RECONNECT_MS)) {
    DBG_PRINTLN("[wifi] attempting reconnect");
    WiFi.disconnect();
    WiFi.reconnect();
    last_reconnect = millis();
  }
}

bool wifi_is_connected() {
  return wifi_connected && WiFi.status() == WL_CONNECTED;
}

String wifi_get_ip() {
  return WiFi.localIP().toString();
}
