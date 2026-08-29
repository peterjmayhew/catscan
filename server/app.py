"""Flask server for the ESP32-CAM cat detector.

Receives raw JPEG frames from the ESP32-CAM on motion (a short burst per
trigger - see BURST_SIZE below), classifies each one, saves it under
captures/<label>/ (both for your review and as future training data), and
combines the burst into one majority-vote result which gets forwarded to
WordPress and/or a generic notification webhook.
"""

import os
import threading
import time
from collections import Counter

import cv2
import numpy as np
import requests
from flask import Flask, jsonify, request

import wordpress_client
from detector import CatDetector

app = Flask(__name__)
detector = CatDetector()

CAPTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "captures")
NOTIFY_WEBHOOK_URL = os.environ.get("NOTIFY_WEBHOOK_URL")

# A single frame is a noisy basis for "my cat or the neighbour's" - one bad
# angle or motion-blurred frame can flip the verdict. The firmware sends a
# short burst per trigger (BURST_FRAME_COUNT in config.h); these two should
# roughly match so a full burst closes the window itself rather than always
# waiting out the timeout.
BURST_SIZE = int(os.environ.get("BURST_SIZE", 3))
BURST_WINDOW_SECONDS = float(os.environ.get("BURST_WINDOW_SECONDS", 10))

_burst_lock = threading.Lock()
_burst_frames = []  # list of (timestamp, result, image_path)


def save_capture(image_bgr, label):
    label_dir = os.path.join(CAPTURES_DIR, label)
    os.makedirs(label_dir, exist_ok=True)
    filename = f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.jpg"
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
        notify(result, image_path)


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
    })


@app.route("/detect", methods=["POST"])
def detect():
    raw = request.get_data()
    if not raw:
        return jsonify({"error": "empty request body"}), 400

    image_array = np.frombuffer(raw, dtype=np.uint8)
    image_bgr = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
    if image_bgr is None:
        return jsonify({"error": "could not decode JPEG"}), 400

    result = detector.classify(image_bgr)
    image_path = save_capture(image_bgr, result["label"])
    app.logger.info("Frame: %s (saved to %s)", result, image_path)

    with _burst_lock:
        _burst_frames.append((time.time(), result, image_path))
        closed = _flush_burst_locked()

    if closed:
        _handle_burst_result(*closed)

    return jsonify(result)


threading.Thread(target=_burst_watchdog, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
