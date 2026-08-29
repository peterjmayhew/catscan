// ESP32-CAM Tabby Cat Detector - firmware
//
// Board: Freenove ESP32-CAM Dev Board Kit (AI-Thinker pinout, OV2640 camera).
// On PIR motion, grabs a JPEG frame and POSTs the raw bytes to a Flask
// server, which does the actual cat / tabby-vs-not classification.
//
// Requires the "config.h" file (copy config.example.h -> config.h and fill
// in your Wi-Fi + server details) and the ESP32 Arduino core installed.

#include <ArduinoOTA.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <time.h>
#include "esp_camera.h"
#include "config.h"

// ---- AI-Thinker / Freenove ESP32-CAM pin map ----
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

// GPIO 13 is free on this pinout (not used by the camera), so it's used for
// the PIR sensor's digital output. GPIO 4 drives the onboard flash LED.
// GPIO 14/15 drive the optional HC-SR04 ultrasonic sensor, and GPIO 33
// (an ADC-capable pin) reads the optional LDR light sensor. See
// config.example.h for wiring notes and how to disable the ultrasonic
// sensor if you haven't wired one up.
#define PIR_PIN    13
#define FLASH_PIN   4

unsigned long lastCaptureMillis = 0;

// Wi-Fi reconnect state - non-blocking with exponential backoff, so a
// dropped connection doesn't freeze sensor polling the way a blocking
// reconnect loop would.
bool wifiWasConnected = false;
unsigned long lastWifiAttemptMillis = 0;
unsigned long wifiRetryDelayMs = 1000;
const unsigned long WIFI_MAX_RETRY_DELAY_MS = 30000;

// A sanity floor for treating the device clock as NTP-synced (well after
// this project could possibly have been built) rather than its un-synced
// default of "seconds since boot from 1970".
const time_t NTP_SANITY_EPOCH = 1700000000; // 2023-11-14

bool initCamera() {
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;

  if (psramFound()) {
    config.frame_size = FRAMESIZE_SVGA; // 800x600, plenty for classification
    config.jpeg_quality = 12;
    config.fb_count = 2;
  } else {
    config.frame_size = FRAMESIZE_VGA;
    config.jpeg_quality = 15;
    config.fb_count = 1;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("Camera init failed with error 0x%x\n", err);
    return false;
  }

  // Push the sensor towards its low-light-friendly settings. This doesn't
  // hurt daytime shots (auto-exposure/auto-gain still adapt), but gives
  // night captures (lit by the flash) noticeably less noise and better
  // exposure.
  sensor_t *sensor = esp_camera_sensor_get();
  if (sensor != nullptr) {
    sensor->set_gainceiling(sensor, GAINCEILING_128X);
    sensor->set_aec2(sensor, 1);
    sensor->set_bpc(sensor, 1);
    sensor->set_wpc(sensor, 1);
  }

  return true;
}

