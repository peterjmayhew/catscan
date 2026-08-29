# ESP32-CAM Cat Detector

Detects when a cat is in front of the camera and tells *your* cat(s) apart
from a neighbour's cat, using a **Freenove ESP32-CAM Dev Board Kit** as the
camera node and a small server (a Raspberry Pi, home server, or your
laptop) to run the actual classification. Optionally logs every detection
- image, label, confidence, timestamp - to a WordPress site via the
included plugin.

## How reliable can this actually be?

Short version: **reliably telling two specific cats apart needs either a
trained model or an AI vision backend - a simple rule-of-thumb like "is it
stripy" cannot do it**, and it's worth being upfront about why before you
build on top of this.

Three backends are included, in increasing order of reliability:

1. **Heuristic** (default, zero setup): scores the coat for "stripy-ness".
   This only tells tabby-patterned coats from plain ones. **If your
   neighbour's cat is also a tabby, this mode cannot distinguish them at
   all** - it isn't actually looking at identity, just pattern. Good for
   getting something running on day one, not for the real problem.
2. **Trained model** (`server/train_classifier.py`): a MobileNetV2 CNN
   fine-tuned on your own labelled photos. This looks at the whole
   coat/face, not just "stripy or not", so it *can* separate two similar
   tabbies - but only as well as the dataset you train it on. Needs
   ~100+ photos per cat/class, ideally including several photos of the
   neighbour's cat specifically (not just "other cats in general").
3. **Cloud AI** (`server/cloud_classifier.py`, new): sends the capture plus
   a handful of reference photos of your cat(s) to Claude's vision model
   and asks it to judge identity directly - no training step, and it
   reasons about colour/markings/build the way a person would, so it
   handles "two similar tabbies" without needing hundreds of examples.
   Needs only `ANTHROPIC_API_KEY` and 3-6 reference photos. Trade-off: a
   few seconds of latency and a small per-image API cost, so it's for the
   already-throttled capture rate here, not a live video feed.

See "Reliable identification" further down for how each is enabled, and
"Multi-frame consensus" for how single noisy frames get smoothed out
regardless of which backend you use.

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

## Detecting cats after dark

Splitting this into two separate problems makes it much less daunting:

- **Knowing something is there.** PIR (body heat) and the ultrasonic sensor
  (reflected sound) added below both work exactly the same in pitch
  darkness as in daylight. Triggering isn't a night-time problem at all.
- **Getting the camera to actually see it.** This is the real challenge — a
  2MP OV2640 with no illumination just captures black. This project handles
  it the simplest way, with no extra hardware beyond what the kit already
  has: the onboard white flash LED (GPIO 4) fires a brief pulse right
  before every night capture (an LDR/light sensor decides when it's dark
  enough to bother, see wiring below), and the firmware nudges the sensor's
  gain/exposure settings to make the most of that light.
- This is genuinely a compromise, not "true" night vision: the flash is
  visible light, so it'll briefly light up the area (and the cat) each
  time, and even with the flash, night frames are noisier/grainier than
  daylight ones — expect somewhat lower classification confidence at night
  (the server flags this via a `low_light` field in its response).
