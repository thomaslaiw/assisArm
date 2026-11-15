# assisArm — eye-tracking assistive robotic arm

Gaze-controlled robotic arm for people with severe paralysis. This repo currently
holds the **eye-tracking input layer** only; the arm control comes later.

## What this is

`eye_test.py` reads the eyes from a webcam and turns gaze into five states:

```
left   right   up   down   centre
```

and the four diagonals that fall out of combining them
(`left up`, `right down`, …).

Everything runs **locally**. No frame, image or landmark ever leaves the machine.

## Requirements

- Python 3.11
- Packages:
  ```
  pip install "mediapipe==0.10.14" "opencv-python==4.11.0.86" "numpy==1.26.4"
  ```

MediaPipe 1.x removed the `mp.solutions` API, so pinning 0.10.14 matters.
Speech uses the Windows `System.Speech` engine through a short-lived PowerShell
process — no extra package, no network.

## How detection works

1. MediaPipe Face Mesh with `refine_landmarks=True` gives iris centres.
2. The image is mirrored first, so the person sees themselves naturally.
3. Four points are tracked every frame:

   ```
   a = my left pupil      b = my right pupil
   c = nose tip           d = midpoint of a and b
   ```

4. Calibration records the resting offset of `d` from `c`:

   ```
   dx0 = d.x - c.x        dy0 = d.y - c.y
   ```

   Because `c` is the nose, the offsets survive the head moving — the nose
   shifts with the pupils, so the difference stays put.

5. A **mode switch** decides which axis is live:

   ```
   HOR (default)
     d.x - c.x > dx0 + 10  held 1s -> speak "left",  cooldown 1s
     d.x - c.x < dx0 - 10  held 1s -> speak "right", cooldown 1s
     |d.x - c.x - dx0| <= 10 held 1s -> switch to VER

   VER
     d.y - c.y - dy0 < -10 held 1s -> speak "up",    cooldown 1s
     d.y - c.y - dy0 > +5  held 1s -> speak "down",  cooldown 1s
     |d.y - c.y - dy0| <= 10 held 1s -> switch to HOR
   ```

   Only the active axis is evaluated, so a diagonal glance cannot fire both
   directions at once. Returning the eyes to the centre band for one second
   hands control to the other axis.

## Files

| file | purpose |
|---|---|
| `eye_test.py` | silent build — **no console output at all**; everything is drawn on the preview |
| `eye_test_verbose.py` | same logic with full console prints; use this when tuning thresholds |

## Usage

Calibrate (look straight ahead when prompted) — with the silent build everything
appears in the preview window:

```
python eye_test.py
```

The verbose build prints the resting offset:

```
resting offset d-c: dx0=-232.5  dy0=-38.2 (90 frames)
```

Reuse those numbers so calibration is skipped:

```
python eye_test.py --dx0 -232.5 --dy0 -38.2
```

| flag | meaning |
|---|---|
| `--dx0 --dy0` | resting offset of the pupil midpoint from the nose |
| `--left-px` | travel needed to count as left (default 10) |
| `--right-px` | travel needed to count as right (default 10) |
| `--up-px` | travel needed to count as up (default 10) |
| `--down-px` | travel needed to count as down (default 5) |
| `--center-px` | band that hands control to the other axis (default 10) |
| `--flip-y` | invert up/down if the camera orientation needs it |
| `--plain` | raw camera image only, no overlay at all |
| `--no-preview` | run headless (silent build: no output whatsoever) |
| `--calib` | force calibration even when offsets are given |

Keys while running:

| key | action |
|---|---|
| `ENTER` | save the raw frame to `saved_frames\` and print that frame's numbers |
| `Q` / `ESC` | quit |

Saved frames are the raw camera image — debug markers are drawn on screen only
and never written into the files.

## Calibration notes

- Sit about 40–50 cm from the camera so the face fills roughly a third of the frame.
- Keep the head straight; a tilted head shifts the two eyes vertically and
  corrupts the up/down reading.
- Recalibrate whenever the seating position, distance or lighting changes.
- `--dx` up for less sensitivity, down for more.

## Roadmap

- [x] 5-state gaze detection with speech
- [ ] Dwell selection on physical objects (scene camera)
- [ ] Map gaze to arm coordinates (hand-eye calibration)
- [ ] Arm control: open / move / grip / release / return home
- [ ] Voice + gaze confirmation before any motion
