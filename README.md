# assisArm — eye-controlled assistive robotic arm

A gaze and blink controller that drives a small servo arm. Built for people
with severe paralysis, where the eyes are the only reliable input.

Everything runs locally. No camera frame ever leaves the machine.

```
        [webcam]
            │
   eye_test.py / eye_test_ratio.py     gaze + blinks → ArmActions hooks
            │
        arm_wifi.py                    HTTP over WiFi
            │
        ESP8266 (D1 mini)              5 × MG996R servos
```

---

## Which tracker to run

Two builds share the same behaviour and the same hooks. Pick one:

| file | measurement | notes |
|---|---|---|
| `eye_test.py` | **pixels** — pupil offset from the nose | what currently runs with the arm |
| `eye_test_ratio.py` | **ratios** — iris position inside the eyelid box | for higher resolution cameras; needs its own threshold tuning |

`arm_wifi.py` currently imports `eye_test`. To try the ratio build with the arm,
change that one import.

---

## What it does

**Gaze directions.** Holding a direction for `HOLD_SECONDS` speaks it. Keeping
the eyes in that same zone for another `CONFIRM_SECONDS` speaks `confirm`.
Leaving the zone — into another direction or back to the centre — wipes the
slate, so the next visit starts from zero.

**Blink gestures.** Three non-overlapping closure bands:

| closure | meaning |
|---|---|
| short (0.005 – 0.10 s in the tuned build) | confirm support / starts a fetch in pairs |
| 0.10 – 0.50 s | dead zone, ignored — the width of an ordinary blink |
| ≥ 0.50 s | long blink; three of them toggle the system on or off |

**Fetch.** Two short blinks open a fetch and the speaker says `fetch`. From
then on nothing else listens: the power toggle and the blink pairs are both
suppressed. **A full eye closure stops the fetch immediately** and speaks
`stop`, followed by `back` one second later.

A one second grace period follows the opening blink, otherwise the blink that
started the fetch would immediately stop it.

> Stopping is deliberately the sensitive one. A false stop costs nothing; a
> missed stop costs an accident. Fetch is the opposite: it is a deliberate
> action, so it takes two blinks to start.

**Arm hooks.** Every confirmed event calls a method on an `ArmActions`
instance, so hardware plugs in without touching the gaze logic.

---

## Files

| file | purpose |
|---|---|
| `eye_test.py` | pixel tracker; silent build, everything drawn on the preview |
| `eye_test_ratio.py` | ratio tracker for the higher resolution camera |
| `eye_test_verbose.py` | pixel tracker with console prints, for tuning thresholds |
| `arm_wifi.py` | wires the hooks to the ESP8266 over HTTP |
| `arm_example.py` | minimal example of subclassing `ArmActions` |
| `firmware/assisArm/assisArm.ino` | ESP8266 firmware: WiFi + web page + HTTP API |
| `requirements.txt` | pinned Python packages |

---

## Requirements

- Python 3.11 (3.13 is untested: mediapipe 0.10.14 has no wheels for it)
- `pip install -r requirements.txt`

The pins are load-bearing:

| package | version | why |
|---|---|---|
| `mediapipe` | 0.10.14 | 1.x removed the `mp.solutions` API this script depends on |
| `opencv-python` | 4.11.0.86 | 5.x requires numpy>=2, which conflicts with mediapipe |
| `numpy` | 1.26.4 | must match what mediapipe 0.10.14 was built against |

Speech uses the Windows `System.Speech` engine through a short-lived PowerShell
process — no extra package, no network.

---

## Hardware

**Controller:** LOLIN(WEMOS) D1 mini, ESP8266, 4 MB flash, CH340 USB-serial.

**Actuators:** 5 × MG996R servos.

| joint | GPIO | D pin |
|---|---|---|
| stand (base rotate) | 14 | D5 |
| arm vertical 1 | 4 | D2 |
| arm vertical 2 | 0 | D3 |
| arm vertical 3 | 16 | D0 |
| hand (gripper) | 5 | D1 |

> **GPIO16 does not work with the `Servo` library** — attaching a servo there
> crashes the board before `setup()` finishes, leaving it silent on serial. It
> is currently disabled in the firmware. Move that servo to a free PWM pin
> (D6/GPIO12 or D7/GPIO13) if a third vertical joint is needed.

> **Power:** five MG996R draw up to 2.5 A each at stall. They must have their
> own 5–6 V supply of at least 5 A, with a common ground to the board. Feeding
> them from the D1 mini's 5 V pin browns out the board and resets it.

---

## Flashing the firmware

`arduino-cli` with the ESP8266 core:

```
arduino-cli core install esp8266:esp8266
arduino-cli compile --fqbn esp8266:esp8266:d1_mini_clone firmware/assisArm
arduino-cli upload  -p COM6 --fqbn esp8266:esp8266:d1_mini_clone firmware/assisArm
```

