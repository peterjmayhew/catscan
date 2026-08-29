// ESP32-CAM Tabby Cat Detector - firmware
//
// Board: Freenove ESP32-CAM Dev Board Kit (AI-Thinker pinout, OV2640 camera).
// On PIR motion, grabs a JPEG frame and POSTs the raw bytes to a Flask
// server, which does the actual cat / tabby-vs-not classification.
//
// Requires the "config.h" file (copy config.example.h -> config.h and fill
// in your Wi-Fi + server details) and the ESP32 Arduino core installed.

#include <ArduinoOTA.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <time.h>
#include "esp_camera.h"
#include "soc/rtc_cntl_reg.h"
#include "soc/soc.h"
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

// Local control server: lets the Flask server (as a bridge to the
// WordPress plugin) check status and send commands over the LAN.
WebServer controlServer(80);
Preferences preferences;
bool deterrentAutoEnabled = true;
unsigned long deterrentOffAtMillis = 0;   // 0 = not currently active
unsigned long lastDeterrentFireMillis = 0; // 0 = never fired this boot
volatile bool manualCaptureRequested = false;

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

// Fires the deterrent output, respecting a minimum cooldown between
// activations. force=true (used by the manual "deter_test" command)
// bypasses the deterrentAutoEnabled toggle, so wiring can be verified even
// while auto-fire is switched off.
void fireDeterrent(bool force) {
  if (!force && !deterrentAutoEnabled) {
    Serial.println("Deterrent auto-fire is disabled - ignoring.");
    return;
  }
  unsigned long now = millis();
  if (lastDeterrentFireMillis != 0 && now - lastDeterrentFireMillis < DETERRENT_COOLDOWN_MS) {
    Serial.println("Deterrent still on cooldown - ignoring.");
    return;
  }
  Serial.println("Firing deterrent.");
  digitalWrite(DETERRENT_PIN, HIGH);
  deterrentOffAtMillis = now + DETERRENT_ACTIVE_MS;
  lastDeterrentFireMillis = now;
}

// Non-blocking auto-off - call every loop() iteration.
void maintainDeterrent() {
  if (deterrentOffAtMillis != 0 && millis() >= deterrentOffAtMillis) {
    digitalWrite(DETERRENT_PIN, LOW);
    deterrentOffAtMillis = 0;
  }
}

// Returns false (and sends a 401) if CONTROL_API_KEY is set but the
// request didn't supply a matching X-Control-Key header. Auth is skipped
// entirely if CONTROL_API_KEY is left blank.
bool checkControlAuth() {
  if (strlen(CONTROL_API_KEY) == 0) {
    return true;
  }
  if (controlServer.header("X-Control-Key") != CONTROL_API_KEY) {
    controlServer.send(401, "text/plain", "unauthorized");
    return false;
  }
  return true;
}

void handleStatus() {
  if (!checkControlAuth()) {
    return;
  }
  String json = "{";
  json += "\"uptime_s\":" + String(millis() / 1000UL) + ",";
  json += "\"free_heap\":" + String(ESP.getFreeHeap()) + ",";
  json += "\"wifi_rssi\":" + String(WiFi.RSSI()) + ",";
  json += "\"dark\":" + String(isDark() ? "true" : "false") + ",";
  json += "\"deterrent_enabled\":" + String(deterrentAutoEnabled ? "true" : "false") + ",";
  // Built as an explicit `long` rather than inline in the ternary: mixing
  // unsigned long (the division result) and int (-1) in one ternary
  // expression forces -1 to convert to an unsigned long (4294967295)
  // instead of staying -1, silently corrupting the "no capture yet" case.
  long secondsSinceLastCapture =
      lastCaptureMillis > 0 ? (long)((millis() - lastCaptureMillis) / 1000UL) : -1;
  json += "\"seconds_since_last_capture\":" + String(secondsSinceLastCapture);
  json += "}";
  controlServer.send(200, "application/json", json);
}

