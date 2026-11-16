#include <SPI.h>
#include <XPT2046_Touchscreen.h>
#include <Preferences.h>
#include <esp_arduino_version.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include <new>
#include <Adafruit_ILI9341.h>
#include <lvgl.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <math.h>
#include <string.h>

const char* WIFI_NAME = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* METRICS_URL = "http://PI_LAN_IP:8091/monitor/api/v1/metrics";

uint16_t BACKGROUND = 0x0842;
uint16_t PANEL = 0x1083;
uint16_t BORDER = 0x3A29;
constexpr uint16_t WHITE = 0xEFFF;
constexpr uint16_t MUTED = 0x9D57;
uint16_t VIOLET = 0xD43E;
uint16_t SAND = 0xF6DA;
constexpr uint16_t GREEN = 0x67EC;
constexpr uint16_t AMBER = 0xFDC6;
constexpr unsigned long POLL_MS = 2000;
constexpr unsigned long STALE_MS = 20000;

SPIClass displaySPI(HSPI);
SPIClass touchSPI(VSPI);
XPT2046_Touchscreen touch(33, 36);
Preferences preferences;
Adafruit_ILI9341 display(&displaySPI, 2, 15);
JsonDocument filter;
uint32_t lastSample = 0;
unsigned long lastFreshAt = 0;

constexpr uint8_t MIN_LIGHT = 5;
constexpr unsigned long WAKE_MS = 30000;
constexpr uint8_t LIGHT_PIN = 21;
constexpr uint8_t LIGHT_CHANNEL = 0;



struct Theme {
  const char* name;
  uint16_t background, panel, border, cpu, ram;
};
const Theme THEMES[] = {
  {"Amethyst",   0x0842, 0x1083, 0x3A29, 0xD43E, 0xF6DA},
  {"Aurora",     0x0844, 0x10C8, 0x2C32, 0x3FDD, 0x77F3},
  {"Ember",      0x1082, 0x20C4, 0x6188, 0xFDA5, 0xFB8E},
  {"Ocean",      0x0845, 0x1109, 0x39F5, 0x4DFF, 0xA57F},
  {"Terminal",   0x0821, 0x10A3, 0x3328, 0x4FEA, 0xB7F5},
  {"Monochrome", 0x0841, 0x18C3, 0x4A49, 0xFFFF, 0xBDF7},
};
constexpr uint8_t THEME_COUNT = sizeof(THEMES) / sizeof(THEMES[0]);

struct Settings {
  uint8_t theme = 0;
  uint8_t brightness = 70;
  bool night = false;
  uint16_t start = 23 * 60;
  uint16_t end = 7 * 60;
};
Settings saved;
Settings draft;

bool menuOpen = false;
bool touchDown = false;
bool wakeOnly = false;
bool pwmReady = false;
bool storageReady = false;
uint8_t currentLight = 0;
unsigned long wakeAt = 0;
bool awakeByTouch = false;
uint32_t clockEpoch = 0;
unsigned long clockAt = 0;
uint32_t drawnMinute = UINT32_MAX;

struct FetchResult {
  JsonDocument* document;
  int status;
};
QueueHandle_t metricsQueue = nullptr;

void applyTheme(uint8_t index) {
  const Theme& selected = THEMES[index];
  BACKGROUND = selected.background;
  PANEL = selected.panel;
  BORDER = selected.border;
  VIOLET = selected.cpu;
  SAND = selected.ram;
}

bool initBrightness() {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  return ledcAttach(LIGHT_PIN, 5000, 8);
#else
  if (ledcSetup(LIGHT_CHANNEL, 5000, 8) == 0) {
    return false;
  }
  ledcAttachPin(LIGHT_PIN, LIGHT_CHANNEL);
  return true;
#endif
}

void writeBrightness(uint8_t percent) {
  if (!pwmReady || currentLight == percent) {
    return;
  }
  currentLight = percent;
  const uint32_t duty = (uint32_t(percent) * 255 + 50) / 100;
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWrite(LIGHT_PIN, duty);
#else
  ledcWrite(LIGHT_CHANNEL, duty);
#endif
}

