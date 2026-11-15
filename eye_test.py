"""
Eye tracking test - per-eye decisions, merged.

Each eye has its own centre point and its own left/right/up/down test:
    x > x0 + dx -> left        x < x0 - dx -> right
    y < y0 - dy -> up          y > y0 + dy -> down

Merge rules:
    both eyes agree            -> that direction
    one eye decides, other is
    in the middle band         -> that direction
    both in the middle         -> centre
    the two eyes disagree      -> centre

Usage:
    python eye_test.py                                     # calibrate both eyes
    python eye_test.py --lx0 470 --ly0 265 --rx0 700 --ry0 268
    python eye_test.py --lx0 470 --ly0 265 --rx0 700 --ry0 268 --dx 100 --dy 20

Speech:
  single axis held 2s -> left/right/up/down
  both axes held 3s   -> combined word
  any state held 5s   -> "<state> confirm"

Keys:
  ENTER saves the raw frame (no markers) and prints the data
  Q/ESC quits

Everything runs locally. No image leaves this machine.
"""

import argparse
import os
import subprocess
import time

import cv2
import mediapipe as mp

HINT_SECONDS = 2.0
COMBINED_SECONDS = 3.0
CONFIRM_SECONDS = 5.0
CALIB_WAIT_SECONDS = 2.0
CALIB_SECONDS = 3.0

DEFAULT_DX = 100
DEFAULT_DY = 20

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
    print(f"[tts] {text}")


# ---------------------------------------------------------------- landmarks
mp_face_mesh = mp.solutions.face_mesh
face_mesh = mp_face_mesh.FaceMesh(
    max_num_faces=1,
    refine_landmarks=True,
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5,
)

# The image is mirrored before display, so 468s sit on the viewer's left,
# which is YOUR RIGHT eye. Names below follow the person's own anatomy.
MY_RIGHT_EYE_IRIS = [468, 469, 470, 471, 472]      # left side of the frame
MY_LEFT_EYE_IRIS = [473, 474, 475, 476, 477]       # right side of the frame


def eye_centres(landmarks, w, h):
    """Return ((rx, ry), (lx, ly)) in pixels, from your own point of view."""
    def centre(ids):
        return (sum(landmarks[i].x for i in ids) / len(ids) * w,
                sum(landmarks[i].y for i in ids) / len(ids) * h)

    r = centre(MY_RIGHT_EYE_IRIS)
    l = centre(MY_LEFT_EYE_IRIS)
    return r, l


def draw_debug(frame, eye_pos, centres, dx, dy):
    """Mark pupils plus per-eye decision bands."""
    h, w = frame.shape[:2]
    (rx, ry), (lx, ly) = eye_pos

    cv2.drawMarker(frame, (int(rx), int(ry)), (0, 0, 255), cv2.MARKER_CROSS, 18, 2)
    cv2.drawMarker(frame, (int(lx), int(ly)), (0, 255, 0), cv2.MARKER_CROSS, 18, 2)

    if centres is None:
        return
    (rx0, ry0), (lx0, ly0) = centres

    for x0, colour in ((rx0, (0, 0, 200)), (lx0, (0, 180, 0))):
        for xx in (x0 - dx, x0 + dx):
            cv2.line(frame, (int(xx), 0), (int(xx), h), colour, 1)
    for y0, colour in ((ry0, (0, 0, 200)), (ly0, (0, 180, 0))):
        for yy in (y0 - dy, y0 + dy):
            cv2.line(frame, (0, int(yy)), (w, int(yy)), colour, 1)

    cv2.drawMarker(frame, (int(rx0), int(ry0)), (255, 0, 255), cv2.MARKER_DIAMOND, 18, 2)
    cv2.drawMarker(frame, (int(lx0), int(ly0)), (255, 0, 255), cv2.MARKER_DIAMOND, 18, 2)


