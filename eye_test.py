"""
Eye tracking with a mode switch between horizontal and vertical.

Points:
    a = my left pupil        b = my right pupil
    c = nose tip             d = 2D midpoint of a and b

Calibration records the resting offset of d relative to c:
    dx0 = d.x - c.x          dy0 = d.y - c.y

HOR mode (default)
    d.x - c.x > dx0 + 10  held 1s -> speak "left",  cooldown 1s, stay in HOR
    d.x - c.x < dx0 - 10  held 1s -> speak "right", cooldown 1s, stay in HOR
    |d.x - c.x - dx0| <= 10 held 1s -> switch to VER

VER mode
    d.y - c.y - dy0 < -10 held 1s -> speak "up",    cooldown 1s
    d.y - c.y - dy0 > +5  held 1s -> speak "down",  cooldown 1s
    |d.y - c.y - dy0| <= 10 held 1s -> switch to HOR

The active mode is checked first, so HOR looks at x before y and vice versa.
Cooldown blocks all detection for 1 second after any utterance.

This build prints nothing to the console: everything is drawn on the preview.
Use eye_test_verbose.py when the numbers are needed for tuning.

Usage:
    python eye_test.py                      # calibrate then run
    python eye_test.py --dx0 -232 --dy0 -38 # reuse a previous centre
    python eye_test.py --flip-y             # invert up/down if your camera needs it

Keys:
    ENTER saves the raw frame (no markers)
    Q / ESC quits

Everything runs locally. No image leaves this machine.
"""

import argparse
import os
import subprocess
import time

import cv2
import mediapipe as mp

HOLD_SECONDS = 1.0
COOLDOWN_SECONDS = 1.0
SWITCH_SECONDS = 1.0
CALIB_WAIT_SECONDS = 2.0
CALIB_SECONDS = 3.0

LEFT_PX = 10.0        # d must be this far left of the resting x
RIGHT_PX = 10.0       # ...or this far right
UP_PX = 10.0          # d must rise this much
DOWN_PX = 5.0         # d must fall this much
CENTER_PX = 10.0      # back within this band switches mode

VIEW_W, VIEW_H = 1280, 720
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
FRAME_DIR = os.path.join(PROJECT_DIR, "saved_frames")

# ---------------------------------------------------------------- speech
_PS_SPEAK = (
    "Add-Type -AssemblyName System.Speech; "
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "$s.Rate = 0; "
    "$s.Speak('{text}')"
)


def speak(text):
    safe = text.replace("'", "''")
    subprocess.Popen(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
         _PS_SPEAK.format(text=safe)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------- landmarks
mp_face_mesh = mp.solutions.face_mesh
face_mesh = mp_face_mesh.FaceMesh(
    max_num_faces=1,
    refine_landmarks=True,
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5,
)

# mirrored image: 468s are on the viewer's left = YOUR right eye
MY_RIGHT_EYE_IRIS = [468, 469, 470, 471, 472]
MY_LEFT_EYE_IRIS = [473, 474, 475, 476, 477]
NOSE_TIP = 1


def measure(landmarks, w, h):
    """a, b, c, d in pixels plus the offsets we care about."""
    def centre(ids):
        return (sum(landmarks[i].x for i in ids) / len(ids) * w,
                sum(landmarks[i].y for i in ids) / len(ids) * h)

    a = centre(MY_LEFT_EYE_IRIS)       # my left pupil
    b = centre(MY_RIGHT_EYE_IRIS)      # my right pupil
    c = (landmarks[NOSE_TIP].x * w, landmarks[NOSE_TIP].y * h)
    d = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)

    return {"a": a, "b": b, "c": c, "d": d,
            "dx": d[0] - c[0], "dy": d[1] - c[1]}


def draw_debug(frame, m, centre, mode, state_msg, calib_label, calib_progress):
    """Everything the console used to say, drawn on the frame instead."""
    h, w = frame.shape[:2]

    for key, colour, label in (("a", (0, 255, 0), "a"),
                               ("b", (0, 0, 255), "b"),
                               ("c", (255, 0, 255), "c"),
                               ("d", (255, 255, 0), "d")):
        x, y = int(m[key][0]), int(m[key][1])
        cv2.drawMarker(frame, (x, y), colour, cv2.MARKER_CROSS, 16, 2)
        cv2.putText(frame, label, (x + 6, y - 6), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, colour, 2)

    cv2.line(frame, (int(m["c"][0]), int(m["c"][1])),
             (int(m["d"][0]), int(m["d"][1])), (150, 150, 150), 1)
    cv2.line(frame, (int(m["a"][0]), int(m["a"][1])),
             (int(m["b"][0]), int(m["b"][1])), (90, 90, 90), 1)

    if centre is not None:
        dx0, dy0 = centre
        cx, cy = int(m["c"][0] + dx0), int(m["c"][1] + dy0)
        cv2.drawMarker(frame, (cx, cy), (255, 0, 255), cv2.MARKER_DIAMOND, 18, 2)
        for xx in (int(cx - LEFT_PX), int(cx + RIGHT_PX)):
            cv2.line(frame, (xx, 0), (xx, h), (80, 80, 200), 1)
        for yy in (int(cy - UP_PX), int(cy + DOWN_PX)):
            cv2.line(frame, (0, yy), (w, yy), (200, 160, 80), 1)

    cv2.putText(frame, f"mode {mode}", (12, 28),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                (0, 255, 0) if mode == "HOR" else (0, 200, 255), 2)
    if state_msg:
        cv2.putText(frame, state_msg, (12, 58),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)


