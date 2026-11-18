# assisArm — System Design Summary

**For:** presentation material and hand-over
**Status:** working end to end, tested on the physical rig
**Repo:** `github.com/thomaslaiw/assisArm` (private)

---

## 1. What the system does

A robotic arm controlled entirely by the eyes. Built for people with severe
paralysis — ALS, locked-in syndrome, high-level spinal injury — for whom a
joystick or a button is impossible, but eye movement is intact.

The user looks in a direction to select it, keeps looking to confirm, and
blinks to start or stop an action. Everything is spoken back so the user knows
what the system understood.

```
        [webcam]                [WiFi]
           │                       │
      eye_test.py  ──────────  arm_wifi.py
      gaze + blinks            HTTP client
           │                       │
           │                       │
      spoken feedback        ESP8266 (D1 mini)
                                   │
                            5 × MG996R servos
```

**No cloud. No camera frame leaves the machine.** Every frame is processed
locally with MediaPipe.

---

## 2. Why the eyes

Choosing an input channel was the first design decision.

| channel | problem for the target user |
|---|---|
| hand / joystick | needs residual motor control |
| voice | needs breath and articulation; often the first thing lost |
| head movement | needs neck control; tiring over hours |
| sip-and-puff | needs a tube in the mouth; uncomfortable, hard to keep clean |
| **eye gaze** | **intact in almost all of these conditions; no wearable, no contact** |

Gaze has two weaknesses, and the design answers both:

1. **The eye is not a pointing device.** It jitters constantly (microsaccades)
   and moves while the person is only looking, not intending. → require a
   *sustained hold* before anything is accepted.
2. **It is the only channel, so accidental activation is expensive.** → split
   commands into two stages: select, then confirm.

Blinks are added because they give a second, independent channel that costs no
extra hardware.

---

## 3. The gaze model

### 3.1 Points tracked

Five points are read from MediaPipe Face Mesh each frame:

```
a = the user's left pupil        (iris landmarks 473-477)
b = the user's right pupil       (iris landmarks 468-472)
c = the nose tip                 (landmark 1)
d = midpoint of a and b
e = midpoint of both upper eyelids   (diagnostic only)
```

### 3.2 The nose is the reference, not the frame

The first working design measured the pupils against the image frame. It failed
the moment the user shifted in their seat: the whole head moved, every value
moved with it, and the calibration was worthless within minutes.

Measuring `d` relative to `c` fixes that. Both points sit on the head, so when
the head moves they move together and the difference survives:

```
dx = d.x - c.x
dy = d.y - c.y
```

Calibration records the resting values `dx0, dy0`. Everything afterwards is a
comparison against them.

**This was the single most important correction in the project.** It turned a
demo that worked only while the user sat perfectly still into something usable
across a session.

### 3.3 Two builds, same behaviour

| build | measurement | why it exists |
|---|---|---|
| `eye_test.py` | pixels of travel from the nose | stable at 720p, what the arm currently runs |
| `eye_test_ratio.py` | iris position as a fraction of the eye opening | for higher resolution cameras; scale-free |

The ratio build was written when the camera moved to 1080p. By measuring the
iris inside its own eyelid box, the numbers stop depending on how far away the
user is sitting:

```
h_ratio = (iris_x - corner_a_x) / (corner_b_x - corner_a_x)    0 = nose side
                                                                1 = ear side
```

Both eyes are inverted so a look to the user's left raises the average. In
practice the pixel build is still the one in use — the ratio build needs its
thresholds retuned against the live arm before it can replace it.

---

## 4. Interaction design

### 4.1 Direction: hold, speak, hold, confirm

```
look left, hold 1 s        → speaker says "left"
keep holding 1 s longer    → speaker says "confirm"   → on_left() fires
leave the zone at any point → nothing happens, the visit never counted
```

Two features matter here:

- **The zone is forgotten on exit.** Leaving a direction — to another direction
  or back to centre — wipes the state. Looking left again starts from zero and
  speaks "left" again. Nothing is remembered between visits, so the user never
  gets a stale confirmation they did not ask for.
- **No lock-out.** An earlier design remembered which direction had been
  confirmed and refused to speak it again until reset. That was wrong: it made
  the arm one-shot. The current rule is simply that a visit either completes or
  is discarded.

### 4.2 Blinks: three non-overlapping bands

| closure length | meaning |
|---|---|
| short (0.05 – 0.30 s) | counts as a blink; two in a row start a fetch |
| **0.30 – 0.50 s** | **dead zone — ignored** |
| long (≥ 0.50 s) | counted; three of them toggle power |

