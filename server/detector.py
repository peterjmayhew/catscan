"""Cat detection + tabby-vs-other classification.

Two modes, picked automatically:

- "model" mode: if server/models/cat_classifier.h5 exists, load it and use
  it for everything (cat/no-cat and which-cat). This is the accurate path,
  but needs you to have trained it first (see train_classifier.py).
- "heuristic" mode: the fallback used until you've trained a model. Finds a
  cat-like face with OpenCV's built-in Haar cascades, then scores the coat
  for "stripy-ness" using edge/texture density. Tabby coats produce much
  higher local texture variance than solid-coloured coats, so this is a
  reasonable - if imperfect - starting point.
"""

import os

import cv2
import numpy as np

LABELS = ["no_cat", "my_tabby", "other_cat"]
MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")
MODEL_PATH = os.path.join(MODEL_DIR, "cat_classifier.h5")

# Heuristic thresholds (see README "Training your own classifier" - the
# heuristic is a starting point, not a substitute for the trained model).
STRIPY_SCORE_THRESHOLD = 18.0  # Laplacian-variance-based texture score
CASCADE_MIN_NEIGHBORS = 8


class CatDetector:
    def __init__(self):
        self._face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalcatface_extended.xml"
        )
        self._model = self._try_load_model()

    def _try_load_model(self):
        if not os.path.exists(MODEL_PATH):
            return None
        # Imported lazily so the server can run in heuristic-only mode
        # without requiring tensorflow to be installed.
        import tensorflow as tf

        return tf.keras.models.load_model(MODEL_PATH)

    @property
    def mode(self):
        return "model" if self._model is not None else "heuristic"

    def find_cat_face(self, image_bgr):
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        faces = self._face_cascade.detectMultiScale(
            gray, scaleFactor=1.05, minNeighbors=CASCADE_MIN_NEIGHBORS, minSize=(60, 60)
        )
        if len(faces) == 0:
            return None
        # Largest detected face = most likely the subject, not background clutter.
        return max(faces, key=lambda f: f[2] * f[3])

    def _stripy_score(self, region_bgr):
        gray = cv2.cvtColor(region_bgr, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (128, 128))
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())

    def classify(self, image_bgr):
        if self._model is not None:
            return self._classify_with_model(image_bgr)
        return self._classify_heuristic(image_bgr)

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

    def _classify_heuristic(self, image_bgr):
        face = self.find_cat_face(image_bgr)
        if face is None:
            return {
                "cat_detected": False,
                "label": "no_cat",
                "confidence": 0.0,
                "mode": "heuristic",
            }

        x, y, w, h = face
        region = image_bgr[y : y + h, x : x + w]
        score = self._stripy_score(region)
        is_tabby = score >= STRIPY_SCORE_THRESHOLD

        # Turn distance from the threshold into a rough 0.5-1.0 confidence.
        spread = max(abs(score - STRIPY_SCORE_THRESHOLD), 0.0)
        confidence = min(0.5 + spread / (STRIPY_SCORE_THRESHOLD * 2), 0.99)

        return {
            "cat_detected": True,
            "label": "my_tabby" if is_tabby else "other_cat",
            "confidence": round(confidence, 3),
            "mode": "heuristic",
            "stripy_score": round(score, 2),
        }
