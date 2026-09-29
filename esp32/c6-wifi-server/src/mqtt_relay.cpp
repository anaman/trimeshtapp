// SPDX-License-Identifier: GPL-3.0-or-later
// mqtt_relay.cpp — MQTT publish of aggregated mesh frames
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include "config.h"
#include "mqtt_ca.h"

#ifdef MQTT_TLS
  #include <WiFiClientSecure.h>
  static WiFiClientSecure mqtt_wifi;
#else
  static WiFiClient mqtt_wifi;
#endif
static PubSubClient mqtt(mqtt_wifi);
static unsigned long last_reconnect = 0;
static uint32_t total_published = 0;

static String base64_encode(const uint8_t* data, size_t len) {
  static const char* chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  String out;
  out.reserve(((len + 2) / 3) * 4);
  for (size_t i = 0; i < len; i += 3) {
    uint32_t n = data[i] << 16;
    if (i + 1 < len) n |= data[i+1] << 8;
    if (i + 2 < len) n |= data[i+2];
    out += chars[(n >> 18) & 0x3F];
    out += chars[(n >> 12) & 0x3F];
    out += (i + 1 < len) ? chars[(n >> 6) & 0x3F] : '=';
    out += (i + 2 < len) ? chars[n & 0x3F] : '=';
  }
  return out;
}

void mqtt_setup() {
#ifdef MQTT_TLS
  #ifdef MQTT_TLS_INSECURE
    mqtt_wifi.setInsecure();
    DBG_PRINTLN("[mqtt] TLS (no cert verification)");
  #else
    mqtt_wifi.setCACert(MQTT_CA_PEM);
    DBG_PRINTLN("[mqtt] TLS (ISRG Root X2 pinned)");
  #endif
#endif
  mqtt.setServer(MQTT_BROKER, MQTT_PORT);
  mqtt.setBufferSize(512);
  DBG_PRINT("[mqtt] broker ");
  DBG_PRINT(MQTT_BROKER);
  DBG_PRINT(":");
  DBG_PRINTLN(MQTT_PORT);
}

void mqtt_loop() {
  if (mqtt.connected()) {
    mqtt.loop();
    return;
  }
  if (millis() - last_reconnect > MQTT_RECONNECT_MS) {
    DBG_PRINTLN("[mqtt] connecting...");
    bool ok;
    if (strlen(MQTT_USER) > 0) {
      ok = mqtt.connect(MQTT_CLIENT_ID, MQTT_USER, MQTT_PASS);
    } else {
      ok = mqtt.connect(MQTT_CLIENT_ID);
    }
    if (ok) {
      DBG_PRINTLN("[mqtt] connected");
    } else {
      DBG_PRINT("[mqtt] failed, state=");
      DBG_PRINTLN(mqtt.state());
    }
    last_reconnect = millis();
  }
}

bool mqtt_is_connected() {
  return mqtt.connected();
}

void mqtt_publish_frame(const char* source_ip, uint8_t* payload, size_t len) {
  if (!mqtt.connected()) return;

  JsonDocument doc;
  doc["source"] = source_ip;
  doc["direction"] = "outbound";
  doc["frame_len"] = (int)len;
  doc["frame_b64"] = base64_encode(payload, len);
  doc["ts"] = (long)millis();

  String topic = String(MQTT_TOPIC) + "/" + String(source_ip);
  String body;
  serializeJson(doc, body);

  if (mqtt.publish(topic.c_str(), body.c_str())) {
    total_published++;
    DBG_PRINT("[mqtt] published frame to ");
    DBG_PRINTLN(topic.c_str());
  } else {
    DBG_PRINTLN("[mqtt] publish failed");
  }
}

uint32_t mqtt_published_count() {
  return total_published;
}