The exact short band has been moved around during tuning; `eye_test.py` ships
with 0.05–0.30 and the `--short-min` / `--short-max` flags change it without
touching the code.

The dead zone is deliberate. An ordinary involuntary blink is roughly 0.1–0.15
seconds. Without a gap, every natural blink would trigger something. Widening
the gap between "short" and "long" makes the mid-band a place where nothing can
happen by accident.

### 4.3 Fetch and stop: asymmetric on purpose

This is the most important safety decision in the design.

```
fetch  ← two short blinks   deliberate action, must be intentional
stop   ← a single eye closure, any length, instantly
```

The reasoning:

```
a false stop   costs nothing      the arm just waits for the next command
a missed stop  costs an accident  the arm keeps moving when it should not
```

So the two are tuned in opposite directions. Starting a fetch needs two
deliberate blinks. Stopping needs only that the eyes close — including a
natural blink. Over-sensitivity is the correct failure mode for a stop.

**A one second grace period follows the fetch.** The fetch is started *by* a
blink, so at the instant fetch begins the eye is still shut; without the grace
that same closure would stop the fetch it had just started. The grace covers
only the opening blink, then the closure rule is immediate again.

While fetching, every other gesture is suppressed — no power toggle, no blink
pairs. One channel, one meaning.

### 4.4 Power

Three long blinks toggle the system. Off parks the arm at home. On does not move
anything, because the user may not be looking at the arm yet.

---

## 5. The arm

### 5.1 Hardware

**Controller:** LOLIN(WEMOS) D1 mini, ESP8266, 4 MB flash, CH340 USB-serial.

**Actuators:** 5 × MG996R (metal gears, ~2.5 A each at stall).

| joint | GPIO | D pin | role |
|---|---|---|---|
| stand | 12 | D6 | base rotation |
| arm1 | 4 | D2 | vertical, lowest link |
| arm2 | 0 | D3 | vertical, middle link |
| arm3 | 14 | D5 | vertical, upper link |
| hand | 5 | D1 | gripper |

Two hardware lessons worth passing on:

- **GPIO16 must not carry a servo.** On the ESP8266 that pin has no hardware
  PWM; attaching a servo to it crashed the board before `setup()` completed,
  leaving it silent on serial with no error. The original wiring used it. It is
  now unused.
- **Five MG996R cannot be powered from the board.** They need their own 5–6 V
  supply of at least 5 A, with a common ground to the board. Running them from
  the D1 mini's 5 V pin browns the board out and resets it mid-motion.

### 5.2 Pose, not raw angles

The user does not think in servo degrees. The arm has two conceptual controls:

```
f = how folded the arm is    0 = fully extended, 40 = folded in
v = how low the arm sits     0 = highest, 30 = lowest
```

and the servo angles follow:

```
arm3 = 90 - f
arm2 = 90 - (f + 8 + v)
arm1 = 90 + 90 - v
```

This was a deliberate choice. Driving servos individually meant every command
had to know the geometry, and the joints drifted out of agreement. With f and v
living **on the board**, the web page, the eye tracker and a curl command all
see the same arm, and the relationship between the joints is enforced in one
place.

The offsets (`8`, the `90 ±` terms) came from measuring the physical rig, not
from theory. They encode where the horn sits on each servo and how the links are
mounted.

### 5.3 Limits

Every command clamps. Asking to extend past `f = 0` or raise past `v = 0` simply
produces no movement — the servo does not hunt against its stop.

---

## 6. Networking

The board joins a phone hotspot as a WiFi station, so the PC keeps its own
network while running the eye tracker.

**The hotspot hands out a different subnet every restart.** This broke
hard-coded addresses repeatedly, so discovery is now automatic: `arm_launch.py`
reads which /24 the PC is on, sweeps it for a board answering `/status`, and
passes the address to whatever it starts.

A note for whoever picks this up: the first sweeper used 96 parallel sockets and
**missed a live board**, because Windows dropped part of the burst. It now uses
32 workers with a longer timeout and sweeps twice. If discovery ever fails while
a direct request to the known address still works, that is the cause.

The board also falls back to its own access point (`ESP8266-ARM` / `88888888`)
if it cannot join, so it is never unreachable.

---

## 7. Software layout

