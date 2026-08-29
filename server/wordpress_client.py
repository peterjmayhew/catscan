"""Uploads detections to the companion WordPress plugin (see
wordpress-plugin/catscan-detections/), so every visit - image, label,
confidence, timestamp, and detection metadata - shows up in your WordPress
admin without you needing to check the server directly.

Configure with:
    WORDPRESS_URL       e.g. https://yoursite.example.com
    WORDPRESS_API_KEY   the key shown on Settings -> CatScan in WP admin

Leave WORDPRESS_URL unset to skip this entirely - it's optional.
"""

import logging
import os
import time

import cv2
import requests

logger = logging.getLogger(__name__)

WORDPRESS_URL = os.environ.get("WORDPRESS_URL", "").rstrip("/")
WORDPRESS_API_KEY = os.environ.get("WORDPRESS_API_KEY", "")
REQUEST_TIMEOUT_SECONDS = 15


def is_configured():
    return bool(WORDPRESS_URL and WORDPRESS_API_KEY)


def upload_detection(result, image_bgr, image_path):
    """Best-effort upload; logs and swallows failures so a WordPress outage
    never breaks the camera pipeline itself."""
    if not is_configured():
        return None

    success, buffer = cv2.imencode(".jpg", image_bgr)
    if not success:
        logger.warning("Failed to encode image, skipping WordPress upload")
        return None

    capture_timestamp = result.get("capture_timestamp")
    captured_at = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(capture_timestamp))
        if capture_timestamp
        else os.path.basename(image_path)  # fall back to receipt-time filename
    )

    fields = {
        "label": result.get("label", "no_cat"),
        "cat_detected": str(bool(result.get("cat_detected", False))).lower(),
        "confidence": str(result.get("confidence", 0)),
        "mode": result.get("mode", "unknown"),
        "low_light": str(bool(result.get("low_light", False))).lower(),
        "frame_count": str(result.get("frame_count", 1)),
        "reasoning": result.get("reasoning", ""),
        "captured_at": captured_at,
    }
    image_bytes = buffer.tobytes()
    headers = {"X-API-Key": WORDPRESS_API_KEY}

    for attempt in range(2):
        try:
            response = requests.post(
                f"{WORDPRESS_URL}/wp-json/catscan/v1/detections",
                data=fields,
                files={"image": ("capture.jpg", image_bytes, "image/jpeg")},
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and 400 <= status < 500 and status != 429:
                # A bad request/auth/rate-limit-exempt client error won't be
                # fixed by retrying - fail fast instead of wasting a retry.
                logger.warning("WordPress upload rejected (%s), not retrying: %s", status, exc)
                return None
            logger.warning("WordPress upload failed (attempt %d/2): %s", attempt + 1, exc)
        except requests.RequestException as exc:
            logger.warning("WordPress upload failed (attempt %d/2): %s", attempt + 1, exc)

        if attempt == 0:
            time.sleep(2)

    return None


def upload_heartbeat(status):
    """Pushes a device status snapshot (uptime, Wi-Fi signal, etc. - see
    remote_control.py) to WordPress's Settings -> CatScan -> Device page.
    Best-effort; failures are logged and swallowed, same as detections."""
    if not is_configured():
        return
    try:
        response = requests.post(
            f"{WORDPRESS_URL}/wp-json/catscan/v1/heartbeat",
            json=status,
            headers={"X-API-Key": WORDPRESS_API_KEY},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Heartbeat upload failed: %s", exc)


def fetch_pending_command():
    """Returns a command string queued from the Device admin page, or None
    if there isn't one or the request failed. WordPress clears the command
    as soon as it's fetched, so this call also acts as the dequeue."""
    if not is_configured():
        return None
    try:
        response = requests.get(
            f"{WORDPRESS_URL}/wp-json/catscan/v1/pending-command",
            headers={"X-API-Key": WORDPRESS_API_KEY},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json().get("command") or None
    except requests.RequestException as exc:
        logger.warning("Could not fetch pending command: %s", exc)
        return None
