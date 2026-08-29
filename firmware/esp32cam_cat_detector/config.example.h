// Copy this file to config.h and fill in your own details.
// config.h is git-ignored so your Wi-Fi credentials never get committed.

#ifndef CAT_DETECTOR_CONFIG_H
#define CAT_DETECTOR_CONFIG_H

#define WIFI_SSID     "your-wifi-name"
#define WIFI_PASSWORD "your-wifi-password"

// URL of the Flask server's /detect endpoint, e.g. http://192.168.1.50:5000/detect
#define SERVER_URL "http://192.168.1.50:5000/detect"

// Minimum seconds between two captures, so one visit doesn't spam the server.
#define CAPTURE_COOLDOWN_SECONDS 15

// Ultrasonic sensor (HC-SR04) - a second, independent trigger alongside the
// PIR. Unlike PIR (body heat) and the camera (needs light), an ultrasonic
// sensor works identically day or night, so it's a good way to reliably
// catch a cat passing through a doorway/path even in full darkness. Set
// USE_ULTRASONIC to false if you haven't wired one up - the PIR alone still
// works fine.
//
// IMPORTANT: the HC-SR04's ECHO pin outputs 5V, but ESP32 GPIOs are only
// 3.3V-tolerant. Put a voltage divider on ECHO (e.g. a 1k resistor in
// series, then a 2k resistor from that junction to GND) before wiring it
// to the ESP32 - skipping this can damage the GPIO.
#define USE_ULTRASONIC true
#define ULTRASONIC_TRIG_PIN 14
#define ULTRASONIC_ECHO_PIN 15
#define ULTRASONIC_MAX_DISTANCE_CM 100

// Double-check these pins are actually free on your specific board revision
// - ESP32-CAM clones vary slightly, and some of these are shared with the
// microSD slot if you use it. GPIO12/GPIO2 are boot-strapping pins; avoid
// routing ECHO through them if you see unreliable boot behaviour.

// Ambient light sensor (LDR + fixed resistor as a voltage divider into an
// analog pin, wired so more light = higher voltage). Used to decide when a
// capture needs a longer flash pulse. Lower ADC reading = darker; tune
// DARK_ADC_THRESHOLD against your own LDR/resistor values and enclosure.
#define LDR_PIN 33
#define DARK_ADC_THRESHOLD 800

// How long to hold the flash LED on before capturing, in milliseconds. A
// longer pulse at night gives the sensor's auto-exposure more light to
// work with; too short and dark frames come out underexposed and noisy.
#define FLASH_WARMUP_MS 50
#define FLASH_WARMUP_DARK_MS 200

#endif