// Reads the HC-SR04 and returns the distance in cm, or -1 if no echo was
// received (e.g. sensor not connected, or nothing in range).
long readUltrasonicDistanceCm() {
  digitalWrite(ULTRASONIC_TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(ULTRASONIC_TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(ULTRASONIC_TRIG_PIN, LOW);

  // Timeout after ~30ms (~5m round trip) so a disconnected/faulty sensor
  // can't stall the main loop.
  unsigned long durationUs = pulseIn(ULTRASONIC_ECHO_PIN, HIGH, 30000UL);
  if (durationUs == 0) {
    return -1;
  }
  return durationUs / 58; // standard HC-SR04 microseconds-to-cm conversion
}

bool isDark() {
  return analogRead(LDR_PIN) < DARK_ADC_THRESHOLD;
}

// Runs once each time Wi-Fi (re)connects: starts NTP sync and (re)arms OTA,
// since both can need re-establishing after a connection drop.
void onWiFiConnected() {
  Serial.printf("Wi-Fi connected, IP: %s\n", WiFi.localIP().toString().c_str());
  configTime(NTP_GMT_OFFSET_SEC, NTP_DST_OFFSET_SEC, NTP_SERVER);
  ArduinoOTA.begin();
}

// Non-blocking Wi-Fi maintenance: call every loop() iteration. Retries with
// exponential backoff instead of blocking the whole device (and therefore
// sensor polling) while waiting to reconnect.
void maintainWiFi() {
  bool connectedNow = WiFi.status() == WL_CONNECTED;

  if (connectedNow && !wifiWasConnected) {
    onWiFiConnected();
    wifiRetryDelayMs = 1000;
  }
  wifiWasConnected = connectedNow;

  if (connectedNow) {
    return;
  }

  unsigned long now = millis();
  if (now - lastWifiAttemptMillis < wifiRetryDelayMs) {
    return;
  }
  lastWifiAttemptMillis = now;
  Serial.printf("Wi-Fi not connected - retrying (next attempt in %lums if this fails)...\n",
                wifiRetryDelayMs);
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  wifiRetryDelayMs = min(wifiRetryDelayMs * 2, WIFI_MAX_RETRY_DELAY_MS);
}

// Cheap insurance against memory fragmentation on a device meant to run
// unattended for months: restart once a day at a quiet hour. No-ops until
// NTP has actually synced at least once.
void maybeRestartDaily() {
  struct tm timeinfo;
  if (!getLocalTime(&timeinfo, 0)) {
    return;
  }
  if (timeinfo.tm_hour == DAILY_RESTART_HOUR && timeinfo.tm_min == 0) {
    Serial.println("Scheduled daily restart.");
    delay(200); // let the serial message flush
    ESP.restart();
  }
}

// Sends one JPEG frame to the server, retrying once on failure (a single
// dropped packet on a Wi-Fi hiccup shouldn't cost a whole frame out of an
// already-short burst). Returns true if either attempt succeeded.
bool sendFrameToServer(camera_fb_t *fb) {
  for (int attempt = 1; attempt <= 2; attempt++) {
    HTTPClient http;
    http.begin(SERVER_URL);
    http.addHeader("Content-Type", "image/jpeg");
    if (strlen(DEVICE_API_KEY) > 0) {
      http.addHeader("X-Device-Key", DEVICE_API_KEY);
    }
    time_t captureTime = time(nullptr);
    if (captureTime > NTP_SANITY_EPOCH) {
      http.addHeader("X-Capture-Time", String((unsigned long)captureTime));
    }

    int httpCode = http.POST(fb->buf, fb->len);

    if (httpCode > 0) {
      String response = http.getString();
      Serial.printf("Server responded [%d]: %s\n", httpCode, response.c_str());
      http.end();
      return true;
    }

    Serial.printf("POST failed (attempt %d/2): %s\n", attempt, http.errorToString(httpCode).c_str());
    http.end();
    if (attempt == 1) {
      delay(500);
    }
  }
  return false;
}

// Captures BURST_FRAME_COUNT frames instead of one. A single frame is an
// unreliable basis for telling cats apart (motion blur, a bad angle, an
// ear in the way); the server combines the burst into one majority-vote
// result instead of trusting any single frame.
void captureAndSend() {
  bool dark = isDark();

  for (int i = 0; i < BURST_FRAME_COUNT; i++) {
    digitalWrite(FLASH_PIN, HIGH); // fill light helps the classifier see
    delay(dark ? FLASH_WARMUP_DARK_MS : FLASH_WARMUP_MS);

    camera_fb_t *fb = esp_camera_fb_get();
    digitalWrite(FLASH_PIN, LOW);

    if (!fb) {
      Serial.println("Camera capture failed");
      continue;
    }

    sendFrameToServer(fb);
    esp_camera_fb_return(fb);

    if (i < BURST_FRAME_COUNT - 1) {
      delay(BURST_FRAME_DELAY_MS);
    }
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(PIR_PIN, INPUT);
  pinMode(FLASH_PIN, OUTPUT);
  digitalWrite(FLASH_PIN, LOW);

  if (USE_ULTRASONIC) {
    pinMode(ULTRASONIC_TRIG_PIN, OUTPUT);
    pinMode(ULTRASONIC_ECHO_PIN, INPUT);
  }

  if (!initCamera()) {
    Serial.println("Halting: camera init failed.");
    while (true) {
      delay(1000);
    }
  }

  ArduinoOTA.setHostname("catscan-esp32cam");
  ArduinoOTA.setPassword(OTA_PASSWORD);

  // Give the initial connection a bounded window at boot; if it doesn't
  // succeed in time, maintainWiFi() takes over with backoff retries in the
  // background rather than freezing the whole device waiting.
  Serial.printf("Connecting to Wi-Fi \"%s\"...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long connectStart = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - connectStart < 15000) {
    delay(300);
    Serial.print(".");
  }
  Serial.println();

  Serial.println("Ready - watching PIR" +
                  String(USE_ULTRASONIC ? " + ultrasonic sensor" : " sensor") +
                  " for motion.");
}

void loop() {
  maintainWiFi();
  if (WiFi.status() == WL_CONNECTED) {
    ArduinoOTA.handle();
  }
  maybeRestartDaily();

  bool pirTriggered = digitalRead(PIR_PIN) == HIGH;

  bool ultrasonicTriggered = false;
  if (USE_ULTRASONIC) {
    long distanceCm = readUltrasonicDistanceCm();
    ultrasonicTriggered = distanceCm > 0 && distanceCm <= ULTRASONIC_MAX_DISTANCE_CM;
  }

  bool motionDetected = pirTriggered || ultrasonicTriggered;
  unsigned long now = millis();
  bool cooldownElapsed =
      (now - lastCaptureMillis) >= (unsigned long)CAPTURE_COOLDOWN_SECONDS * 1000UL;

  if (motionDetected && cooldownElapsed) {
    Serial.printf("Motion detected (PIR=%d, ultrasonic=%d, dark=%d) - capturing frame.\n",
                  pirTriggered, ultrasonicTriggered, isDark());
    captureAndSend();
    lastCaptureMillis = now;
  }

  delay(200);
}