# ---------------------------------------------------------------- calibration
class CentreCalibration:
    """One prompt: look straight ahead. Records the median position of each eye."""

    def __init__(self):
        self.prompt_at = None
        self.record_start = None
        self.samples_r = []
        self.samples_l = []
        self.centre_r = None
        self.centre_l = None

    def needs_prompt(self):
        return self.prompt_at is None

    def do_prompt(self, now):
        speak("Please look straight ahead")
        self.prompt_at = now

    @staticmethod
    def _median(pairs):
        xs = sorted(p[0] for p in pairs)
        ys = sorted(p[1] for p in pairs)
        return xs[len(xs) // 2], ys[len(ys) // 2]

    def feed(self, eye_pos, now):
        waited = now - self.prompt_at
        if waited < CALIB_WAIT_SECONDS:
            return f"LOOK CENTRE - starting in {CALIB_WAIT_SECONDS - waited:.1f}s", -1.0

        if self.record_start is None:
            self.record_start = now
        progress = (now - self.record_start) / CALIB_SECONDS

        if eye_pos is not None:
            self.samples_r.append(eye_pos[0])
            self.samples_l.append(eye_pos[1])

        if progress >= 1.0 and self.samples_r:
            self.centre_r = self._median(self.samples_r)
            self.centre_l = self._median(self.samples_l)
            print(f"\ncentre: my right eye = ({self.centre_r[0]:.1f}, {self.centre_r[1]:.1f})")
            print(f"        my left  eye = ({self.centre_l[0]:.1f}, {self.centre_l[1]:.1f})")

        return "RECORDING CENTRE", min(1.0, progress)

    def done(self):
        return self.centre_r is not None and self.centre_l is not None


def eye_direction(pos, centre, dx, dy):
    """Per-axis direction for one eye: returns (horiz, vert), each '' or a word."""
    if pos is None or centre is None:
        return "", ""
    x, y = pos
    x0, y0 = centre
    horiz = "left" if x > x0 + dx else ("right" if x < x0 - dx else "")
    vert = "up" if y < y0 - dy else ("down" if y > y0 + dy else "")
    return horiz, vert


def merge_axis(a, b):
    """Merge two eyes on ONE axis. Returns '' , the agreed word, or 'conflict'."""
    if a and b:
        return a if a == b else "conflict"
    return a or b


def hold_bar(held, total, width=32):
    n = int(width * min(1.0, held / total))
    return "[" + "#" * n + "-" * (width - n) + "]"


def save_frame(frame):
    try:
        os.makedirs(FRAME_DIR, exist_ok=True)
        name = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}.png"
        path = os.path.join(FRAME_DIR, name)
        return path if cv2.imwrite(path, frame) else None
    except OSError as e:
        print(f"\n[frame] cannot write to {FRAME_DIR}: {e}")
        return None


def parse_args():
    p = argparse.ArgumentParser(description="Eye tracking, per-eye decisions")
    p.add_argument("--lx0", type=float, default=None, help="my LEFT eye centre x")
    p.add_argument("--ly0", type=float, default=None, help="my LEFT eye centre y")
    p.add_argument("--rx0", type=float, default=None, help="my RIGHT eye centre x")
    p.add_argument("--ry0", type=float, default=None, help="my RIGHT eye centre y")
    p.add_argument("--dx", type=float, default=DEFAULT_DX, help="horizontal offset")
    p.add_argument("--dy", type=float, default=DEFAULT_DY, help="vertical offset")
    p.add_argument("--calib", action="store_true", help="calibrate even if centres are given")
    p.add_argument("--no-preview", action="store_true", help="do not open the camera window")
    p.add_argument("--debug", action="store_true", help="draw pupils and bands")
    return p.parse_args()


