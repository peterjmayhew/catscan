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

    fields = {
        "label": result.get("label", "no_cat"),
        "cat_detected": str(bool(result.get("cat_detected", False))).lower(),
        "confidence": str(result.get("confidence", 0)),
        "mode": result.get("mode", "unknown"),
        "low_light": str(bool(result.get("low_light", False))).lower(),
        "frame_count": str(result.get("frame_count", 1)),
        "reasoning": result.get("reasoning", ""),
        "captured_at": os.path.basename(image_path),
    }
    files = {"image": ("capture.jpg", buffer.tobytes(), "image/jpeg")}
    headers = {"X-API-Key": WORDPRESS_API_KEY}

    try:
        response = requests.post(
            f"{WORDPRESS_URL}/wp-json/catscan/v1/detections",
            data=fields,
            files=files,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        logger.warning("WordPress upload failed: %s", exc)
        return None