uint32_t localClock() {
  return clockEpoch ? clockEpoch + (millis() - clockAt) / 1000 + 3 * 3600 : 0;
}

bool nightDimmed() {
  if (!saved.night || !clockEpoch || saved.start == saved.end) {
    return false;
  }
  const uint16_t minute = (localClock() / 60) % 1440;
  const bool nightHours = saved.start < saved.end
    ? minute >= saved.start && minute < saved.end
    : minute >= saved.start || minute < saved.end;
  return nightHours && !(awakeByTouch && millis() - wakeAt < WAKE_MS);
}

void updateBrightness() {
  writeBrightness(nightDimmed() ? MIN_LIGHT : (menuOpen ? draft.brightness : saved.brightness));
}

void readSettings() {
  storageReady = preferences.begin("netwatch-cyd", false);
  if (storageReady) {
    saved.theme = min(preferences.getUChar("theme", 0), uint8_t(THEME_COUNT - 1));
    saved.brightness = constrain(preferences.getUChar("light", 70), MIN_LIGHT, 100);
    saved.night = preferences.getBool("night", false);
    saved.start = preferences.getUShort("start", 1380) % 1440;
    saved.end = preferences.getUShort("end", 420) % 1440;
  }
  applyTheme(saved.theme);
}

void saveSettings() {
  saved = draft;
  if (storageReady) {
    preferences.putUChar("theme", saved.theme);
    preferences.putUChar("light", saved.brightness);
    preferences.putBool("night", saved.night);
    preferences.putUShort("start", saved.start);
    preferences.putUShort("end", saved.end);
  }
}



struct Gauge {
  lv_obj_t* arc;
  lv_obj_t* name;
  lv_obj_t* value;
  lv_obj_t* detail;
};
struct Tile {
  lv_obj_t* panel;
  lv_obj_t* label;
  lv_obj_t* value;
};

alignas(4) uint16_t drawBuffer[320 * 16];
lv_obj_t* dashboard;
lv_obj_t* menu;
lv_obj_t* clockLabel;
lv_obj_t* statusLabel;
lv_obj_t* brightnessLabel;
lv_obj_t* brightnessSlider;
lv_obj_t* nightLabel;
lv_obj_t* timeLabels[2];
lv_obj_t* themeButtons[THEME_COUNT];
Gauge gauges[2];
Tile tiles[6];

#if LV_FONT_MONTSERRAT_24
const lv_font_t* const largeFont = &lv_font_montserrat_24;
#else
const lv_font_t* const largeFont = LV_FONT_DEFAULT;
#endif
#if LV_FONT_MONTSERRAT_12
const lv_font_t* const tileFont = &lv_font_montserrat_12;
#else
const lv_font_t* const tileFont = LV_FONT_DEFAULT;
#endif

lv_color_t rgb(uint16_t color) {
  return lv_color_make(((color >> 11) & 31) * 255 / 31,
                       ((color >> 5) & 63) * 255 / 63,
                       (color & 31) * 255 / 31);
}

lv_obj_t* label(lv_obj_t* parent, const char* text, uint16_t color, const lv_font_t* font) {
  lv_obj_t* obj = lv_label_create(parent);
  lv_obj_set_style_text_font(obj, font, 0);
  lv_obj_set_style_text_color(obj, rgb(color), 0);
  lv_label_set_text(obj, text);
  return obj;
}

void setText(lv_obj_t* obj, const char* text) {
  if (strcmp(lv_label_get_text(obj), text) != 0) {
    lv_label_set_text(obj, text);
  }
}

void status(const char* text, uint16_t color) {
  setText(statusLabel, text);
  if (!lv_color_eq(lv_obj_get_style_text_color(statusLabel, 0), rgb(color))) {
    lv_obj_set_style_text_color(statusLabel, rgb(color), 0);
  }
}

void drawGauge(int index, float percent, const char* detail) {
  Gauge& gauge = gauges[index];
  char value[16] = "--";
  int rounded = 0;
  if (isfinite(percent)) {
    rounded = int(lroundf(constrain(percent, 0.0f, 100.0f)));
    snprintf(value, sizeof(value), "%d%%", rounded);
  }
  if (lv_arc_get_value(gauge.arc) != rounded) {
    lv_arc_set_value(gauge.arc, rounded);
  }
  setText(gauge.value, value);
  setText(gauge.detail, detail);
}