// Commands (sent as a form field, e.g. POST /command with body
// "command=reboot"): reboot, capture, deter, deter_test, deterrent_on,
// deterrent_off. See README "Remote control" for what queues these from
// WordPress.
void handleCommand() {
  if (!checkControlAuth()) {
    return;
  }

  String command = controlServer.arg("command");
  Serial.printf("Received control command: %s\n", command.c_str());

  if (command == "reboot") {
    controlServer.send(200, "text/plain", "rebooting");
    delay(300); // let the response actually get sent before we restart
    ESP.restart();
  } else if (command == "capture") {
    manualCaptureRequested = true;
    controlServer.send(200, "text/plain", "capture queued");
  } else if (command == "deter") {
    // The automatic path from the server on a real "other_cat" detection -
    // respects the deterrentAutoEnabled toggle.
    fireDeterrent(false);
    controlServer.send(200, "text/plain", "ok");
  } else if (command == "deter_test") {
    // Manual test from the WordPress Device page - always fires, so
    // wiring can be verified even with auto-fire switched off.
    fireDeterrent(true);
    controlServer.send(200, "text/plain", "deterrent fired");
  } else if (command == "deterrent_on" || command == "deterrent_off") {
    deterrentAutoEnabled = (command == "deterrent_on");
    preferences.putBool("deter_on", deterrentAutoEnabled);
    controlServer.send(200, "text/plain", deterrentAutoEnabled ? "enabled" : "disabled");
  } else {
    controlServer.send(400, "text/plain", "unknown command");
  }
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
    http.setTimeout(8000); // don't let a stalled connection hang a whole burst
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
  // ESP32-CAM boards are prone to spurious brownout resets under the
  // current spikes Wi-Fi + flash LED use cause, especially on marginal
  // USB power - a very common real-world reliability fix.
  WRITE_PERI_REG(RTC_CNTL_BROWNOUT_DET_ENA_REG, 0);

  Serial.begin(115200);
  pinMode(PIR_PIN, INPUT);
  pinMode(FLASH_PIN, OUTPUT);
  digitalWrite(FLASH_PIN, LOW);
  pinMode(DETERRENT_PIN, OUTPUT);
  digitalWrite(DETERRENT_PIN, LOW);

  if (USE_ULTRASONIC) {
    pinMode(ULTRASONIC_TRIG_PIN, OUTPUT);
    pinMode(ULTRASONIC_ECHO_PIN, INPUT);
  }

  preferences.begin("catscan", false);
  deterrentAutoEnabled = preferences.getBool("deter_on", DETERRENT_ENABLED_DEFAULT);

  // Camera init failures are usually a transient power glitch at boot;
  // retry a few times before giving up and restarting the whole device,
  // rather than hanging dead until someone physically power-cycles it.
  int cameraInitAttempts = 0;
  while (!initCamera()) {
    cameraInitAttempts++;
    Serial.printf("Camera init failed (attempt %d).\n", cameraInitAttempts);
    if (cameraInitAttempts >= 5) {
      Serial.println("Giving up after 5 attempts - restarting.");
      delay(500);
      ESP.restart();
    }
    esp_camera_deinit(); // clean up before retrying, in case of partial init
    delay(2000);
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

  const char *controlHeaderKeys[] = {"X-Control-Key"};
  controlServer.collectHeaders(controlHeaderKeys, 1);
  controlServer.on("/status", HTTP_GET, handleStatus);
  controlServer.on("/command", HTTP_POST, handleCommand);
  controlServer.begin();

  Serial.println("Ready - watching PIR" +
                  String(USE_ULTRASONIC ? " + ultrasonic sensor" : " sensor") +
                  " for motion.");
}

void loop() {
  maintainWiFi();
  if (WiFi.status() == WL_CONNECTED) {
    ArduinoOTA.handle();
  }
  controlServer.handleClient();
  maintainDeterrent();
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

  if ((motionDetected && cooldownElapsed) || manualCaptureRequested) {
    Serial.printf("Capturing (PIR=%d, ultrasonic=%d, manual=%d, dark=%d).\n",
                  pirTriggered, ultrasonicTriggered, manualCaptureRequested, isDark());
    captureAndSend();
    lastCaptureMillis = now;
    manualCaptureRequested = false;
  }

  delay(200);
}
