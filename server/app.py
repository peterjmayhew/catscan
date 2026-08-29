"""Flask server for the ESP32-CAM cat detector.

Receives raw JPEG frames from the ESP32-CAM on motion (a short burst per
trigger - see BURST_SIZE below), classifies each one, saves it under
captures/<label>/ (both for your review and as future training data), and
combines the burst into one majority-vote result which gets forwarded to
WordPress and/or a generic notification webhook.
"""

import hmac
import logging
import os
import threading
import time
from collections import Counter

import cv2
import numpy as np
import requests
from flask import Flask, jsonify, request

import remote_control
import wordpress_client
from detector import CatDetector

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

app = Flask(__name__)
# A malicious or buggy client sending an oversized body shouldn't be able to
# exhaust server memory - 15MB is generous for a single VGA/SVGA JPEG.
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024
detector = CatDetector()

CAPTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "captures")
NOTIFY_WEBHOOK_URL = os.environ.get("NOTIFY_WEBHOOK_URL")

# Optional shared secret the firmware sends as X-Device-Key (see
# config.example.h). Unset by default - only enforced if you opt in, so
# existing setups keep working unchanged.
DEVICE_API_KEY = os.environ.get("DEVICE_API_KEY")

# Every capture is saved to disk (see save_capture) and nothing ever
# deleted them until now - left running for months, that grows without
# bound. 0 disables cleanup and keeps everything forever.
CAPTURES_RETENTION_DAYS = int(os.environ.get("CAPTURES_RETENTION_DAYS", 30))
CAPTURES_CLEANUP_INTERVAL_SECONDS = 6 * 3600

# A single frame is a noisy basis for "my cat or the neighbour's" - one bad
# angle or motion-blurred frame can flip the verdict. The firmware sends a
# short burst per trigger (BURST_FRAME_COUNT in config.h); these two should
# roughly match so a full burst closes the window itself rather than always
# waiting out the timeout.
BURST_SIZE = int(os.environ.get("BURST_SIZE", 3))
BURST_WINDOW_SECONDS = float(os.environ.get("BURST_WINDOW_SECONDS", 10))

_burst_lock = threading.Lock()
_burst_frames = []  # list of (timestamp, result, image_path)


def save_capture(image_bgr, label, capture_timestamp=None):
    """capture_timestamp, if provided (from the firmware's NTP-synced
    clock via the X-Capture-Time header), makes the filename reflect when
    the photo was actually taken rather than when the server received it -
    useful when a Wi-Fi retry delays upload. The millisecond disambiguator
    always comes from receipt time, since capture_timestamp only has
    second-level resolution and burst frames can share a whole second."""
    label_dir = os.path.join(CAPTURES_DIR, label)
    os.makedirs(label_dir, exist_ok=True)
    display_ts = capture_timestamp if capture_timestamp is not None else time.time()
    filename = (
        f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(display_ts))}"
        f"-{int(time.time() * 1000) % 1000:03d}.jpg"
    )
    path = os.path.join(label_dir, filename)
    cv2.imwrite(path, image_bgr)
    return path


def _aggregate_burst(frames):
    labels = [result["label"] for _, result, _ in frames]
    top_label, _ = Counter(labels).most_common(1)[0]
    agreeing = [f for f in frames if f[1]["label"] == top_label]
    best_timestamp, best_result, best_image_path = max(
        agreeing, key=lambda f: f[1]["confidence"]
    )
    avg_confidence = sum(f[1]["confidence"] for f in agreeing) / len(agreeing)

    return {
        "cat_detected": top_label != "no_cat",
        "label": top_label,
        "confidence": round(avg_confidence, 3),
        "mode": best_result["mode"],
        "low_light": any(f[1].get("low_light") for f in frames),
        "frame_count": len(frames),
        "agreeing_frame_count": len(agreeing),
        "reasoning": best_result.get("reasoning", ""),
        "capture_timestamp": best_result.get("capture_timestamp"),
    }, best_image_path


def _flush_burst_locked(force=False):
    """Must be called with _burst_lock held. Returns (result, image_path)
    if a burst was closed out, else None."""
    global _burst_frames
    if not _burst_frames:
        return None

    elapsed = time.time() - _burst_frames[0][0]
    if not force and len(_burst_frames) < BURST_SIZE and elapsed < BURST_WINDOW_SECONDS:
        return None

    frames, _burst_frames = _burst_frames, []
    return _aggregate_burst(frames)


