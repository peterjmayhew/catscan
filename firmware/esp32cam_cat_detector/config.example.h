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

#endif
