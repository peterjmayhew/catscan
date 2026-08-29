"""Bridges the local server to two things it otherwise can't reach:

- Your ESP32-CAM, over the LAN, for on-demand status and commands (reboot,
  trigger a capture, fire the deterrent). See ESP32_CONTROL_URL below.
- Your WordPress site, which - being on the public internet - can't reach
  into your home LAN itself. A background loop here periodically pushes a
  status heartbeat to WordPress and polls it for a command an admin queued
  from Settings -> CatScan -> Device, forwarding it to the ESP32.

This is polling-based, not truly live: HEARTBEAT_INTERVAL_SECONDS controls
how fresh WordPress's view of the device is and how quickly a queued
command actually reaches it. Deterrent activation on an "other_cat"
detection deliberately does NOT go through this path - see
trigger_deterrent() below - since that needs a same-LAN response time, not
a round trip through a hosted website.

Leave ESP32_CONTROL_URL and/or WORDPRESS_URL unset to skip whichever half
of this you don't want; the rest of the project works the same without it.
"""

import logging
import os
import threading
import time

import requests

import wordpress_client

logger = logging.getLogger(__name__)

ESP32_CONTROL_URL = os.environ.get("ESP32_CONTROL_URL", "").rstrip("/")
ESP32_CONTROL_KEY = os.environ.get("ESP32_CONTROL_KEY", "")
HEARTBEAT_INTERVAL_SECONDS = int(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", 20))
REQUEST_TIMEOUT_SECONDS = 8


def esp32_configured():
    return bool(ESP32_CONTROL_URL)


def _esp32_headers():
    return {"X-Control-Key": ESP32_CONTROL_KEY} if ESP32_CONTROL_KEY else {}


def fetch_esp32_status():
    """Returns the ESP32's /status JSON, or None if unreachable/unconfigured."""
    if not esp32_configured():
        return None
    try:
        response = requests.get(
            f"{ESP32_CONTROL_URL}/status", headers=_esp32_headers(), timeout=REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Could not reach ESP32 for status: %s", exc)
        return None


def send_esp32_command(command):
    """Best-effort command send. Returns True only if the ESP32 acked it -
    callers shouldn't assume a command was applied just because this
    returned without raising."""
    if not esp32_configured():
        logger.warning("ESP32_CONTROL_URL not set - cannot send command %r", command)
        return False
    try:
        response = requests.post(
            f"{ESP32_CONTROL_URL}/command",
            data={"command": command},
            headers=_esp32_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.warning("Failed to send ESP32 command %r: %s", command, exc)
        return False


def trigger_deterrent():
    """Called immediately when a burst resolves to 'other_cat' - a direct
    LAN call to the ESP32, bypassing WordPress entirely. A cat approaching
    a cat flap needs a response in well under a second; a round trip
    through a hosted website cannot deliver that."""
    if send_esp32_command("deter"):
        logger.info("Deterrent triggered")
    else:
        logger.warning("Deterrent trigger failed (see warning above) - check ESP32_CONTROL_URL/wiring")


def _remote_control_tick():
    if wordpress_client.is_configured():
        wordpress_client.upload_heartbeat(fetch_esp32_status() or {"reachable": False})

        command = wordpress_client.fetch_pending_command()
        if command:
            logger.info("Forwarding queued command from WordPress: %s", command)
            send_esp32_command(command)


def _remote_control_loop():
    while True:
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)
        try:
            _remote_control_tick()
        except Exception:
            logger.exception("Remote control loop iteration failed")


def start():
    """No-ops if neither ESP32 control nor WordPress is configured - no
    point running a background loop that can't do anything."""
    if not esp32_configured() and not wordpress_client.is_configured():
        return
    threading.Thread(target=_remote_control_loop, daemon=True).start()