- If you want proper invisible-light night vision instead, that needs a
  hardware change: an IR LED illuminator (850nm/940nm) plus a camera module
  with its IR-cut filter removed (stock OV2640/OV5640 modules include one,
  which blocks the IR light you're trying to illuminate with). That's a
  bigger step — a dedicated "NoIR" camera module, or physically removing
  the tiny IR-cut glass from the lens barrel — and trades away accurate
  daytime colour, so it isn't done here. The flash-based approach above is
  the pragmatic default; treat the IR route as an optional future upgrade.
- Whichever route you use, **include night-time captures in your training
  set** (see `data/README.md`) — a classifier trained only on daylight
  photos won't generalise well to grainy night ones.

## How it works

```
 ESP32-CAM (Freenove)          Server (Pi / laptop / home server)
 ─────────────────────         ───────────────────────────────────
 PIR/ultrasonic trigger        Each frame -> detector.classify():
   -> burst of N frames          1. cloud AI, if configured, else
   ──HTTP──► /detect receives       trained model, else heuristic
             each JPEG            2. label: no_cat / my_cat / other_cat
                                 Burst of frames -> majority-vote consensus
                                   -> save image, log to WordPress,
                                      optionally call a notification webhook
```

Each `/detect` call classifies and responds to that one frame immediately;
the burst it belongs to is combined into a single consensus result behind
the scenes before anything gets logged to WordPress (see "Multi-frame
consensus" below) - this smooths out the odd bad frame instead of treating
every frame as its own verdict.

## Repository layout

```
firmware/esp32cam_cat_detector/esp32cam_cat_detector.ino  - ESP32-CAM sketch
firmware/esp32cam_cat_detector/config.example.h           - Wi-Fi/server settings template
server/app.py                - Flask server: receives frames, aggregates bursts, dispatches results
server/detector.py           - Backend selection (cloud/model/heuristic) + classification logic
server/cloud_classifier.py   - Cloud AI backend: identity matching via Claude's vision model
server/train_classifier.py   - Fine-tunes a MobileNetV2 classifier on your photos
server/wordpress_client.py   - Uploads each detection to the WordPress plugin
server/requirements.txt      - Python dependencies
data/README.md               - How to collect a training set
data/reference_photos/       - Reference photos for the cloud AI backend
captures/                    - Auto-saved incoming photos, sorted by verdict
wordpress-plugin/catscan-detections/  - WordPress plugin (see "WordPress integration")
```

## Hardware setup

1. Freenove ESP32-CAM Dev Board Kit (OV2640 camera).
2. A PIR motion sensor (the kit includes one) wired to **GPIO 13** and 5V/GND.
   GPIO 13 is one of the few pins on the AI-Thinker/Freenove pinout not used
   by the camera, so it's safe to use for the PIR's digital output.
3. *(Optional but recommended)* An HC-SR04 ultrasonic sensor, wired to
   **TRIG → GPIO 14**, **ECHO → GPIO 15** (through a voltage divider — see
   below), plus 5V/GND. This is a second, independent motion trigger that
   works identically in full darkness, unlike the camera. Set
   `USE_ULTRASONIC` to `false` in `config.h` if you skip this — the PIR
   alone is still enough to run the project.
   - **Voltage divider required on ECHO**: the HC-SR04 outputs 5V, but the
     ESP32's GPIOs only tolerate 3.3V. Wire a 1kΩ resistor in series from
     ECHO to the ESP32 pin, then a 2kΩ resistor from that junction to GND.
     Skipping this can damage the GPIO.
4. *(Optional)* An LDR (photoresistor) in a voltage-divider pair with a
   fixed resistor (e.g. 10kΩ), wired so more ambient light gives a higher
   voltage, feeding **GPIO 33**. This lets the firmware decide when it's
   dark enough to use a longer flash pulse (see "Detecting cats after
   dark" above). Without one, `isDark()` will just read whatever GPIO 33
   floats to — fine to leave disconnected if you don't need this, but wire
   it up if you want the night-time flash behaviour to actually work.
5. Power the board from 5V (camera + Wi-Fi draws more current than USB-only
   power on some boards can reliably supply — use the kit's dedicated 5V
   supply/programmer, not just a phone charger through a thin cable).

Board pinouts vary slightly between ESP32-CAM clones/revisions — double
check GPIO 14/15/33 are actually free on yours (some are shared with the
microSD slot) before wiring, and adjust the `#define`s in `config.h` if not.

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

- `POST /detect` — body is a raw JPEG, one per frame. Returns that single
  frame's own classification as JSON:
  `{"cat_detected": true, "label": "my_cat", "confidence": 0.94, "low_light": false}`
  (`low_light: true` flags a dim/night frame). This is per-frame, not the
  burst consensus - see "Multi-frame consensus" below for what actually
  gets logged/uploaded.
- `GET /health` — liveness check; also reports which backend is active
  (`"mode": "cloud" | "model" | "heuristic"`) and whether WordPress upload
  is configured.

Every image the server receives is saved under `captures/<label>/` with a
timestamp, both so you can review what it's seeing and so those images
become your training set for `train_classifier.py`.

### Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `5000` | Server port |
| `DETECTION_BACKEND` | `auto` | Force `cloud`, `model`, or `heuristic`; `auto` picks the best one that's configured |
| `ANTHROPIC_API_KEY` | unset | Enables the cloud AI backend |
| `ANTHROPIC_MODEL` | `claude-sonnet-5` | Model used for cloud identification |
| `BURST_SIZE` | `3` | Frames per burst before closing it out early - match `BURST_FRAME_COUNT` in `config.h` |
| `BURST_WINDOW_SECONDS` | `10` | Max time to wait for a full burst before closing it out anyway |
| `WORDPRESS_URL` | unset | Enables uploading detections to your WordPress site |
| `WORDPRESS_API_KEY` | unset | From Settings → CatScan in WordPress admin |
| `NOTIFY_WEBHOOK_URL` | unset | Generic webhook (ntfy.sh, Home Assistant, Discord, etc.), called on `other_cat` |

## Training your own classifier

1. Let the heuristic mode run for a while (or manually sort photos) until
   you have images sorted into `data/my_cat/`, `data/other_cat/`, and
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

## Cloud AI backend (optional, recommended if training data is a hassle)

Instead of (or before) collecting hundreds of training photos, you can ask
Claude's vision model to compare each capture against a few reference
photos of your own cat(s) directly - useful if you want good identity
matching immediately, or as a sanity check against the trained model's
verdicts.

1. Get an API key from the Anthropic Console and set it:
   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...
   ```
2. Drop 3-6 clear photos of your cat(s) into `data/reference_photos/` (see
   `data/reference_photos/README.md`).
3. Restart `app.py`. With `DETECTION_BACKEND=auto` (the default), it
   automatically prefers this over the trained model or heuristic once
   both the API key and reference photos are present; `GET /health` will
   report `"mode": "cloud"`.

This adds a couple of seconds of latency and a small per-image cost to
each classification - fine given the existing capture cooldown, not meant
for high-frequency triggering. Set `DETECTION_BACKEND=model` or
`DETECTION_BACKEND=heuristic` to opt out and force a different backend.

## Multi-frame consensus (burst capture)

A single frame is a shaky basis for "is this my cat or the neighbour's" -
motion blur, a bad angle, or an ear blocking the face can flip the verdict.
Each PIR/ultrasonic trigger captures a short burst of frames
(`BURST_FRAME_COUNT` in `config.h`, default 3) instead of one.

The server buffers frames from the same burst and, once it has enough of
them (or `BURST_WINDOW_SECONDS` passes), picks the majority-vote label
across the burst and averages the confidence of the frames that agreed
with it. That consensus - not any single frame - is what gets saved as the
"representative" image and forwarded to WordPress/the notification
webhook. Each individual `/detect` call still gets its own immediate
per-frame response (so the firmware doesn't have to wait), but downstream
consumers only see the settled-on result.

Keep `BURST_SIZE` (server) and `BURST_FRAME_COUNT` (firmware) in sync so a
full burst closes itself out immediately rather than always waiting for
the timeout.

## WordPress integration

The included plugin (`wordpress-plugin/catscan-detections/`) logs every
detection - image, label, confidence, timestamp, and detection metadata -
as a post in your WordPress site, so you can browse the history without
touching the server directly.

**Install:**

1. Copy `wordpress-plugin/catscan-detections/` into your WordPress site's
   `wp-content/plugins/` directory (or zip it and upload via Plugins → Add
   New → Upload Plugin).
2. Activate "CatScan Detections" in the WordPress admin.
3. Go to **Settings → CatScan** and copy the site URL and API key shown
   there.
4. Set them on your server before starting `app.py`:
   ```bash
   export WORDPRESS_URL=https://yoursite.example.com
   export WORDPRESS_API_KEY=<the key from the settings page>
   ```

**What gets uploaded:** after each burst is aggregated (see above), the
server POSTs the representative JPEG plus `label`, `confidence`,
`cat_detected`, `mode`, `low_light`, `frame_count`, and (in cloud mode) the
model's `reasoning` to `/wp-json/catscan/v1/detections`, authenticated with
an `X-Api-Key` header. The plugin stores it as a "Cat Detection" post with
the image as its featured image, viewable under its own admin menu (with
label/confidence/mode columns) or via the `[catscan_recent limit="12"
label="all"]` shortcode (label can be `all`, `my_cat`, `other_cat`, or
`no_cat`) to show a gallery anywhere on the site.

If `WORDPRESS_URL` is unset, this is skipped entirely - the rest of the
project works the same without it.

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
