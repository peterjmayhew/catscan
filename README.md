# ESP32-CAM Tabby Cat Detector

Detects when a cat is in front of the camera and tells your tabby cats apart
from a neighbour's non-tabby cat, using a **Freenove ESP32-CAM Dev Board Kit**
as the camera node and a small server (a Raspberry Pi, home server, or your
laptop) to run the actual classification.

## Does the Freenove ESP32-CAM board's camera work for this, or do I need a better one?

**Short answer: the stock camera (OV2640, 2MP) is fine. You don't need a
better camera.**

Longer answer:

- Resolution isn't the bottleneck. Image classifiers for a task like this
  (cat vs. no cat, tabby vs. not) resize input images down to something like
  96x96–224x224 pixels before they ever look at them. A 2MP OV2640 has far
  more resolution than that already.
- The real constraint is the **ESP32 itself**, not the lens. The ESP32-CAM's
  microcontroller (even the WROVER with PSRAM that Freenove ships) has too
  little RAM and compute to run a reliable fine-grained image classifier
  (e.g. "which specific cat is this") in real time on its own. That's a
  genuinely hard problem for a $10 microcontroller.
- That's why this project splits the work: the ESP32-CAM's job is just to
  **watch for motion and grab a JPEG**; the classification runs on a small
  always-on computer on your network (Raspberry Pi, NAS, old laptop, etc.)
  using a proper CNN (transfer learning on MobileNetV2). That combination
  is very accurate and doesn't need new hardware.
- Things that *will* matter more than megapixels: **lighting** (add an IR
  illuminator or place the camera somewhere with consistent light for
  night use — the OV2640 has poor low-light performance) and **camera
  placement** (mount it so cats reliably walk through the frame at a
  consistent distance/angle — fixed-focus lenses have a narrow sweet spot).
- If you later want a fully standalone unit with **no server at all**, that
  does need different/better hardware — e.g. an ESP32-S3-EYE or an ESP32-S3
  camera board with more RAM/AI acceleration for on-device TinyML, or an
  Edge Impulse deployment. That's listed under "Future ideas" below but is
  not required for what you asked for.
- If you want a nicer image purely for your own review (not classification
  accuracy), an OV5640 module (5MP, autofocus) is a drop-in upgrade the
  Freenove board's socket also accepts — a "nice to have", not a blocker.

## How it works

```
 ESP32-CAM (Freenove)          Server (Pi / laptop / home server)
 ─────────────────────         ───────────────────────────────────
 PIR sensor triggers   ──HTTP──►  /detect endpoint receives JPEG
 camera capture (JPEG)           │
                                  ├─ 1. Is there a cat in frame at all?
                                  │    (OpenCV cat-face cascade)
                                  ├─ 2. Which cat? "my_tabby" / "other_cat"
                                  │    (trained MobileNetV2 classifier,
                                  │     falls back to a colour/texture
                                  │     heuristic if no model is trained yet)
                                  └─ 3. Save image + log result,
                                       optionally call a notification webhook
```

Two classification modes are included:

1. **Heuristic mode** (works immediately, no training data needed): looks
   for a cat-like face with OpenCV's Haar cascade, then scores the coat for
   "stripy-ness" (edge/texture density) vs. colour uniformity. Tabby coats
   have much higher local texture variance than most solid-coloured cats.
   This is a reasonable starting point but will make mistakes — it can't
   tell your tabby apart from *another* tabby, for instance.
2. **Trained model mode** (much more accurate): once you've collected a
   couple hundred photos of your cats and the neighbour's cat (the server
   auto-saves every capture into `captures/` for exactly this purpose),
   run `server/train_classifier.py` to fine-tune a MobileNetV2 classifier
   on your own cats specifically. Drop the resulting model into
   `server/models/` and the server automatically switches to using it.

## Repository layout

```
firmware/esp32cam_cat_detector/esp32cam_cat_detector.ino  - ESP32-CAM sketch
firmware/esp32cam_cat_detector/config.example.h           - Wi-Fi/server settings template
server/app.py               - Flask server, receives images, returns a verdict
server/detector.py          - Cat detection + tabby classification logic
server/train_classifier.py  - Fine-tunes a MobileNetV2 classifier on your photos
server/requirements.txt     - Python dependencies
data/README.md              - How to collect a training set
captures/                   - Auto-saved incoming photos, sorted by verdict
```

## Hardware setup

1. Freenove ESP32-CAM Dev Board Kit (OV2640 camera).
2. A PIR motion sensor (the kit includes one) wired to **GPIO 13** and 5V/GND.
   GPIO 13 is one of the few pins on the AI-Thinker/Freenove pinout not used
   by the camera, so it's safe to use for the PIR's digital output.
3. Power the board from 5V (camera + Wi-Fi draws more current than USB-only
   power on some boards can reliably supply — use the kit's dedicated 5V
   supply/programmer, not just a phone charger through a thin cable).

## Firmware setup

1. Open `firmware/esp32cam_cat_detector/esp32cam_cat_detector.ino` in the
   Arduino IDE with the ESP32 board package installed.
2. Copy `config.example.h` to `config.h` in the same folder and fill in your
   Wi-Fi SSID/password and the server's URL (e.g.
   `http://192.168.1.50:5000/detect`). `config.h` is git-ignored so your
   credentials never get committed.
3. Board settings: **AI Thinker ESP32-CAM** (Freenove's board uses the same
   pinout), Partition Scheme: "Huge APP (3MB No OTA)", PSRAM: Enabled.
4. To flash: connect GPIO0 to GND, reset, upload, then disconnect GPIO0 and
   reset again to run normally (standard ESP32-CAM flashing procedure).

## Server setup

```bash
cd server
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

The server listens on `0.0.0.0:5000` by default (`PORT` env var to change
it) and exposes:

- `POST /detect` — body is a raw JPEG. Returns JSON:
  `{"cat_detected": true, "label": "my_tabby", "confidence": 0.94}`
- `GET /health` — simple liveness check.

Every image the server receives is saved under `captures/<label>/` with a
timestamp, both so you can review what it's seeing and so those images
become your training set for `train_classifier.py`.

## Training your own classifier

1. Let the heuristic mode run for a while (or manually sort photos) until
   you have images sorted into `data/my_tabby/`, `data/other_cat/`, and
   `data/no_cat/` — aim for at least ~100 images per class, more is better.
   See `data/README.md`.
2. Run:
   ```bash
   cd server
   python3 train_classifier.py
   ```
   This fine-tunes a MobileNetV2 (ImageNet-pretrained) on your images and
   writes `server/models/cat_classifier.h5` and a quantized
   `server/models/cat_classifier.tflite`.
3. Restart `app.py` — it will detect the model file and use it instead of
   the heuristic automatically.

## Notifications (optional)

Set the `NOTIFY_WEBHOOK_URL` environment variable before starting `app.py`
to have it `POST` a JSON payload (label + confidence + image URL) to any
webhook-based notifier (e.g. ntfy.sh, Home Assistant, a Discord webhook).
Left unset, the server just logs to the console.

## Future ideas (not implemented here)

- Fully on-device TinyML on an ESP32-S3 board (no server needed) via Edge
  Impulse, once you have a solid labelled dataset from this project.
- A physical deterrent (sprinkler/ultrasonic) triggered only on
  `other_cat`.
- Telegram/Pushover push notifications instead of a generic webhook.