def draw_numbers(frame, m, centre, x_err, y_err, trig_dir, held, sw_held, action):
    """Live readout block, bottom-left, replacing the console status line."""
    h, w = frame.shape[:2]
    lines = []

    if m is not None:
        lines.append(f"c=({m['c'][0]:.0f},{m['c'][1]:.0f})  d=({m['d'][0]:.0f},{m['d'][1]:.0f})")
        lines.append(f"d-c=({m['dx']:+.1f},{m['dy']:+.1f})")
        if centre is not None:
            lines.append(f"err=({x_err:+.1f},{y_err:+.1f})")
    else:
        lines.append("no face")

    lines.append(f"trig={trig_dir or '-'}  held={held:.1f}s  sw={sw_held:.1f}s")
    if action and action != "-":
        lines.append(action)

    y0 = h - 16 - (len(lines) - 1) * 20
    for i, text in enumerate(lines):
        cv2.putText(frame, text, (12, y0 + i * 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)


def draw_calibration(frame, label, progress, m):
    """Calibration prompt and progress, drawn instead of printed."""
    h, w = frame.shape[:2]

    cv2.rectangle(frame, (0, 0), (w, 90), (40, 40, 40), -1)
    cv2.putText(frame, label, (16, 40), cv2.FONT_HERSHEY_SIMPLEX,
                1.0, (0, 255, 255), 2)

    bar_w = int((w - 32) * max(0.0, min(1.0, progress)))
    cv2.rectangle(frame, (16, 56), (w - 16, 76), (70, 70, 70), -1)
    if progress > 0:
        cv2.rectangle(frame, (16, 56), (16 + bar_w, 76), (0, 255, 0), -1)

    if m is not None:
        cv2.putText(frame,
                    f"c=({m['c'][0]:.0f},{m['c'][1]:.0f}) d=({m['d'][0]:.0f},{m['d'][1]:.0f})",
                    (16, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)


# ---------------------------------------------------------------- calibration
class RestCalibration:
    """Records the resting offset of d relative to c."""

    def __init__(self):
        self.prompt_at = None
        self.record_start = None
        self.samples = []
        self.dx0 = None
        self.dy0 = None

    def needs_prompt(self):
        return self.prompt_at is None

    def do_prompt(self, now):
        speak("Please look straight ahead")
        self.prompt_at = now

    def feed(self, m, now):
        waited = now - self.prompt_at
        if waited < CALIB_WAIT_SECONDS:
            return f"LOOK CENTRE - starting in {CALIB_WAIT_SECONDS - waited:.1f}s", -1.0

        if self.record_start is None:
            self.record_start = now
        progress = (now - self.record_start) / CALIB_SECONDS

        if m is not None:
            self.samples.append((m["dx"], m["dy"]))

        if progress >= 1.0 and self.samples:
            xs = sorted(p[0] for p in self.samples)
            ys = sorted(p[1] for p in self.samples)
            self.dx0 = xs[len(xs) // 2]
            self.dy0 = ys[len(ys) // 2]

        return "RECORDING CENTRE", min(1.0, progress)

    def done(self):
        return self.dx0 is not None and self.dy0 is not None


def save_frame(frame):
    try:
        os.makedirs(FRAME_DIR, exist_ok=True)
        name = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}.png"
        path = os.path.join(FRAME_DIR, name)
        return path if cv2.imwrite(path, frame) else None
    except OSError:
        return None


def parse_args():
    p = argparse.ArgumentParser(description="Eye tracking, HOR/VER mode switch")
    p.add_argument("--dx0", type=float, default=None, help="resting d.x - c.x")
    p.add_argument("--dy0", type=float, default=None, help="resting d.y - c.y")
    p.add_argument("--left-px", type=float, default=LEFT_PX)
    p.add_argument("--right-px", type=float, default=RIGHT_PX)
    p.add_argument("--up-px", type=float, default=UP_PX)
    p.add_argument("--down-px", type=float, default=DOWN_PX)
    p.add_argument("--center-px", type=float, default=CENTER_PX)
    p.add_argument("--flip-y", action="store_true", help="invert the up/down test")
    p.add_argument("--calib", action="store_true", help="calibrate even if dx0/dy0 given")
    p.add_argument("--no-preview", action="store_true", help="run headless")
    p.add_argument("--plain", action="store_true",
                   help="raw camera image only, no overlay at all")
    return p.parse_args()


def main():
    args = parse_args()

    fixed = None
    if not args.calib and args.dx0 is not None and args.dy0 is not None:
        fixed = (args.dx0, args.dy0)

    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cap.isOpened():
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, VIEW_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, VIEW_H)

    WIN = "Eye tracking"
    show_preview = not args.no_preview
    if show_preview:
        cv2.namedWindow(WIN, cv2.WINDOW_AUTOSIZE)
        cv2.moveWindow(WIN, 0, 0)

    cal = RestCalibration()
    if fixed:
        cal.dx0, cal.dy0 = fixed

    mode = "HOR"
    calib_announced = False

    trig_dir = None
    trig_start = 0.0
    switch_start = None
    cooldown_until = 0.0

    m = None
    action = "-"
    x_err = y_err = 0.0
    held = sw_held = 0.0

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break

            frame = cv2.flip(frame, 1)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result = face_mesh.process(rgb)

            m = None
            if result.multi_face_landmarks:
                lm = result.multi_face_landmarks[0].landmark
                m = measure(lm, frame.shape[1], frame.shape[0])

            now = time.time()
            cooling = now < cooldown_until

            # -------------------------------------------- calibration
            if not cal.done():
                if cal.needs_prompt():
                    cal.do_prompt(now)
                label, progress = cal.feed(m, now)
                if show_preview and not args.plain:
                    draw_calibration(frame, label, max(0.0, progress), m)

            elif m is None:
                trig_dir, trig_start, switch_start = None, 0.0, None
                action = "no face"

            else:
                if not calib_announced:
                    speak("ready")
                    calib_announced = True

                x_err = m["dx"] - cal.dx0
                y_err = m["dy"] - cal.dy0
                if args.flip_y:
                    y_err = -y_err

                spoken = None
                action = "-"

                if cooling:
                    action = f"cooldown {cooldown_until - now:.1f}s"
                    trig_dir, trig_start, switch_start = None, 0.0, None

                elif mode == "HOR":
                    if x_err > args.left_px:
                        want = "left"
                    elif x_err < -args.right_px:
                        want = "right"
                    else:
                        want = None

                    if want:
                        switch_start = None
                        if trig_dir != want:
                            trig_dir, trig_start = want, now
                        elif now - trig_start >= HOLD_SECONDS:
                            spoken = want
                            action = f"say {want}"
                            cooldown_until = now + COOLDOWN_SECONDS
                            trig_dir, trig_start = None, 0.0
                    else:
                        trig_dir, trig_start = None, 0.0
                        if abs(x_err) <= args.center_px:
                            if switch_start is None:
                                switch_start = now
                            elif now - switch_start >= SWITCH_SECONDS:
                                mode = "VER"
                                switch_start = None
                                action = "-> VER"
                        else:
                            switch_start = None

                else:  # VER
                    if y_err < -args.up_px:
                        want = "up"
                    elif y_err > args.down_px:
                        want = "down"
                    else:
                        want = None

                    if want:
                        switch_start = None
                        if trig_dir != want:
                            trig_dir, trig_start = want, now
                        elif now - trig_start >= HOLD_SECONDS:
                            spoken = want
                            action = f"say {want}"
                            cooldown_until = now + COOLDOWN_SECONDS
                            trig_dir, trig_start = None, 0.0
                    else:
                        trig_dir, trig_start = None, 0.0
                        if abs(y_err) <= args.center_px:
                            if switch_start is None:
                                switch_start = now
                            elif now - switch_start >= SWITCH_SECONDS:
                                mode = "HOR"
                                switch_start = None
                                action = "-> HOR"
                        else:
                            switch_start = None

                if spoken:
                    speak(spoken)

                held = (now - trig_start) if trig_dir else 0.0
                sw_held = (now - switch_start) if switch_start else 0.0

            # -------------------------------------------- window
            if show_preview:
                display = frame.copy()
                if not args.plain:
                    if cal.done():
                        if m is not None:
                            draw_debug(display, m, (cal.dx0, cal.dy0), mode, action, None, 0)
                            draw_numbers(display, m, (cal.dx0, cal.dy0),
                                         x_err, y_err, trig_dir, held, sw_held, action)
                        else:
                            cv2.putText(display, "no face", (12, 28),
                                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
                            cv2.putText(display, f"mode {mode}", (12, 58),
                                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 2)

                cv2.imshow(WIN, display)
                key = cv2.waitKey(1) & 0xFF

                if key in (ord("q"), ord("Q"), 27):
                    break

                if key in (13, 10):
                    save_frame(frame)

                if cv2.getWindowProperty(WIN, cv2.WND_PROP_VISIBLE) < 1:
                    break

    except KeyboardInterrupt:
        pass

    finally:
        cap.release()
        if show_preview:
            cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
