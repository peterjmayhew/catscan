"""Cat detection + identity classification (your cat vs. a neighbour's).

Four backends, picked automatically in this order of preference (each
falls back to the next if it isn't set up):

- "cloud": asks Claude's vision model to compare the capture against a
  handful of reference photos of your own cat(s) in data/reference_photos/.
  No training required, and unlike the local modes it judges actual
  identity (coat colour/pattern + build), not just "is it stripy" - so it's
  the only mode that reliably works out of the box if the neighbour's cat
  is *also* a tabby. Requires ANTHROPIC_API_KEY. See README "Reliable
  identification".
- "model": if server/models/cat_classifier.h5 exists, a MobileNetV2
  classifier fine-tuned on your own labelled photos (train_classifier.py).
  Accurate once trained, including on two similar-looking tabbies, but
  needs you to collect a real dataset first.
- "yolo": if server/models/cat_classifier_yolo.pt exists, a YOLOv8
  classifier fine-tuned the same way (train_classifier_yolo.py) - same job
  as "model", different framework, entirely local (no TensorFlow needed).
  Also uses a pretrained YOLOv8 *detector* for locating the cat in-frame,
  which is more robust than the Haar cascade the other backends rely on.
- "heuristic": the zero-setup fallback. Finds a cat-like face with OpenCV's
  built-in Haar cascades, then scores the coat for "stripy-ness" using
  edge/texture density. This only distinguishes tabby vs. non-tabby coat
  *pattern* - it cannot tell two tabbies apart, so if your neighbour's cat
  is also a tabby, this mode will not reliably solve your actual problem.
  Use it to get something running today, then move to "model"/"yolo" or
  "cloud".

Set DETECTION_BACKEND=cloud|model|yolo|heuristic to force one; default
"auto" uses the best one that's actually configured.
"""

import logging
import os

import cv2
import numpy as np

LABELS = ["no_cat", "my_cat", "other_cat"]
MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")
MODEL_PATH = os.path.join(MODEL_DIR, "cat_classifier.h5")
BACKEND = os.environ.get("DETECTION_BACKEND", "auto")

# Heuristic thresholds (see README "Training your own classifier" - the
# heuristic is a starting point, not a substitute for the trained model).
STRIPY_SCORE_THRESHOLD = 18.0  # Laplacian-variance-based texture score
CASCADE_MIN_NEIGHBORS = 8

# Mean grayscale brightness (0-255) below which a frame is treated as
# low-light - e.g. a night capture lit only by the ESP32-CAM's flash. Below
# this, the heuristic path denoises and contrast-enhances the frame before
# scoring it, and confidence is discounted since night frames are grainier.
LOW_LIGHT_MEAN_THRESHOLD = 60.0