void tile(int index, const char* value, uint16_t color) {
  lv_obj_t* obj = tiles[index].value;
  setText(obj, value);
  if (!lv_color_eq(lv_obj_get_style_text_color(obj, 0), rgb(color))) {
    lv_obj_set_style_text_color(obj, rgb(color), 0);
  }
}

void flushDisplay(lv_display_t* screen, const lv_area_t* area, uint8_t* pixels) {
  const uint16_t width = area->x2 - area->x1 + 1;
  const uint16_t height = area->y2 - area->y1 + 1;
  display.startWrite();
  display.setAddrWindow(area->x1, area->y1, width, height);
  display.writePixels(reinterpret_cast<uint16_t*>(pixels), uint32_t(width) * height);
  display.endWrite();
  lv_display_flush_ready(screen);
}

void readTouch(lv_indev_t*, lv_indev_data_t* data) {
  const bool down = touch.touched();
  data->state = LV_INDEV_STATE_RELEASED;
  if (!down) {
    touchDown = false;
    wakeOnly = false;
    return;
  }
  if (!touchDown) {
    wakeOnly = nightDimmed();
  }
  touchDown = true;
  wakeAt = millis();
  awakeByTouch = true;
  if (wakeOnly) {
    return;
  }
  const TS_Point point = touch.getPoint();
  const int y = constrain(map(point.y, 683, 3346, 24, 215), 0, 239);
  const long left = map(y, 24, 215, 599, 479);
  const long right = map(y, 24, 215, 3334, 3438);
  data->point.x = constrain(map(point.x, left, right, 24, 295), 0, 319);
  data->point.y = y;
  data->state = LV_INDEV_STATE_PRESSED;
}

void formatRate(char* output, size_t length, JsonVariantConst value) {
  if (value.isNull()) {
    snprintf(output, length, "--");
  } else {
    const float rate = value.as<float>();
    if (rate >= 1048576.0f) {
      snprintf(output, length, "%.1f MiB/s", rate / 1048576.0f);
    } else {
      snprintf(output, length, "%.1f KiB/s", rate / 1024.0f);
    }
  }
}

void drawMetrics(const JsonDocument& metrics, uint32_t sampledAt) {
  if (sampledAt == lastSample) {
    if (millis() - lastFreshAt >= STALE_MS) {
      status("STALE DATA", AMBER);
    } else {
      status("LIVE", GREEN);
    }
    return;
  }
  lastFreshAt = millis();
  lastSample = sampledAt;

  const float ramUsed = metrics["memory"]["used_bytes"] | NAN;
  const float ramTotal = metrics["memory"]["total_bytes"] | NAN;
  const float ramPercent = ramTotal > 0 ? 100.0f * ramUsed / ramTotal : NAN;
  char ramDetail[24] = "USED";
  if (ramTotal > 0) {
    snprintf(ramDetail, sizeof(ramDetail), "OF %.0f MiB", ramTotal / 1048576.0f);
  }

  drawGauge(0, metrics["cpu_percent"] | NAN, "SYSTEM LOAD");
  drawGauge(1, ramPercent, ramDetail);

  const float temperature = metrics["temperature_c"] | NAN;
  char temp[16] = "--";
  if (isfinite(temperature)) {
    snprintf(temp, sizeof(temp), "%.1f C", temperature);
  }

  char diskUsage[16] = "--";
  float usbFree = NAN;
  for (JsonObjectConst disk : metrics["storage"].as<JsonArrayConst>()) {
    if (disk["available"] != true) {
      continue;
    }
    const char* mount = disk["mount"] | "";
    const char* label = disk["label"] | "";
    if (strcmp(mount, "/") == 0) {
      const float used = disk["used_bytes"] | NAN;
      const float total = disk["total_bytes"] | NAN;
      if (isfinite(used) && total > 0) {
        snprintf(diskUsage, sizeof(diskUsage), "%.0f%%", 100.0f * used / total);
      }
    } else if (strcmp(mount, "/mnt/netwatch-backup") == 0) {
      usbFree = disk["available_bytes"] | NAN;
    } else if (!isfinite(usbFree) && strncmp(label, "USB", 3) == 0) {
      usbFree = disk["available_bytes"] | NAN;
    }
  }
  char usb[18] = "--";
  if (isfinite(usbFree)) {
    snprintf(usb, sizeof(usb), "%.1f GiB", usbFree / 1073741824.0f);
  }

  char rx[16] = "--";
  char tx[16] = "--";
  for (JsonObjectConst link : metrics["network"].as<JsonArrayConst>()) {
    if (link["interface"] == "eth0") {
      formatRate(rx, sizeof(rx), link["rx_bytes_per_s"]);
      formatRate(tx, sizeof(tx), link["tx_bytes_per_s"]);
      break;
    }
  }

  const unsigned long seconds = metrics["uptime_seconds"].as<unsigned long>();
  char uptime[20];
  snprintf(uptime, sizeof(uptime), "%lud %02luh %02lum",
           seconds / 86400, seconds / 3600 % 24, seconds / 60 % 60);

  tile(0, temp, temperature >= 70.0f ? AMBER : GREEN);
  tile(1, diskUsage, WHITE);
  tile(2, uptime, WHITE);
  tile(3, usb, WHITE);
  tile(4, rx, WHITE);
  tile(5, tx, WHITE);
  status("LIVE", GREEN);
}

