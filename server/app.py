"""Flask server for the ESP32-CAM tabby cat detector.

Receives a raw JPEG frame from the ESP32-CAM on motion, classifies it, saves
it under captures/<label>/ (both for your review and as future training
data), and optionally forwards the result to a notification webhook.
"""

import os
import time

import cv2
import numpy as np
import requests
from flask import Flask, jsonify, request

from detector import CatDetector

app = Flask(__name__)
detector = CatDetector()

CAPTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "captures")
NOTIFY_WEBHOOK_URL = os.environ.get("NOTIFY_WEBHOOK_URL")


def save_capture(image_bgr, label):
    label_dir = os.path.join(CAPTURES_DIR, label)
    os.makedirs(label_dir, exist_ok=True)
    filename = f"{time.strftime('%Y%m%d-%H%M%S')}.jpg"
    path = os.path.join(label_dir, filename)
    cv2.imwrite(path, image_bgr)
    return path


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
    return jsonify({"status": "ok", "mode": detector.mode})


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
    app.logger.info("Detection: %s (saved to %s)", result, image_path)

    if result["label"] == "other_cat":
        notify(result, image_path)

    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