class CatDetector:
    def __init__(self):
        self._face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalcatface_extended.xml"
        )
        self._cloud = None
        self._model = None
        self._yolo = None

        if BACKEND in ("auto", "cloud"):
            self._cloud = self._try_init_cloud()
        if self._cloud is None and BACKEND in ("auto", "model"):
            self._model = self._try_load_model()
        if self._cloud is None and self._model is None and BACKEND in ("auto", "yolo"):
            self._yolo = self._try_init_yolo()

    def _try_init_cloud(self):
        try:
            # Imported lazily so the server doesn't need the anthropic
            # package or an API key unless you actually opt into cloud mode.
            from cloud_classifier import CloudClassifier

            return CloudClassifier()
        except Exception as exc:  # not installed, not configured, or misconfigured
            if BACKEND == "cloud":
                raise
            logging.getLogger(__name__).info(
                "Cloud AI backend not available, falling back: %s", exc
            )
            return None

    def _try_load_model(self):
        if not os.path.exists(MODEL_PATH):
            return None
        # Imported lazily so the server can run in heuristic-only mode
        # without requiring tensorflow to be installed.
        import tensorflow as tf

        return tf.keras.models.load_model(MODEL_PATH)

    def _try_init_yolo(self):
        try:
            from yolo_detector import CLASSIFIER_PATH, YoloDetector

            if BACKEND == "auto" and not os.path.exists(CLASSIFIER_PATH):
                # Don't let auto-mode pick a YOLO backend that can only ever
                # say "other_cat" for every detection - that's worse than
                # the heuristic's actual (if weak) attempt at a guess.
                # Explicitly requesting DETECTION_BACKEND=yolo still works
                # without a trained classifier, for detection-only use.
                return None
            return YoloDetector()
        except Exception as exc:  # not installed, or misconfigured
            if BACKEND == "yolo":
                raise
            logging.getLogger(__name__).info(
                "YOLOv8 backend not available, falling back: %s", exc
            )
            return None

    @property
    def mode(self):
        if self._cloud is not None:
            return "cloud"
        if self._model is not None:
            return "model"
        return "yolo" if self._yolo is not None else "heuristic"

    def _mean_brightness(self, image_bgr):
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        return float(gray.mean())

    def is_low_light(self, image_bgr):
        return self._mean_brightness(image_bgr) < LOW_LIGHT_MEAN_THRESHOLD

    def _enhance_low_light(self, gray):
        denoised = cv2.fastNlMeansDenoising(gray, h=10)
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        return clahe.apply(denoised)

    def find_cat_face(self, image_bgr, enhance=False):
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        if enhance:
            gray = self._enhance_low_light(gray)
        faces = self._face_cascade.detectMultiScale(
            gray, scaleFactor=1.05, minNeighbors=CASCADE_MIN_NEIGHBORS, minSize=(60, 60)
        )
        if len(faces) == 0:
            return None
        # Largest detected face = most likely the subject, not background clutter.
        return max(faces, key=lambda f: f[2] * f[3])

    def _stripy_score(self, region_bgr, enhance=False):
        gray = cv2.cvtColor(region_bgr, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (128, 128))
        if enhance:
            gray = self._enhance_low_light(gray)
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())

    def _crop_subject(self, image_bgr, low_light):
        """Crops to the detected cat, with padding, so the CNN/cloud model
        judges the animal itself rather than background clutter. Falls back
        to the full frame if the cascade doesn't find anything (it often
        misses side-on poses) - the classifier still gets a shot at it."""
        face = self.find_cat_face(image_bgr, enhance=low_light)
        if face is None:
            return image_bgr

        x, y, w, h = face
        pad_x, pad_y = int(w * 0.3), int(h * 0.3)
        img_h, img_w = image_bgr.shape[:2]
        x0, y0 = max(x - pad_x, 0), max(y - pad_y, 0)
        x1, y1 = min(x + w + pad_x, img_w), min(y + h + pad_y, img_h)
        return image_bgr[y0:y1, x0:x1]

    def classify(self, image_bgr):
        low_light = self.is_low_light(image_bgr)
        if self._cloud is not None:
            subject = self._crop_subject(image_bgr, low_light)
            result = self._classify_with_cloud(subject)
        elif self._model is not None:
            subject = self._crop_subject(image_bgr, low_light)
            result = self._classify_with_model(subject)
        elif self._yolo is not None:
            # YOLO does its own subject localization internally (its own
            # detector model, not the Haar cascade), so it gets the full frame.
            result = self._yolo.classify(image_bgr)
        else:
            result = self._classify_heuristic(image_bgr, low_light)
        result["low_light"] = low_light
        return result

    def _classify_with_cloud(self, image_bgr):
        return self._cloud.classify(image_bgr)

    def _classify_with_model(self, image_bgr):
        resized = cv2.resize(image_bgr, (224, 224))
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype("float32") / 255.0
        batch = np.expand_dims(rgb, axis=0)
        predictions = self._model.predict(batch, verbose=0)[0]
        top_index = int(np.argmax(predictions))
        label = LABELS[top_index]
        confidence = float(predictions[top_index])
        return {
            "cat_detected": label != "no_cat",
            "label": label,
            "confidence": round(confidence, 3),
            "mode": "model",
        }

    def _classify_heuristic(self, image_bgr, low_light=False):
        face = self.find_cat_face(image_bgr, enhance=low_light)
        if face is None:
            return {
                "cat_detected": False,
                "label": "no_cat",
                "confidence": 0.0,
                "mode": "heuristic",
            }

        x, y, w, h = face
        region = image_bgr[y : y + h, x : x + w]
        score = self._stripy_score(region, enhance=low_light)
        is_tabby = score >= STRIPY_SCORE_THRESHOLD

        # Turn distance from the threshold into a rough 0.5-1.0 confidence.
        spread = max(abs(score - STRIPY_SCORE_THRESHOLD), 0.0)
        confidence = min(0.5 + spread / (STRIPY_SCORE_THRESHOLD * 2), 0.99)
        if low_light:
            # Denoised/contrast-enhanced night frames are still noisier than
            # daylight ones, so the heuristic is less trustworthy here.
            confidence *= 0.85

        return {
            "cat_detected": True,
            "label": "my_cat" if is_tabby else "other_cat",
            "confidence": round(confidence, 3),
            "mode": "heuristic",
            "stripy_score": round(score, 2),
        }
