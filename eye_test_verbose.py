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

Usage:
    python eye_test.py                      # calibrate then run
    python eye_test.py --dx0 -232 --dy0 -38 # reuse a previous centre
    python eye_test.py --flip-y             # invert up/down if your camera needs it

Keys:
    ENTER saves the raw frame (no markers) and prints the numbers
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
    print(f"\n[tts] {text}")


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


def draw_debug(frame, m, centre, mode, state_msg):
    """Draw a, b, c, d, the resting centre and the decision bands."""
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
            print(f"\nresting offset d-c: dx0={self.dx0:+.1f}  dy0={self.dy0:+.1f} "
                  f"({len(self.samples)} frames)")

        return "RECORDING CENTRE", min(1.0, progress)

    def done(self):
        return self.dx0 is not None and self.dy0 is not None


def hold_bar(held, total, width=20):
    n = int(width * min(1.0, held / total))
    return "[" + "#" * n + "-" * (width - n) + "]"


def save_frame(frame):
    try:
        os.makedirs(FRAME_DIR, exist_ok=True)
        name = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}.png"
        path = os.path.join(FRAME_DIR, name)
        return path if cv2.imwrite(path, frame) else None
    except OSError as e:
        print(f"[frame] cannot write to {FRAME_DIR}: {e}")
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
    p.add_argument("--debug", action="store_true", help="draw a, b, c, d and the bands")
    return p.parse_args()


