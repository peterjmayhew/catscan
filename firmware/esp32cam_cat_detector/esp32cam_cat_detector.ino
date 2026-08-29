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
  return true;
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

void captureAndSend() {
  digitalWrite(FLASH_PIN, HIGH); // brief fill light helps the classifier
  delay(50);

  camera_fb_t *fb = esp_camera_fb_get();
  digitalWrite(FLASH_PIN, LOW);

  if (!fb) {
    Serial.println("Camera capture failed");
    return;
  }

  sendFrameToServer(fb);
  esp_camera_fb_return(fb);
}

void setup() {
  Serial.begin(115200);
  pinMode(PIR_PIN, INPUT);
  pinMode(FLASH_PIN, OUTPUT);
  digitalWrite(FLASH_PIN, LOW);

  if (!initCamera()) {
    Serial.println("Halting: camera init failed.");
    while (true) {
      delay(1000);
    }
  }

  connectWiFi();
  Serial.println("Ready - watching PIR sensor for motion.");
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  bool motionDetected = digitalRead(PIR_PIN) == HIGH;
  unsigned long now = millis();
  bool cooldownElapsed =
      (now - lastCaptureMillis) >= (unsigned long)CAPTURE_COOLDOWN_SECONDS * 1000UL;

  if (motionDetected && cooldownElapsed) {
    Serial.println("Motion detected - capturing frame.");
    captureAndSend();
    lastCaptureMillis = now;
  }

  delay(200);
}
