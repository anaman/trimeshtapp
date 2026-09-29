// SPDX-License-Identifier: GPL-3.0-or-later
// lcd_status.cpp — status display for the Waveshare ESP32-C6-LCD-1.47
//
// Waveshare ESP32-C6-LCD-1.47: ST7789 SPI panel, 172x320, 262K colour.
//   MOSI=GPIO6  SCLK=GPIO7  CS=GPIO14  DC=GPIO15  RST=GPIO21  BL=GPIO22
//   (RGB LED on GPIO8; TF card shares the SPI bus)
//
// Enabled with -D HAS_LCD=1. Shows hub status at a glance.
// Backlight is PWM'd to ~50% — Waveshare warn that full brightness overheats
// the panel and can leave shadows.
#include <Arduino.h>
#include "config.h"

#ifdef HAS_LCD
#include <Arduino_GFX_Library.h>

#ifndef LCD_MOSI
  #define LCD_MOSI 6
#endif
#ifndef LCD_SCLK
  #define LCD_SCLK 7
#endif
#ifndef LCD_CS
  #define LCD_CS 14
#endif
#ifndef LCD_DC
  #define LCD_DC 15
#endif
#ifndef LCD_RST
  #define LCD_RST 21
#endif
#ifndef LCD_BL
  #define LCD_BL 22
#endif
// 1.47" panel is 172 px wide inside the ST7789's 240 px frame -> 34 px offset
#ifndef LCD_COL_OFFSET
  #define LCD_COL_OFFSET 34
#endif
#ifndef LCD_BRIGHTNESS
  #define LCD_BRIGHTNESS 128   // 0-255, ~50%
#endif

static Arduino_DataBus *bus = nullptr;
static Arduino_GFX *gfx = nullptr;
static bool lcd_ok = false;
static unsigned long last_draw = 0;

// from other modules
extern bool wifi_is_connected();
extern String wifi_get_ip();
extern bool mqtt_is_connected();
extern uint32_t mqtt_published_count();
extern int mesh_client_node_count();
extern bool mesh_client_is_connected(int i);
extern uint32_t mesh_client_frame_count(int i);

void lcd_setup() {
  bus = new Arduino_ESP32SPI(LCD_DC, LCD_CS, LCD_SCLK, LCD_MOSI, GFX_NOT_DEFINED);
  gfx = new Arduino_ST7789(bus, LCD_RST, 0 /* portrait */, true /* IPS */,
                           172 /* width */, 320 /* height */,
                           LCD_COL_OFFSET, 0, LCD_COL_OFFSET, 0);
  if (!gfx->begin()) {
    DBG_PRINTLN("[lcd] ST7789 init failed");
    lcd_ok = false;
    return;
  }
  lcd_ok = true;

  // Backlight PWM (avoid full brightness per Waveshare thermal note)
  ledcAttach(LCD_BL, 5000, 8);
  ledcWrite(LCD_BL, LCD_BRIGHTNESS);

  gfx->fillScreen(RGB565_BLACK);
  gfx->setTextColor(RGB565_CYAN);
  gfx->setTextSize(3);
  gfx->setCursor(10, 20);
  gfx->println("C6");
  gfx->setTextSize(2);
  gfx->setCursor(10, 60);
  gfx->println("MESH HUB");
  gfx->setTextSize(1);
  gfx->setTextColor(RGB565_WHITE);
  gfx->setCursor(10, 95);
  gfx->println("booting...");
  DBG_PRINTLN("[lcd] ST7789 ready");
}

static void line(int y, uint16_t colour, uint8_t size, const char* label, const String& value) {
  gfx->setTextSize(size);
  gfx->setTextColor(colour);
  gfx->setCursor(8, y);
  gfx->print(label);
  gfx->print(value);
}

void lcd_loop() {
  if (!lcd_ok) return;
  if (millis() - last_draw < 1000) return;
  last_draw = millis();

  int n = mesh_client_node_count();
  int up = 0;
  uint32_t frames = 0;
  for (int i = 0; i < n; i++) {
    if (mesh_client_is_connected(i)) up++;
    frames += mesh_client_frame_count(i);
  }

  gfx->fillScreen(RGB565_BLACK);

  gfx->setTextColor(RGB565_CYAN);
  gfx->setTextSize(3);
  gfx->setCursor(8, 12);
  gfx->println("C6 HUB");
  gfx->drawFastHLine(0, 52, 172, RGB565_DARKGREY);

  line(64,  wifi_is_connected() ? RGB565_GREEN : RGB565_RED, 2, "WiFi ", wifi_is_connected() ? "OK" : "DOWN");
  gfx->setTextSize(1);
  gfx->setTextColor(RGB565_WHITE);
  gfx->setCursor(8, 88);
  gfx->println(wifi_is_connected() ? wifi_get_ip() : "-");

  line(104, mqtt_is_connected() ? RGB565_GREEN : RGB565_RED, 2, "MQTT ", mqtt_is_connected() ? "OK" : "DOWN");
  gfx->setTextSize(1);
  gfx->setTextColor(RGB565_WHITE);
  gfx->setCursor(8, 128);
  gfx->print("published: ");
  gfx->println(mqtt_published_count());

  line(148, (up > 0) ? RGB565_GREEN : RGB565_YELLOW, 2, "Nodes ", String(up) + "/" + String(n));

  line(184, (frames > 0) ? RGB565_GREEN : RGB565_YELLOW, 2, "Frames ", String(frames));

  gfx->drawFastHLine(0, 220, 172, RGB565_DARKGREY);
  gfx->setTextSize(1);
  gfx->setTextColor(RGB565_LIGHTGREY);
  gfx->setCursor(8, 232);
  gfx->print("uptime ");
  gfx->print(millis() / 1000 / 60);
  gfx->println(" min");
  gfx->setCursor(8, 246);
  gfx->print("ip ");
  gfx->println(wifi_get_ip());

}

bool lcd_is_ok() { return lcd_ok; }

#else  // !HAS_LCD
void lcd_setup() {}
void lcd_loop() {}
bool lcd_is_ok() { return false; }
#endif