void updateClock() {
  const uint32_t time = localClock();
  const uint32_t minute = time ? time / 60 : UINT32_MAX - 1;
  if (minute == drawnMinute) {
    return;
  }
  drawnMinute = minute;
  char value[8] = "--:--";
  if (time) {
    snprintf(value, sizeof(value), "%02u:%02u", unsigned(minute / 60 % 24), unsigned(minute % 60));
  }
  setText(clockLabel, value);
}

lv_obj_t* newScreen() {
  lv_obj_t* screen = lv_obj_create(nullptr);
  lv_obj_remove_style_all(screen);
  lv_obj_remove_flag(screen, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_set_style_bg_opa(screen, LV_OPA_COVER, 0);
  return screen;
}

void stylePanel(lv_obj_t* panel) {
  lv_obj_set_style_bg_color(panel, rgb(PANEL), 0);
  lv_obj_set_style_border_color(panel, rgb(BORDER), 0);
  lv_obj_set_style_border_width(panel, 1, 0);
  lv_obj_set_style_radius(panel, 5, 0);
}

void updateThemeStyles(uint8_t index) {
  applyTheme(index);
  lv_obj_set_style_bg_color(dashboard, rgb(BACKGROUND), 0);
  lv_obj_set_style_bg_color(menu, rgb(BACKGROUND), 0);
  lv_obj_set_style_bg_color(brightnessSlider, rgb(BORDER), LV_PART_MAIN);
  lv_obj_set_style_bg_color(brightnessSlider, rgb(VIOLET), LV_PART_INDICATOR);
  lv_obj_set_style_bg_color(brightnessSlider, rgb(WHITE), LV_PART_KNOB);
  for (int i = 0; i < 2; ++i) {
    const uint16_t color = i == 0 ? VIOLET : SAND;
    lv_obj_set_style_arc_color(gauges[i].arc, rgb(BORDER), LV_PART_MAIN);
    lv_obj_set_style_arc_color(gauges[i].arc, rgb(color), LV_PART_INDICATOR);
    lv_obj_set_style_text_color(gauges[i].name, rgb(color), 0);
  }
  for (Tile& item : tiles) {
    stylePanel(item.panel);
  }
  for (uint8_t i = 0; i < THEME_COUNT; ++i) {
    stylePanel(themeButtons[i]);
    if (i == index) {
      lv_obj_set_style_border_color(themeButtons[i], rgb(VIOLET), 0);
      lv_obj_set_style_border_width(themeButtons[i], 2, 0);
    }
  }
}

void refreshMenuLabels() {
  char brightness[24];
  snprintf(brightness, sizeof(brightness), "Brightness %u%%", unsigned(draft.brightness));
  setText(brightnessLabel, brightness);
  setText(nightLabel, draft.night ? "Night mode: ON" : "Night mode: OFF");
  const uint16_t minutes[] = {draft.start, draft.end};
  for (int i = 0; i < 2; ++i) {
    char time[8];
    snprintf(time, sizeof(time), "%02u:%02u", unsigned(minutes[i] / 60), unsigned(minutes[i] % 60));
    setText(timeLabels[i], time);
  }
}

void menuAction(lv_event_t* event) {
  const int action = int(reinterpret_cast<intptr_t>(lv_event_get_user_data(event)));
  if (action == -1) {
    draft = saved;
    menuOpen = true;
    lv_slider_set_value(brightnessSlider, draft.brightness, LV_ANIM_OFF);
    refreshMenuLabels();
    lv_screen_load(menu);
  } else if (action == 10 || action == 11) {
    if (action == 11) {
      saveSettings();
    }
    updateThemeStyles(saved.theme);
    refreshMenuLabels();
    menuOpen = false;
    lv_screen_load(dashboard);
  } else {
    if (action < THEME_COUNT) {
      draft.theme = action;
      updateThemeStyles(draft.theme);
    } else if (action == 12) {
      draft.night = !draft.night;
    } else {
      uint16_t& minute = action < 15 ? draft.start : draft.end;
      minute = (int(minute) + (action % 2 ? -15 : 15) + 1440) % 1440;
    }
    refreshMenuLabels();
  }
}

void brightnessChanged(lv_event_t*) {
  draft.brightness = lv_slider_get_value(brightnessSlider);
  refreshMenuLabels();
}

lv_obj_t* button(int x, int y, int w, int h, const char* text, int action) {
  lv_obj_t* obj = lv_button_create(menu);
  lv_obj_set_pos(obj, x, y);
  lv_obj_set_size(obj, w, h);
  stylePanel(obj);
  lv_obj_center(label(obj, text, WHITE, LV_FONT_DEFAULT));
  lv_obj_add_event_cb(obj, menuAction, LV_EVENT_CLICKED, reinterpret_cast<void*>(intptr_t(action)));
  return obj;
}

void buildInterface() {
  dashboard = newScreen();
  menu = newScreen();
  lv_obj_set_pos(label(dashboard, "NETWATCH / PI", MUTED, LV_FONT_DEFAULT), 9, 5);
  clockLabel = label(dashboard, "--:--", WHITE, largeFont);
  lv_obj_align(clockLabel, LV_ALIGN_TOP_MID, 0, 0);
  statusLabel = label(dashboard, "CONNECTING", MUTED, LV_FONT_DEFAULT);
  lv_obj_align(statusLabel, LV_ALIGN_TOP_RIGHT, -8, 5);

  for (int i = 0; i < 2; ++i) {
    const int x = 8 + 156 * i;
    Gauge& gauge = gauges[i];
    gauge.arc = lv_arc_create(dashboard);
    lv_obj_remove_style(gauge.arc, nullptr, LV_PART_KNOB);
    lv_obj_remove_flag(gauge.arc, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(gauge.arc, 108, 108);
    lv_obj_set_pos(gauge.arc, x + 20, 26);
    lv_arc_set_bg_angles(gauge.arc, 135, 45);
    lv_arc_set_range(gauge.arc, 0, 100);
    lv_arc_set_value(gauge.arc, 0);
    lv_obj_set_style_arc_width(gauge.arc, 5, LV_PART_MAIN);
    lv_obj_set_style_arc_width(gauge.arc, 5, LV_PART_INDICATOR);
    gauge.name = label(dashboard, i == 0 ? "CPU" : "RAM", WHITE, LV_FONT_DEFAULT);
    gauge.value = label(dashboard, "--", WHITE, largeFont);
    gauge.detail = label(dashboard, i == 0 ? "SYSTEM LOAD" : "USED", MUTED, LV_FONT_DEFAULT);
    lv_obj_align_to(gauge.name, gauge.arc, LV_ALIGN_CENTER, 0, -23);
    lv_obj_set_width(gauge.value, 96);
    lv_obj_set_width(gauge.detail, 96);
    lv_obj_set_style_text_align(gauge.value, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_align(gauge.detail, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align_to(gauge.value, gauge.arc, LV_ALIGN_CENTER, 0, 2);
    lv_obj_align_to(gauge.detail, gauge.arc, LV_ALIGN_CENTER, 0, 26);
  }
  lv_obj_t* cpuTouch = lv_obj_create(dashboard);
  lv_obj_remove_style_all(cpuTouch);
  lv_obj_remove_flag(cpuTouch, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_add_flag(cpuTouch, LV_OBJ_FLAG_CLICKABLE);
  lv_obj_set_pos(cpuTouch, 8, 25);
  lv_obj_set_size(cpuTouch, 148, 112);
  lv_obj_add_event_cb(cpuTouch, menuAction, LV_EVENT_CLICKED, reinterpret_cast<void*>(intptr_t(-1)));

  const char* names[] = {"TEMP", "DISK", "UP", "USB FREE", "ETH RX", "ETH TX"};
  for (int i = 0; i < 6; ++i) {
    Tile& item = tiles[i];
    item.panel = lv_obj_create(dashboard);
    lv_obj_remove_style_all(item.panel);
    lv_obj_remove_flag(item.panel, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_opa(item.panel, LV_OPA_COVER, 0);
    lv_obj_set_size(item.panel, 148, 26);
    lv_obj_set_pos(item.panel, 8 + 156 * (i % 2), 143 + 31 * (i / 2));
    item.label = label(item.panel, names[i], MUTED, tileFont);
    item.value = label(item.panel, "--", WHITE, tileFont);
    lv_obj_align(item.label, LV_ALIGN_LEFT_MID, 6, 0);
    lv_obj_align(item.value, LV_ALIGN_RIGHT_MID, -6, 0);
  }

  button(8, 4, 61, 23, "BACK", 10);
  lv_obj_align(label(menu, "SETTINGS", WHITE, LV_FONT_DEFAULT), LV_ALIGN_TOP_MID, 0, 8);
  button(251, 4, 61, 23, "SAVE", 11);
  for (uint8_t i = 0; i < THEME_COUNT; ++i) {
    themeButtons[i] = button(8 + 103 * (i % 3), 32 + 24 * (i / 3), 98, 22, THEMES[i].name, i);
  }
  brightnessLabel = label(menu, "", WHITE, LV_FONT_DEFAULT);
  lv_obj_set_pos(brightnessLabel, 10, 83);
  brightnessSlider = lv_slider_create(menu);
  lv_obj_set_size(brightnessSlider, 270, 10);
  lv_obj_set_pos(brightnessSlider, 25, 107);
  lv_slider_set_range(brightnessSlider, MIN_LIGHT, 100);
  lv_obj_add_event_cb(brightnessSlider, brightnessChanged, LV_EVENT_VALUE_CHANGED, nullptr);
  nightLabel = lv_obj_get_child(button(8, 126, 304, 24, "", 12), 0);
  for (int i = 0; i < 2; ++i) {
    const int y = 156 + i * 30;
    lv_obj_set_pos(label(menu, i == 0 ? "START" : "END", WHITE, LV_FONT_DEFAULT), 13, y + 4);
    button(122, y, 36, 25, "-", 13 + i * 2);
    timeLabels[i] = label(menu, "", WHITE, LV_FONT_DEFAULT);
    lv_obj_set_pos(timeLabels[i], 177, y + 4);
    button(246, y, 36, 25, "+", 14 + i * 2);
  }
  lv_obj_set_pos(label(menu, "MSK   NIGHT 5%   TAP TO WAKE", MUTED, LV_FONT_DEFAULT), 10, 220);
  updateThemeStyles(saved.theme);
  lv_screen_load(dashboard);
}

void fetchMetrics(void*) {
  for (;;) {
    const TickType_t started = xTaskGetTickCount();
    FetchResult result{nullptr, -1000};
    if (WiFi.status() == WL_CONNECTED) {
      WiFiClient client;
      HTTPClient http;
      http.useHTTP10(true);
      http.setConnectTimeout(2000);
      http.setTimeout(2500);
      if (http.begin(client, METRICS_URL)) {
        result.status = http.GET();
        if (result.status == HTTP_CODE_OK) {
          result.document = new (std::nothrow) JsonDocument;
          if (!result.document || deserializeJson(*result.document, http.getStream(),
                  DeserializationOption::Filter(filter))
              || !(*result.document)["sampled_at"].as<uint32_t>()) {
            delete result.document;
            result.document = nullptr;
            result.status = -1001;
          }
        }
        http.end();
      } else {
        result.status = -1002;
      }
    }
    if (xQueueSend(metricsQueue, &result, 0) != pdTRUE) {
      delete result.document;
    }
    const TickType_t elapsed = xTaskGetTickCount() - started;
    const TickType_t interval = pdMS_TO_TICKS(POLL_MS);
    vTaskDelay(elapsed < interval ? interval - elapsed : 1);
  }
}

void receiveMetrics() {
  if (!metricsQueue) {
    return;
  }
  FetchResult result;
  if (xQueueReceive(metricsQueue, &result, 0) != pdTRUE) {
    return;
  }
  if (result.document) {
    const uint32_t sample = (*result.document)["sampled_at"].as<uint32_t>();
    if (clockEpoch != sample) {
      clockEpoch = sample;
      clockAt = millis();
    }
    drawMetrics(*result.document, sample);
    delete result.document;
  } else {
    const char* message = "PI OFFLINE";
    if (result.status == -1000) {
      message = "WI-FI DOWN";
    } else if (result.status == -1001) {
      message = "INVALID DATA";
    } else if (result.status == HTTP_CODE_FORBIDDEN) {
      message = "ACCESS DENIED";
    }
    status(message, AMBER);
  }
}

void setup() {
  Serial.begin(115200);
  readSettings();
  pwmReady = initBrightness();
  if (pwmReady) {
    updateBrightness();
  } else {
    pinMode(LIGHT_PIN, OUTPUT);
    digitalWrite(LIGHT_PIN, HIGH);
  }
  displaySPI.begin(14, 12, 13, 15);
  display.begin(20000000);
  display.setRotation(1);
  touchSPI.begin(25, 39, 32, 33);
  touch.begin(touchSPI);
  touch.setRotation(1);

  lv_init();
  lv_tick_set_cb([]() -> uint32_t { return millis(); });
  lv_display_t* screen = lv_display_create(320, 240);
  lv_display_set_color_format(screen, LV_COLOR_FORMAT_RGB565);
  lv_display_set_buffers(screen, drawBuffer, nullptr, sizeof(drawBuffer), LV_DISPLAY_RENDER_MODE_PARTIAL);
  lv_display_set_flush_cb(screen, flushDisplay);
  lv_indev_t* pointer = lv_indev_create();
  lv_indev_set_type(pointer, LV_INDEV_TYPE_POINTER);
  lv_indev_set_read_cb(pointer, readTouch);
  buildInterface();

  filter["sampled_at"] = true;
  filter["cpu_percent"] = true;
  filter["temperature_c"] = true;
  filter["uptime_seconds"] = true;
  filter["memory"]["used_bytes"] = true;
  filter["memory"]["total_bytes"] = true;
  filter["storage"][0]["mount"] = true;
  filter["storage"][0]["label"] = true;
  filter["storage"][0]["available"] = true;
  filter["storage"][0]["used_bytes"] = true;
  filter["storage"][0]["total_bytes"] = true;
  filter["storage"][0]["available_bytes"] = true;
  filter["network"][0]["interface"] = true;
  filter["network"][0]["rx_bytes_per_s"] = true;
  filter["network"][0]["tx_bytes_per_s"] = true;

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_NAME, WIFI_PASSWORD);
  metricsQueue = xQueueCreate(1, sizeof(FetchResult));
  if (!metricsQueue || xTaskCreate(fetchMetrics, "metrics", 8192, nullptr, 1, nullptr) != pdPASS) {
    status("LOW MEMORY", AMBER);
  }
}

void loop() {
  lv_timer_handler();
  receiveMetrics();
  updateClock();
  updateBrightness();
  delay(15);
}
