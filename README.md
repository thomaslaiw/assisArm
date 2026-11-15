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
3. Each eye is measured **independently** against its own calibrated centre:

   ```
   x > x0 + dx  -> left        x < x0 - dx  -> right
   y < y0 - dy  -> up          y > y0 + dy  -> down
   ```

4. The two eyes are merged per axis:

   | right eye | left eye | result |
   |---|---|---|
   | left | left | left |
   | left | neutral | left |
   | neutral | neutral | centre |
   | left | right | centre (they disagree) |

5. Dwell timing turns a state into speech:

   | held | speaks |
   |---|---|
   | 2 s, one axis | `left` / `right` / `up` / `down` |
   | 3 s, both axes | `left up`, `right down`, … |
   | 5 s | `<state> confirm` |

## Usage

Calibrate (look straight ahead when prompted):

```
python eye_test.py --debug
```

It prints the centre of each eye:

```
centre: my right eye = (700.5, 268.0)
        my left  eye = (470.2, 265.5)
```

Reuse those numbers so calibration is skipped:

```
python eye_test.py --lx0 470.2 --ly0 265.5 --rx0 700.5 --ry0 268.0 --dx 100 --dy 20
```

| flag | meaning |
|---|---|
| `--lx0 --ly0` | centre of your left eye, pixels |
| `--rx0 --ry0` | centre of your right eye, pixels |
| `--dx` | horizontal travel needed to count as left/right (default 100) |
| `--dy` | vertical travel needed to count as up/down (default 20) |
| `--debug` | draw pupils, centres and decision bands on the preview |
| `--no-preview` | run headless |
| `--calib` | force calibration even when centres are given |

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
