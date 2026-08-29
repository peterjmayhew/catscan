// ESP32-CAM Tabby Cat Detector - firmware
//
// Board: Freenove ESP32-CAM Dev Board Kit (AI-Thinker pinout, OV2640 camera).
// On PIR motion, grabs a JPEG frame and POSTs the raw bytes to a Flask
// server, which does the actual cat / tabby-vs-not classification.
//
// Requires the "config.h" file (copy config.example.h -> config.h and fill
// in your Wi-Fi + server details) and the ESP32 Arduino core installed.

#include <WiFi.h>
#include <HTTPClient.h>
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

void connectWiFi() {
  Serial.printf("Connecting to Wi-Fi \"%s\"...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.printf("\nConnected, IP: %s\n", WiFi.localIP().toString().c_str());
}

// Sends one JPEG frame to the server and returns true if the request
// succeeded (regardless of what the server decided the frame contained).
bool sendFrameToServer(camera_fb_t *fb) {
  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "image/jpeg");
  int httpCode = http.POST(fb->buf, fb->len);

  if (httpCode > 0) {
    String response = http.getString();
    Serial.printf("Server responded [%d]: %s\n", httpCode, response.c_str());
  } else {
    Serial.printf("POST failed: %s\n", http.errorToString(httpCode).c_str());
  }

  http.end();
  return httpCode > 0;
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

  connectWiFi();
  Serial.println("Ready - watching PIR" +
                  String(USE_ULTRASONIC ? " + ultrasonic sensor" : " sensor") +
                  " for motion.");
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

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