| file | lines | role |
|---|---|---|
| `eye_test.py` | 848 | pixel tracker: gaze, blinks, confirm, fetch, hooks |
| `eye_test_ratio.py` | 748 | ratio tracker, same behaviour |
| `eye_test_verbose.py` | — | pixel tracker with console prints, for tuning |
| `arm_wifi.py` | 220 | turns hooks into HTTP calls |
| `arm_web.py` | 221 | local web remote with direction buttons |
| `arm_launch.py` | 138 | finds the board, starts one of the above |
| `firmware/assisArm/assisArm.ino` | 384 | ESP8266 firmware |
| `run_arm.bat` `run_web.bat` `find_arm.bat` | — | double-click shortcuts |

### The hook interface

Hardware plugs in without touching the gaze logic. Every event calls a method:

| hook | fires when | wired on the arm |
|---|---|---|
| `on_left` `on_right` | direction confirmed | base rotates |
| `on_up` `on_down` | direction confirmed | pose v −/+ 5 |
| `on_centre` | eyes returned to rest | nothing |
| `on_fetch` | fetch started | claw opens, reaches out every 0.3 s |
| `on_stop` | eyes closed during fetch | reach halts, claw closes |
| `on_back` | 1 s after stop | not yet assigned |
| `on_system_on` / `on_system_off` | triple blink | off parks the arm |
| `on_shutdown` | program closing, any route | parks the arm |

`on_back` is deliberately empty. What the arm should physically do on "back" is
still an open question — see section 10.

---

## 8. Speech

All feedback is spoken through the Windows `System.Speech` engine, launched as a
short-lived PowerShell process.

`pyttsx3` was tried first and rejected: it reported success and produced no
sound, which is a known Windows SAPI problem. `System.Speech` worked first time
and needs no package.

While the speaker is talking, gaze logic is suppressed, so the system never
interprets the user's reaction to a spoken word as a new command.

---

## 9. What was built and verified

**Working, tested on the hardware:**

- four-direction gaze with hold-to-select and confirm
- three-band blink detection with a dead zone
- three long blinks toggle power
- fetch: continuous reach at 5 units every 0.3 s, halting at the limit
- stop: a single eye closure, immediate, with the opening-blink grace
- pose-based arm control with clamped limits
- claw open/close
- web remote with direction buttons
- automatic board discovery
- park on exit and on power-off
- spoken "waiting" every 2 s when the arm stops answering

**Measured on the rig:**

```
fetch from f=40 to f=0      ≈ 2.4 s, halts cleanly
raise while already at top  no movement, no servo hum
stop mid-fetch              pose frozen at the instant of closure
```

---

## 10. Open work — where the next member should start

**1. `back` has no physical meaning yet.**
The gesture fires, nothing happens. It needs a decision: retract the reach?
return to a neutral hold? Until that exists, `on_back` is a stub.

**2. The ratio tracker is not in service.**
`eye_test_ratio.py` is finished and scale-free but its thresholds were tuned
against a synthetic rig, not a face. It needs a live tuning session
(`--help` lists every flag) before it can replace the pixel build.

**3. arm3's role is undecided.**
It currently just follows `f`. It could instead hold the gripper level as the
arm extends — a real usability improvement, and the geometry is already there.

**4. Per-user calibration is manual.**
Each session begins with a 3-second "look straight ahead". A saved profile per
user would remove that, and `--dx0 --dy0` already supports loading one.

**5. No quantitative evaluation.**
There are no numbers on time-to-target, error rate, or false-trigger rate. Any
write-up or demonstration needs them, and they are straightforward to collect:
log every spoken word with a timestamp and count from that.

**6. GPIO16 and the third vertical link.**
If arm3 ever needs independent hardware PWM, it must move to D6 (GPIO12) or D7
(GPIO13). GPIO16 will not work.

---

## 11. Files a new member should read first

1. `eye_test.py` — the state machine; read the module docstring, then `main()`
2. `firmware/assisArm/assisArm.ino` — the pose model and the API
3. `arm_wifi.py` — how events become motion
4. `README.md` — wiring, flashing, every flag

---

## 12. Summary in one paragraph

A gaze-and-blink interface that drives a five-servo arm, built so that a person
who can only move their eyes can still operate it. Direction is held and
confirmed; blinks start actions; a single eye closure stops everything, because
over-sensitivity is the safe failure for a stop and under-sensitivity is the
safe failure for a start. The arm is driven by a two-number pose rather than raw
servo angles, so the joints stay coordinated no matter which interface issued
the command. Everything runs locally, and the whole thing is one `run_arm.bat`
away from starting.
