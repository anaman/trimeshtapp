// SPDX-License-Identifier: GPL-3.0-or-later
// status_server.cpp — HTTP status endpoint
#include <WiFi.h>
#include <WebServer.h>
#include <ArduinoJson.h>
#include "config.h"

// Forward declarations from mesh_client
extern int mesh_client_node_count();
extern bool mesh_client_is_connected(int i);
extern String mesh_client_node_ip(int i);
extern uint32_t mesh_client_frame_count(int i);
extern unsigned long mesh_client_last_frame(int i);
// From mqtt_relay
extern bool mqtt_is_connected();
extern uint32_t mqtt_published_count();
extern bool lcd_is_ok();
// From mesh_client ring buffer
extern uint32_t mesh_frames_total();
extern int mesh_frames_since(uint32_t since_seq, int max_n, FrameRecord* out);

static String b64(const uint8_t* data, size_t len) {
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
// From wifi_client
extern bool wifi_is_connected();
extern String wifi_get_ip();

static WebServer server(STATUS_PORT);

static unsigned long boot_time = 0;

void status_setup() {
  boot_time = millis();

  server.on("/", []() {
    String html = "<!DOCTYPE html><html><head><title>C6 Mesh Server</title>";
    html += "<meta http-equiv=\"refresh\" content=\"5\">";
    html += "<style>";
    html += "body{font-family:monospace;background:#1a1a2e;color:#e0e0e0;padding:20px}";
    html += "h1{color:#0f3460} .ok{color:#16c79a} .bad{color:#e74c3c}";
    html += "table{border-collapse:collapse;width:100%}";
    html += "th,td{border:1px solid #333;padding:8px;text-align:left}";
    html += "th{background:#16213e}";
    html += "</style></head><body>";
    html += "<h1>ESP32-C6 Mesh Server</h1>";

    html += "<p>WiFi: ";
    html += wifi_is_connected() ? "<span class='ok'>connected</span>" : "<span class='bad'>disconnected</span>";
    html += " (IP: " + wifi_get_ip() + ")</p>";

    html += "<p>MQTT: ";
    html += mqtt_is_connected() ? "<span class='ok'>connected</span>" : "<span class='bad'>disconnected</span>";
    html += " (published: " + String(mqtt_published_count()) + ")</p>";

    unsigned long uptime_s = (millis() - boot_time) / 1000;
    html += "<p>Uptime: " + String(uptime_s / 3600) + "h " + String((uptime_s % 3600) / 60) + "m " + String(uptime_s % 60) + "s</p>";

    int n = mesh_client_node_count();
    html += "<h2>Nodes (" + String(n) + ")</h2>";
    html += "<table><tr><th>#</th><th>IP</th><th>Status</th><th>Frames</th><th>Last frame</th></tr>";
    for (int i = 0; i < n; i++) {
      bool conn = mesh_client_is_connected(i);
      unsigned long last = mesh_client_last_frame(i);
      String last_str = last ? String((millis() - last) / 1000) + "s ago" : "never";
      html += String("<tr><td>") + String(i) + "</td><td>" + mesh_client_node_ip(i) + "</td>";
      html += String("<td>") + (conn ? "<span class='ok'>connected</span>" : "<span class='bad'>disconnected</span>") + "</td>";
      html += String("<td>") + String(mesh_client_frame_count(i)) + "</td>";
      html += String("<td>") + last_str + "</td></tr>";
    }
    html += "</table>";
    html += "<p><a href='/status'>/status</a> &middot; <a href='/health'>/health</a> &middot; <a href='/nodes'>/nodes</a> &middot; <a href='/frames'>/frames</a></p>";
    html += "</body></html>";
    server.send(200, "text/html", html);
  });

  server.on("/status", []() {
    JsonDocument doc;
    doc["wifi_connected"] = wifi_is_connected();
    doc["wifi_ip"] = wifi_get_ip();
    doc["mqtt_connected"] = mqtt_is_connected();
    doc["mqtt_published"] = (long)mqtt_published_count();
    doc["uptime_s"] = (long)((millis() - boot_time) / 1000);
    doc["lcd"] = lcd_is_ok();

    JsonArray arr = doc["nodes"].to<JsonArray>();
    for (int i = 0; i < mesh_client_node_count(); i++) {
      JsonObject obj = arr.add<JsonObject>();
      obj["ip"] = mesh_client_node_ip(i);
      obj["connected"] = mesh_client_is_connected(i);
      obj["frames"] = (long)mesh_client_frame_count(i);
      obj["last_frame_ms_ago"] = (long)(mesh_client_last_frame(i) ? (millis() - mesh_client_last_frame(i)) : 0);
    }

    String body;
    serializeJson(doc, body);
    server.send(200, "application/json", body);
  });

  server.on("/health", []() {
    server.send(200, "application/json", "{\"ok\":true}");
  });

  // Serve buffered mesh frames (for host-side pull integrations).
  //   GET /frames            -> newest up to FRAME_RING_SIZE frames
  //   GET /frames?since=SEQ  -> frames with seq >= SEQ
  server.on("/frames", []() {
    uint32_t since = 0;
    if (server.hasArg("since")) {
      since = strtoul(server.arg("since").c_str(), nullptr, 10);
    }
    static FrameRecord buf[FRAME_RING_SIZE];
    int n = mesh_frames_since(since, FRAME_RING_SIZE, buf);

    JsonDocument doc;
    doc["total"] = (long)mesh_frames_total();
    JsonArray arr = doc["frames"].to<JsonArray>();
    for (int i = 0; i < n; i++) {
      JsonObject o = arr.add<JsonObject>();
      o["seq"] = (long)buf[i].seq;
      o["source"] = buf[i].source;
      o["len"] = buf[i].len;
      o["frame_b64"] = b64(buf[i].data, buf[i].len);
      o["ms_ago"] = (long)(millis() - buf[i].ts);
    }
    String body;
    serializeJson(doc, body);
    server.send(200, "application/json", body);
  });

  server.on("/nodes", []() {
    JsonDocument doc;
    JsonArray arr = doc.to<JsonArray>();
    for (int i = 0; i < mesh_client_node_count(); i++) {
      JsonObject obj = arr.add<JsonObject>();
      obj["ip"] = mesh_client_node_ip(i);
      obj["connected"] = mesh_client_is_connected(i);
      obj["frames"] = (long)mesh_client_frame_count(i);
    }
    String body;
    serializeJson(doc, body);
    server.send(200, "application/json", body);
  });

  server.begin();
  DBG_PRINT("[http] status server on port ");
  DBG_PRINTLN(STATUS_PORT);
}

void status_loop() {
  server.handleClient();
}