The sketch joins a WiFi network in station mode and prints its address on the
serial console at 115200:

```
assisArm servo controller
joining Galaxy A55 5G 78A5
..............
ready at http://192.168.244.157
http server started
```

Set `USE_STA`, `STA_SSID` and `STA_PASS` at the top of the sketch. If the join
fails it falls back to its own access point, `ESP8266-ARM` / `88888888`,
reachable at `http://192.168.4.1`.

> The phone hotspot hands out a **different subnet every time it restarts**, so
> the board's address moves. Read the serial line, or scan the /24, then pass
> `--host` or update `DEFAULT_HOST` in `arm_wifi.py`.

### HTTP API

| request | effect |
|---|---|
| `GET /` | slider page, one per joint |
| `GET /status` | JSON of every angle |
| `GET /set?ch=stand&a=90` | set one joint, 10–170° |
| `GET /step?ch=stand&d=10` | nudge one joint |
| `GET /home` | every joint to its home angle |

---

## Running

**Just the eye tracker:**

```
python eye_test.py
python eye_test_ratio.py
```

Calibrate by looking straight ahead when prompted, then hold a direction.

**With the arm:**

```
python arm_wifi.py
python arm_wifi.py --host 192.168.244.157 --step 10
```

`arm_wifi.py` checks the arm before calibrating and reports the address it
found. While the arm is silent it speaks `waiting` every four seconds and
stops as soon as contact returns.

**Wiring your own arm:**

```python
from eye_test import ArmActions, main

class MyArm(ArmActions):
    def on_left(self):
        arm.move(x=-50)
    def on_fetch(self):
        arm.grip(close=True)

main(MyArm())
```

| hook | fires when | wired on the ESP8266 arm |
|---|---|---|
| `on_left` / `on_right` | that direction was confirmed | base rotates |
| `on_up` / `on_down` | that direction was confirmed | not wired yet |
| `on_centre` | the eyes came back to rest | nothing |
| `on_fetch` | fetch started | claw opens |
| `on_stop` | fetch stopped (eyes closed) | claw closes |
| `on_back` | one second after `on_stop` | nothing yet |
| `on_system_on` | triple long blink, power on | nothing |
| `on_system_off` | triple long blink, power off | every joint home |
| `on_shutdown` | program closing, any route | every joint home |

---

## Tuning

All thresholds are command-line flags; `--help` lists them.

**Pixel build** (`eye_test.py`):

| flag | default | effect |
|---|---|---|
| `--left-px` `--right-px` | 12 | travel needed for left/right |
| `--up-px` `--down-px` | 20 | travel needed for up/down |
| `--center-px` | 10 | band that counts as "no direction" |
| `--hold` | 1.0 | hold a direction this long to hear it |
| `--confirm-after` | 1.0 | keep holding this much longer to hear `confirm` |
| `--blink-gap` | 2.0 | two short blinks must be this close together |
| `--ear-open` `--ear-closed` | 0.150 / 0.090 | eye aspect ratio bands |
| `--short-min` `--short-max` `--long-min` | 0.05 / 0.30 / 0.50 | blink bands, seconds |
| `--dx0` `--dy0` | — | reuse a previous calibration |
| `--flip-y` | off | invert up/down if the camera orientation needs it |
| `--plain` | off | raw camera image, no overlay |
| `--no-preview` | off | headless |

**Ratio build** (`eye_test_ratio.py`) uses `--h0 --v0` for calibration and
`--left-r --right-r --up-r --down-r --center-r` in ratio units, where 0.12
means the iris travelled 12% of the eye width. `DOWN_R` is deliberately smaller
than `UP_R`: looking down is limited by the lower lid, so the iris barely
moves. A lid guard suppresses `down` once the eyelids nearly meet, otherwise
every blink would read as a downward glance.

**Calibration notes.** Sit about 40–50 cm from the camera so the face fills
roughly a third of the frame. Keep the head straight — a tilted head shifts the
two eyes vertically and corrupts the up/down reading. Recalibrate whenever the
seating position, distance or lighting changes.

---

## Keys

| key | action |
|---|---|
| `ENTER` | save the raw frame to `saved_frames\` |
| `Q` / `ESC` | quit |
| window X | quit and park the arm at home |

Saved frames are the raw camera image. `saved_frames\` is gitignored.

---

## Status

**Working:** 4-direction gaze, confirm, blink gestures, power toggle, fetch with
closure-to-stop, WiFi arm control over HTTP, arm hooks, park on exit.

**Open:**
- map `up` / `down` onto the remaining arm joints
- decide what `back` should physically do
- free a PWM pin for the third vertical servo (GPIO16 is unusable)
- the ratio build still needs its thresholds tuned against a live arm
