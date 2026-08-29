"""Optional cloud-AI backend: asks Claude's vision model to compare a new
capture against reference photos of your own cat(s).

Why this exists: the local heuristic can only judge coat *pattern* (stripy
vs. not), and even the trained CNN needs a real dataset to do well. If your
neighbour's cat happens to also be a tabby, neither is a sure thing without
real work. A general-purpose vision model can instead be asked directly
"is this the same cat as in these reference photos?", using colour,
markings, and build - much closer to how a person would tell two cats
apart, and it needs only a handful of reference photos, not hundreds of
labelled training images.

Trade-offs: each classification is an API call (a few seconds of latency
and a small per-image cost), so this suits the existing capture cooldown
rather than high-frequency triggering.

Enable by setting the ANTHROPIC_API_KEY environment variable and dropping
a handful of clear, well-lit photos of your cat(s) - different angles/poses
help - into data/reference_photos/ (.jpg/.jpeg). Leave either unset and the
server automatically falls back to the trained model or heuristic instead.
"""

import base64
import glob
import json
import os

import anthropic
import cv2

REFERENCE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "reference_photos")
MODEL_NAME = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
MAX_REFERENCE_PHOTOS = 6  # keeps each request small, fast, and cheap

SYSTEM_PROMPT = (
    "You are a strict JSON API for a home cat-identification camera. "
    "Respond with only a single JSON object and nothing else - no markdown, "
    "no code fences, no explanation outside the JSON."
)

PROMPT = (
    "The reference images above are photos of the homeowner's own cat(s). "
    "The final image is a new photo just captured by a security camera "
    "outside their house. Decide whether the final image shows one of the "
    "homeowner's own cat(s), a different cat, or no cat at all. Judge by "
    "coat colour, pattern/markings, and body shape - ignore background, "
    "pose, and lighting differences. "
    'Respond with exactly this JSON shape: {"cat_detected": bool, '
    '"is_my_cat": bool, "confidence": number between 0.0 and 1.0, '
    '"reasoning": "one short sentence"}'
)


class CloudClassifier:
    def __init__(self):
        # Raises if ANTHROPIC_API_KEY isn't set - caller treats that as
        # "cloud backend not configured" and falls back to another mode.
        self._client = anthropic.Anthropic()
        self._reference_images = self._load_reference_images()
        if not self._reference_images:
            raise RuntimeError(f"No reference photos found in {REFERENCE_DIR}")

    def _load_reference_images(self):
        paths = sorted(
            glob.glob(os.path.join(REFERENCE_DIR, "*.jpg"))
            + glob.glob(os.path.join(REFERENCE_DIR, "*.jpeg"))
        )
        images = []
        for path in paths[:MAX_REFERENCE_PHOTOS]:
            with open(path, "rb") as f:
                images.append(base64.standard_b64encode(f.read()).decode("utf-8"))
        return images

    def _parse_response(self, text):
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        return json.loads(text)

    def classify(self, image_bgr):
        success, buffer = cv2.imencode(".jpg", image_bgr)
        if not success:
            raise RuntimeError("Failed to encode capture as JPEG")
        capture_b64 = base64.standard_b64encode(buffer).decode("utf-8")

        content = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": ref_b64},
            }
            for ref_b64 in self._reference_images
        ]
        content.append({"type": "text", "text": PROMPT})
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": capture_b64},
            }
        )

        response = self._client.messages.create(
            model=MODEL_NAME,
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
        raw_text = response.content[0].text

        try:
            parsed = self._parse_response(raw_text)
        except (json.JSONDecodeError, IndexError):
            # Don't let an unparseable reply take the whole request down -
            # report it as "unknown" rather than crashing /detect.
            return {
                "cat_detected": False,
                "label": "no_cat",
                "confidence": 0.0,
                "mode": "cloud",
                "reasoning": f"unparseable model response: {raw_text[:200]!r}",
            }

        cat_detected = bool(parsed.get("cat_detected", False))
        is_my_cat = bool(parsed.get("is_my_cat", False))
        label = "no_cat"
        if cat_detected:
            label = "my_cat" if is_my_cat else "other_cat"

        return {
            "cat_detected": cat_detected,
            "label": label,
            "confidence": round(float(parsed.get("confidence", 0.5)), 3),
            "mode": "cloud",
            "reasoning": parsed.get("reasoning", ""),
        }