def main():
    args = parse_args()

    fixed = None
    if not args.calib and args.dx0 is not None and args.dy0 is not None:
        fixed = (args.dx0, args.dy0)

    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cap.isOpened():
        print("ERROR: cannot open webcam. Close any app using the camera and retry.")
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, VIEW_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, VIEW_H)

    WIN = "Eye test - Q/ESC quit, ENTER save frame"
    show_preview = not args.no_preview
    if show_preview:
        cv2.namedWindow(WIN, cv2.WINDOW_AUTOSIZE)
        cv2.moveWindow(WIN, 0, 0)

    print("=" * 78)
    print("Eye tracking. HOR / VER mode switch. Local only.")
    print(f"  HOR: {args.left_px:.0f}px left  -> left   |  {args.right_px:.0f}px right -> right")
    print(f"       back within {args.center_px:.0f}px for {SWITCH_SECONDS:.0f}s -> switch to VER")
    print(f"  VER: {args.up_px:.0f}px up    -> up     |  {args.down_px:.0f}px down  -> down")
    print(f"       back within {args.center_px:.0f}px for {SWITCH_SECONDS:.0f}s -> switch to HOR")
    print(f"  each action needs {HOLD_SECONDS:.0f}s hold, then {COOLDOWN_SECONDS:.0f}s cooldown")
    print(f"  ENTER saves the raw frame to {FRAME_DIR}")
    print("  Q / ESC / window X to quit.")
    if args.flip_y:
        print("  --flip-y active: up and down are swapped")
    if args.debug:
        print("  debug overlay ON (preview only)")
    print("=" * 78)

    cal = RestCalibration()
    if fixed:
        cal.dx0, cal.dy0 = fixed
        print(f"resting offset: dx0={cal.dx0:+.1f} dy0={cal.dy0:+.1f}")

    mode = "HOR"
    calib_announced = False
    last_line = ""

    # timers
    trig_dir = None
    trig_start = 0.0
    switch_start = None
    cooldown_until = 0.0

    G = {"m": None, "mode": mode, "state": ""}

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("ERROR: lost camera frame.")
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
                detail = (f"c=({m['c'][0]:6.1f},{m['c'][1]:6.1f}) "
                          f"d=({m['d'][0]:6.1f},{m['d'][1]:6.1f}) "
                          f"d-c=({m['dx']:+6.1f},{m['dy']:+6.1f})") if m else "no face"
                if progress < 0:
                    line = f"{label:<38} {detail}"
                else:
                    line = (f"{label} {hold_bar(progress * CALIB_SECONDS, CALIB_SECONDS)} "
                            f"{progress * 100:3.0f}%  {detail}")
                if line != last_line:
                    print("\r" + line + " " * 6, end="", flush=True)
                    last_line = line
                G.update(m=m, state="calibrating")

            elif m is None:
                # no face: reset timers, keep the mode
                trig_dir, trig_start, switch_start = None, 0.0, None
                line = "no face"
                if line != last_line:
                    print("\r" + line + " " * 80, end="", flush=True)
                    last_line = line

            else:
                if not calib_announced:
                    print("\r" + " " * 100, end="\r")
                    print("Tracking now. Mode: HOR")
                    speak("ready")
                    calib_announced = True
                    last_line = ""

                x_err = m["dx"] - cal.dx0          # + means d is left of rest
                y_err = m["dy"] - cal.dy0          # - means d is above rest
                if args.flip_y:
                    y_err = -y_err

                spoken = None
                action = "-"

                if cooling:
                    # ignore everything, keep timers frozen
                    action = f"cooldown {cooldown_until - now:.1f}s"
                    trig_dir, trig_start, switch_start = None, 0.0, None

                elif mode == "HOR":
                    # horizontal is checked first
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

                line = (f"{mode}  c=({m['c'][0]:5.0f},{m['c'][1]:5.0f}) "
                        f"d=({m['d'][0]:5.0f},{m['d'][1]:5.0f}) "
                        f"err=({x_err:+6.1f},{y_err:+6.1f}) "
                        f"{trig_dir or '-':<5} {hold_bar(held, HOLD_SECONDS)} "
                        f"sw {hold_bar(sw_held, SWITCH_SECONDS)} {action}")

                if line != last_line:
                    print("\r" + line + " " * 8, end="", flush=True)
                    last_line = line

                G.update(m=m, mode=mode, state=action)

            # -------------------------------------------- window
            if show_preview:
                display = frame.copy()
                if args.debug and m is not None:
                    centre = (cal.dx0, cal.dy0) if cal.done() else None
                    draw_debug(display, m, centre, mode, G["state"])

                cv2.imshow(WIN, display)
                key = cv2.waitKey(1) & 0xFF

                if key in (ord("q"), ord("Q"), 27):
                    break

                if key in (13, 10):
                    path = save_frame(frame)
                    print("\n" + "-" * 78)
                    print(f"[capture] image : {path if path else 'FAILED to save'}")
                    if G["m"] is not None:
                        mm = G["m"]
                        print(f"[capture] a left pupil : ({mm['a'][0]:.1f}, {mm['a'][1]:.1f})")
                        print(f"[capture] b right pupil: ({mm['b'][0]:.1f}, {mm['b'][1]:.1f})")
                        print(f"[capture] c nose       : ({mm['c'][0]:.1f}, {mm['c'][1]:.1f})")
                        print(f"[capture] d midpoint   : ({mm['d'][0]:.1f}, {mm['d'][1]:.1f})")
                        print(f"[capture] d - c        : ({mm['dx']:+.1f}, {mm['dy']:+.1f})")
                    if cal.done():
                        print(f"[capture] resting      : dx0={cal.dx0:+.1f} dy0={cal.dy0:+.1f}")
                    print(f"[capture] thresholds   : left {args.left_px:.0f} right {args.right_px:.0f} "
                          f"up {args.up_px:.0f} down {args.down_px:.0f} centre {args.center_px:.0f}")
                    print(f"[capture] mode         : {G['mode']}")
                    print(f"[capture] last action  : {G['state']}")
                    print("-" * 78)

                if cv2.getWindowProperty(WIN, cv2.WND_PROP_VISIBLE) < 1:
                    break

    except KeyboardInterrupt:
        print("\nStopped by user.")

    finally:
        cap.release()
        if show_preview:
            cv2.destroyAllWindows()
        print()


if __name__ == "__main__":
    main()