def main():
    args = parse_args()

    fixed = None
    if not args.calib and None not in (args.lx0, args.ly0, args.rx0, args.ry0):
        fixed = ((args.rx0, args.ry0), (args.lx0, args.ly0))
    elif not args.calib and any(v is not None for v in (args.lx0, args.ly0, args.rx0, args.ry0)):
        print("ERROR: give ALL FOUR centres (--lx0 --ly0 --rx0 --ry0) or none.")
        return

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

    print("=" * 66)
    print("Eye test. Per-eye decisions. Local only.")
    print(f"  each eye: x > x0+{args.dx:.0f} -> left   x < x0-{args.dx:.0f} -> right")
    print(f"            y < y0-{args.dy:.0f} -> up     y > y0+{args.dy:.0f} -> down")
    print("  both agree -> that word; one decides, other neutral -> that word")
    print("  both neutral -> centre; the two disagree -> centre")
    print(f"  single axis {HINT_SECONDS:.0f}s -> word, both axes {COMBINED_SECONDS:.0f}s -> combined")
    print(f"  any state {CONFIRM_SECONDS:.0f}s -> '<state> confirm'")
    print(f"  ENTER saves the raw frame to {FRAME_DIR}")
    print("  Q / ESC / window X to quit.")
    if args.debug:
        print("  debug overlay ON (preview only)")
    print("=" * 66)

    cal = CentreCalibration()
    if fixed:
        cal.centre_r, cal.centre_l = fixed
        print(f"centre: my right = ({cal.centre_r[0]:.1f}, {cal.centre_r[1]:.1f})  "
              f"my left = ({cal.centre_l[0]:.1f}, {cal.centre_l[1]:.1f})")

    calib_announced = False
    last_line = ""

    h_state = v_state = ""
    h_start = v_start = 0.0
    spoken = set()
    announced_state = None

    G = {"r": None, "l": None, "h_state": "", "v_state": "",
         "h_held": 0.0, "v_held": 0.0, "state": ""}

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("ERROR: lost camera frame.")
                break

            frame = cv2.flip(frame, 1)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result = face_mesh.process(rgb)

            eye_pos = None
            if result.multi_face_landmarks:
                lm = result.multi_face_landmarks[0].landmark
                eye_pos = eye_centres(lm, frame.shape[1], frame.shape[0])

            now = time.time()
            if eye_pos is not None:
                (rx, ry), (lx, ly) = eye_pos
                g = f"R=({rx:.0f},{ry:.0f}) L=({lx:.0f},{ly:.0f})"
            else:
                g = "no face"

            # -------------------------------------------- calibration
            if not cal.done():
                if cal.needs_prompt():
                    cal.do_prompt(now)
                label, progress = cal.feed(eye_pos, now)
                if progress < 0:
                    line = f"{label:<40} {g}"
                else:
                    line = f"{label}  {hold_bar(progress * CALIB_SECONDS, CALIB_SECONDS)}  {g}"
                if line != last_line:
                    print("\r" + line + " " * 10, end="", flush=True)
                    last_line = line
                G.update(r=eye_pos[0] if eye_pos else None,
                         l=eye_pos[1] if eye_pos else None, state="calibrating")

            else:
                if not calib_announced:
                    print("\r" + " " * 120, end="\r")
                    print("Tracking now.")
                    speak("ready")
                    calib_announced = True
                    last_line = ""

                # ---------------------------------------- per-eye decisions
                r_eye = eye_pos[0] if eye_pos else None
                l_eye = eye_pos[1] if eye_pos else None

                rh, rv = eye_direction(r_eye, cal.centre_r, args.dx, args.dy)
                lh, lv = eye_direction(l_eye, cal.centre_l, args.dx, args.dy)

                mh = merge_axis(rh, lh)
                mv = merge_axis(rv, lv)
                # a conflict means the eyes point opposite ways on that axis
                if mh == "conflict":
                    mh = ""
                if mv == "conflict":
                    mv = ""

                new_h, new_v = mh, mv

                if new_h != h_state:
                    h_state = new_h
                    h_start = now
                if new_v != v_state:
                    v_state = new_v
                    v_start = now

                h_held = (now - h_start) if h_state else 0.0
                v_held = (now - v_start) if v_state else 0.0

                state = (f"{h_state} {v_state}" if (h_state and v_state)
                         else (h_state or v_state or "centre"))
                if state != announced_state:
                    announced_state = state
                    spoken.clear()

                both = bool(h_state and v_state)
                both_held = min(h_held, v_held) if both else 0.0
                single_held = max(h_held, v_held)

                if both and both_held >= COMBINED_SECONDS:
                    word = f"{h_state} {v_state}"
                    if word not in spoken:
                        speak(word)
                        spoken.add(word)
                elif (h_state or v_state) and single_held >= HINT_SECONDS:
                    word = h_state if (h_state and h_held >= HINT_SECONDS) else v_state
                    if word and word not in spoken:
                        speak(word)
                        spoken.add(word)

                if (h_state or v_state) and single_held >= CONFIRM_SECONDS:
                    word = (f"{h_state} {v_state}" if both and both_held >= COMBINED_SECONDS
                            else (h_state if h_held >= HINT_SECONDS else v_state))
                    confirm = f"{word} confirm"
                    if confirm not in spoken:
                        speak(confirm)
                        spoken.add(confirm)

                # status line shows what each eye decided
                eye_bits = f"R:{rh or '.'}{rv or '.'} L:{lh or '.'}{lv or '.'}"
                if not h_state and not v_state:
                    line = f"centre      {eye_bits:<12} {g}"
                else:
                    parts = []
                    if h_state:
                        parts.append(f"h:{h_state}({h_held:.1f}s)")
                    if v_state:
                        parts.append(f"v:{v_state}({v_held:.1f}s)")
                    line = (f"{' + '.join(parts):<30} {eye_bits:<12} "
                            f"{hold_bar(single_held, CONFIRM_SECONDS)} {g}")

                if line != last_line:
                    print("\r" + line + " " * 16, end="", flush=True)
                    last_line = line

                G.update(r=r_eye, l=l_eye, h_state=h_state, v_state=v_state,
                         h_held=h_held, v_held=v_held, state=state)

            # -------------------------------------------- window
            if show_preview:
                display = frame.copy()
                if args.debug and eye_pos is not None:
                    centres = (cal.centre_r, cal.centre_l) if cal.done() else None
                    draw_debug(display, eye_pos, centres, args.dx, args.dy)

                cv2.imshow(WIN, display)
                key = cv2.waitKey(1) & 0xFF

                if key in (ord("q"), ord("Q"), 27):
                    break

                if key in (13, 10):
                    path = save_frame(frame)
                    print("\n" + "-" * 66)
                    print(f"[capture] image : {path if path else 'FAILED to save'}")
                    if G["r"]:
                        print(f"[capture] my right eye: ({G['r'][0]:.1f}, {G['r'][1]:.1f})")
                    if G["l"]:
                        print(f"[capture] my left  eye: ({G['l'][0]:.1f}, {G['l'][1]:.1f})")
                    if cal.done():
                        print(f"[capture] centre: R=({cal.centre_r[0]:.1f}, {cal.centre_r[1]:.1f}) "
                              f"L=({cal.centre_l[0]:.1f}, {cal.centre_l[1]:.1f})")
                    print(f"[capture] dx={args.dx:.0f} dy={args.dy:.0f}")
                    print(f"[capture] state : {G['state']}")
                    if G["h_state"]:
                        print(f"[capture] h held: {G['h_held']:.2f}s ({G['h_state']})")
                    if G["v_state"]:
                        print(f"[capture] v held: {G['v_held']:.2f}s ({G['v_state']})")
                    print("-" * 66)

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
