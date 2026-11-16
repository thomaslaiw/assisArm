# assisArm — eye-controlled assistive robotic arm

A gaze and blink controller that drives a small servo arm. Built for people
with severe paralysis, where the eyes are the only reliable input.

Everything runs locally. No camera frame ever leaves the machine.

```
        [webcam]
            │
        eye_test.py            gaze + blinks  →  ArmActions hooks
            │
        arm_wifi.py            HTTP over WiFi
            │
        ESP8266 (D1 mini)      5 × MG996R servos
```

---

## What it does

**Gaze directions.** The controller tracks four points every frame:

```
a = my left pupil        b = my right pupil
c = nose tip             d = midpoint of a and b
e = midpoint of the upper eyelids   (shown for diagnostics)
```

Calibration records the resting offset of `d` from `c`:

```
dx0 = d.x - c.x          dy0 = d.y - c.y
```

Because `c` is the nose, the offsets survive the head moving — the nose shifts
with the pupils, so the difference stays put.

The gaze is then compared with that centre:

| measurement | reads as |
|---|---|
| `x > dx0 + LEFT_PX` | left |
| `x < dx0 - RIGHT_PX` | right |
| `y < dy0 - UP_PX` | up |
| horizontal centred **and** eyelids half shut | down |

Holding a direction for `HOLD_SECONDS` speaks it. Keeping the eyes in that same
zone for another `CONFIRM_SECONDS` speaks `confirm`. Leaving the zone — into
another direction or back to the centre — wipes the slate, so the next visit
starts from zero.

**Blink gestures.** Three non-overlapping closure bands:

| closure | meaning |
|---|---|
| 0.05 – 0.30 s (short) | confirm / supports the blink-pair gestures |
| 0.30 – 0.50 s | dead zone, ignored — the width of an ordinary blink |
| ≥ 0.50 s (long) | three of them toggle the system on or off |

Short blinks come in pairs: two in a row turns fetch mode on (`fetch`), two
more turn it off (`stop`, then `back` one second later).

**Arm hooks.** Every confirmed event calls a method on an `ArmActions`
instance, so the hardware plugs in without touching the gaze logic.

---

## Files

| file | purpose |
|---|---|
| `eye_test.py` | the controller; silent build, everything drawn on the preview |
| `eye_test_verbose.py` | same logic with console prints, for tuning thresholds |
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

> **Power:** five MG996R draw up to 2.5 A each at stall. They must have their own
> 5–6 V supply of at least 5 A, with a common ground to the board. Feeding them
> from the D1 mini's 5 V pin browns out the board and resets it.

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
ready at http://192.168.133.157
http server started
```

Set `USE_STA`, `STA_SSID` and `STA_PASS` at the top of the sketch. If the join
fails it falls back to its own access point, `ESP8266-ARM` / `88888888`,
reachable at `http://192.168.4.1`.

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
```

Calibrate by looking straight ahead when prompted, then hold a direction.

**With the arm:**

```
python arm_wifi.py
python arm_wifi.py --host 192.168.133.157 --step 10
```

`arm_wifi.py` checks the arm before calibrating and reports the address it
found. While the arm is silent it speaks `waiting` every four seconds, and
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

| hook | fires when |
|---|---|
| `on_left` `on_right` `on_up` `on_down` | that direction was confirmed |
| `on_centre` | the eyes came back to rest |
| `on_fetch` / `on_stop` / `on_back` | fetch mode changes |
| `on_system_on` / `on_system_off` | triple long blink toggled power |

---

## Tuning

All thresholds are command-line flags; `eye_test.py --help` lists them.

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

Saved frames are the raw camera image; the debug markers are drawn on screen
only and never written into the files. `saved_frames\` is gitignored.

---

## Status

Working: 4-direction gaze, confirm, blink gestures, power toggle, WiFi arm
control over HTTP, arm hooks.

Open: mapping up/down/back onto the remaining joints, and freeing a PWM pin for
the third vertical servo.
