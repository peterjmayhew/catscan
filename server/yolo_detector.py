"""Optional local YOLOv8 backend (DETECTION_BACKEND=yolo). Runs entirely on
your PC via the `ultralytics` package - no cloud calls, no API key, no
per-image cost, unlike the cloud backend. Works on either CPU or GPU
(automatically uses CUDA if a GPU-enabled torch install is present).

Two things happen here:

1. Subject localization: a pretrained YOLOv8 detection model (trained on
   COCO, which includes a "cat" class) finds the cat in the frame. This is
   generally more robust than the Haar cascade used elsewhere in this
   project - it handles side-on poses and low-quality frames the cascade
   often misses, and downloads automatically on first use (no setup).
2. Identity classification: if you've trained a custom classifier with
   train_classifier_yolo.py (server/models/cat_classifier_yolo.pt), it
   decides my_cat/other_cat/no_cat - the same job the TensorFlow model or
   cloud backend do, just via a different framework/local-only approach.
   Without a trained classifier, this backend can still say "a cat is
   here" (from the pretrained detector) but reports identity as unknown -
   train it before relying on this for the actual my-cat-vs-their-cat
   decision.

Requires `pip install ultralytics`.
"""

import logging
import os

from ultralytics import YOLO

logger = logging.getLogger(__name__)

MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")
CLASSIFIER_PATH = os.path.join(MODEL_DIR, "cat_classifier_yolo.pt")
DETECTOR_MODEL = os.environ.get("YOLO_DETECTOR_MODEL", "yolov8n.pt")
COCO_CAT_CLASS_ID = 15
DETECTOR_CONFIDENCE_THRESHOLD = 0.4


class YoloDetector:
    def __init__(self):
        # Ultralytics downloads this checkpoint automatically on first use
        # if it isn't already cached locally.
        self._detector = YOLO(DETECTOR_MODEL)
        self._classifier = YOLO(CLASSIFIER_PATH) if os.path.exists(CLASSIFIER_PATH) else None
        if self._classifier is None:
            logger.info(
                "No trained YOLO classifier at %s - yolo backend will report "
                "cat/no_cat only, not identity, until you run "
                "train_classifier_yolo.py",
                CLASSIFIER_PATH,
            )

    def _find_cat_box(self, image_bgr):
        results = self._detector.predict(
            image_bgr,
            classes=[COCO_CAT_CLASS_ID],
            conf=DETECTOR_CONFIDENCE_THRESHOLD,
            verbose=False,
        )[0]
        boxes = results.boxes
        if boxes is None or len(boxes) == 0:
            return None

        # Largest box by area = most likely the actual subject, not
        # something small/distant in the background.
        areas = (boxes.xyxy[:, 2] - boxes.xyxy[:, 0]) * (boxes.xyxy[:, 3] - boxes.xyxy[:, 1])
        best_index = int(areas.argmax())
        x0, y0, x1, y1 = (int(v) for v in boxes.xyxy[best_index].tolist())
        confidence = float(boxes.conf[best_index])
        return x0, y0, x1, y1, confidence

    def classify(self, image_bgr):
        box = self._find_cat_box(image_bgr)
        if box is None:
            return {"cat_detected": False, "label": "no_cat", "confidence": 0.0, "mode": "yolo"}

        x0, y0, x1, y1, detection_confidence = box

        if self._classifier is None:
            return {
                "cat_detected": True,
                "label": "other_cat",  # safer unknown-default than assuming "my_cat"
                "confidence": round(detection_confidence, 3),
                "mode": "yolo",
                "identity_unavailable": True,
            }

        img_h, img_w = image_bgr.shape[:2]
        crop = image_bgr[max(y0, 0) : min(y1, img_h), max(x0, 0) : min(x1, img_w)]
        results = self._classifier.predict(crop, verbose=False)[0]
        probs = results.probs
        top_index = int(probs.top1)
        label = results.names[top_index]
        confidence = float(probs.top1conf)

        return {
            "cat_detected": label != "no_cat",
            "label": label,
            "confidence": round(confidence, 3),
            "mode": "yolo",
        }
