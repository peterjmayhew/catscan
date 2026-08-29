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

// A single frame is an unreliable basis for "is this my cat or the
// neighbour's" - a bad angle or motion blur on one frame can flip the
// verdict. Each trigger instead captures a short burst of frames; the
// server combines them into one majority-vote result (see BURST_SIZE /
// BURST_WINDOW_SECONDS in server/app.py, which should match these).
#define BURST_FRAME_COUNT 3
#define BURST_FRAME_DELAY_MS 400

// Shared secret sent as the X-Device-Key header on every POST to /detect.
// Optional (leave blank to skip): the server only checks it if its own
// DEVICE_API_KEY environment variable is set. This is defense in depth
// in case the server port is ever reachable beyond your LAN, not a
// replacement for keeping the server itself off the public internet.
#define DEVICE_API_KEY ""

// Password for OTA updates (Sketch -> Upload Using... in Arduino IDE, once
// the device shows up on your network). Change this from the default.
#define OTA_PASSWORD "change-me"

// The device restarts once a day at this hour (0-23, in whatever timezone
// NTP_GMT_OFFSET_SEC/NTP_DST_OFFSET_SEC below describe) as cheap insurance
// against memory fragmentation on a device meant to run unattended for
// months. Requires NTP to have synced at least once; if it never syncs
// (e.g. no internet), the device just never restarts on this schedule.
#define DAILY_RESTART_HOUR 4
#define NTP_SERVER "pool.ntp.org"
#define NTP_GMT_OFFSET_SEC 0       // e.g. 3600 for UTC+1
#define NTP_DST_OFFSET_SEC 0       // e.g. 3600 if your zone observes DST

// Local control server (port 80) for remote monitoring/control - the
// Flask server polls /status and forwards commands from the WordPress
// plugin's Device page to /command. Leave blank to disable auth on this
// local server (fine if it's genuinely LAN-only and you trust your
// network; set a key otherwise, matched by ESP32_CONTROL_KEY on the
// server).
#define CONTROL_API_KEY ""

// Deterrent output - a buzzer, ultrasonic repeller module, or relay,
// whatever you've wired up near the cat flap. Fired automatically when
// the server confirms an "other_cat" detection (see README "Deterring the
// neighbour's cat"). GPIO12 is a boot-strapping pin (MTDI) - wire your
// driver circuit so it idles LOW (e.g. via a pull-down resistor), or boot
// can become unreliable. Verify against your specific board if in doubt,
// same caveat as the ultrasonic sensor's pins above.
#define DETERRENT_PIN 12
#define DETERRENT_ACTIVE_MS 3000        // how long the deterrent stays on
#define DETERRENT_COOLDOWN_MS 10000     // minimum gap between activations
#define DETERRENT_ENABLED_DEFAULT true  // used only on first-ever boot; the
                                         // on/off toggle is remembered in
                                         // flash after that (survives reboots)

#endif