def _burst_watchdog():
    # Closes out a burst that never reached BURST_SIZE (e.g. the cat left
    # after one or two frames) once it's been sitting for BURST_WINDOW_SECONDS.
    while True:
        time.sleep(2)
        with _burst_lock:
            closed = _flush_burst_locked()
        if closed:
            _handle_burst_result(*closed)


def _handle_burst_result(result, image_path):
    app.logger.info("Burst result: %s (representative image %s)", result, image_path)

    image_bgr = cv2.imread(image_path)
    if image_bgr is not None:
        wordpress_client.upload_detection(result, image_bgr, image_path)

    if result["label"] == "other_cat":
        # Deterrent first, and directly to the ESP32 (not via WordPress) -
        # this needs to happen in well under a second, not after a round
        # trip through a hosted website.
        remote_control.trigger_deterrent()
        notify(result, image_path)


def _cleanup_old_captures():
    if CAPTURES_RETENTION_DAYS <= 0 or not os.path.isdir(CAPTURES_DIR):
        return

    cutoff = time.time() - CAPTURES_RETENTION_DAYS * 86400
    removed = 0
    for label_name in os.listdir(CAPTURES_DIR):
        label_dir = os.path.join(CAPTURES_DIR, label_name)
        if not os.path.isdir(label_dir):
            continue
        for filename in os.listdir(label_dir):
            file_path = os.path.join(label_dir, filename)
            try:
                if os.path.isfile(file_path) and os.path.getmtime(file_path) < cutoff:
                    os.remove(file_path)
                    removed += 1
            except OSError as exc:
                app.logger.warning("Could not remove old capture %s: %s", file_path, exc)

    if removed:
        app.logger.info("Cleaned up %d capture(s) older than %d days", removed, CAPTURES_RETENTION_DAYS)


def _captures_cleanup_loop():
    while True:
        try:
            _cleanup_old_captures()
        except Exception:
            app.logger.exception("Capture cleanup failed")
        time.sleep(CAPTURES_CLEANUP_INTERVAL_SECONDS)


def notify(result, image_path):
    if not NOTIFY_WEBHOOK_URL:
        return
    try:
        requests.post(
            NOTIFY_WEBHOOK_URL,
            json={**result, "image_path": image_path},
            timeout=5,
        )
    except requests.RequestException as exc:
        app.logger.warning("Notification webhook failed: %s", exc)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "mode": detector.mode,
        "wordpress_configured": wordpress_client.is_configured(),
        "esp32_control_configured": remote_control.esp32_configured(),
    })


@app.route("/detect", methods=["POST"])
def detect():
    if DEVICE_API_KEY and not hmac.compare_digest(
        request.headers.get("X-Device-Key", ""), DEVICE_API_KEY
    ):
        return jsonify({"error": "unauthorized"}), 401

    raw = request.get_data()
    if not raw:
        return jsonify({"error": "empty request body"}), 400

    image_array = np.frombuffer(raw, dtype=np.uint8)
    image_bgr = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
    if image_bgr is None:
        return jsonify({"error": "could not decode JPEG"}), 400

    capture_timestamp = None
    try:
        capture_timestamp = int(request.headers.get("X-Capture-Time", ""))
    except ValueError:
        pass

    result = detector.classify(image_bgr)
    if capture_timestamp is not None:
        result["capture_timestamp"] = capture_timestamp
    image_path = save_capture(image_bgr, result["label"], capture_timestamp)
    app.logger.info("Frame: %s (saved to %s)", result, image_path)

    with _burst_lock:
        _burst_frames.append((time.time(), result, image_path))
        closed = _flush_burst_locked()

    if closed:
        _handle_burst_result(*closed)

    return jsonify(result)


_cleanup_old_captures()  # run once at startup, then every CAPTURES_CLEANUP_INTERVAL_SECONDS
threading.Thread(target=_burst_watchdog, daemon=True).start()
threading.Thread(target=_captures_cleanup_loop, daemon=True).start()
remote_control.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # threaded=True so a slow outbound call (WordPress/cloud AI) on one
    # request doesn't stall other incoming frames. For a real production
    # deployment (not just "runs reliably on my home network"), put this
    # behind a proper WSGI server (gunicorn/waitress) instead of Flask's
    # built-in one - see README "Production deployment".
    app.run(host="0.0.0.0", port=port, threaded=True)
