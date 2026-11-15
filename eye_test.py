"""
Eye tracking for an assistive arm: gaze directions plus blink control.

Points:
    a = my left pupil        b = my right pupil
    c = nose tip             d = 2D midpoint of a and b

Calibration records the resting offset of d relative to c:
    dx0 = d.x - c.x          dy0 = d.y - c.y

GAZE (only while the system is ON and not busy)
    HOR mode
      d.x - c.x > dx0 + 10  held 1s -> speak "left",  cooldown 2s
      d.x - c.x < dx0 - 10  held 1s -> speak "right", cooldown 2s
      |d.x - c.x - dx0| <= 10 held 1s -> switch to VER
    VER mode
      d.y - c.y - dy0 < -10 held 1s -> speak "up",    cooldown 2s
      d.y - c.y - dy0 > +5  held 1s -> speak "down",  cooldown 2s
      |d.y - c.y - dy0| <= 10 held 1s -> switch to HOR

BLINKS - three non-overlapping bands, so the gestures never collide
    0.05 - 0.30 s  short blink -> "confirm"
                   only inside the 2s cooldown after a spoken direction.
                   It opens a fresh 1s window in which a fetch may start.
                   Short blinks are counted forever on the overlay.
    0.30 - 0.50 s  dead zone, ignored (the width of an ordinary blink)
    0.50 s and up  long blink. Three of them toggle the system on or off.
                   The count resets when no long blink arrives for 3 s, or
                   after a toggle. Only counted while ON and not cooling down.
    1.20 s and up  fetch, but only if the closure STARTED inside the confirm
                   window. Stays quiet until the eyes open, then speaks
                   "back" and cools down for as long as the eyes were shut.
    A fetch can only follow a confirm; it is never triggered on its own.

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
COOLDOWN_SECONDS = 2.0
SWITCH_SECONDS = 1.0
CALIB_WAIT_SECONDS = 2.0
CALIB_SECONDS = 3.0

LEFT_PX = 10.0        # d must be this far left of the resting x
RIGHT_PX = 10.0       # ...or this far right
UP_PX = 10.0          # d must rise this much
DOWN_PX = 5.0         # d must fall this much
CENTER_PX = 10.0      # back within this band switches mode

EAR_OPEN = 0.150      # above this the eyes count as open
EAR_CLOSED = 0.090    # below this the eyes count as closed

TRIPLE_GAP = 3.0      # a long blink older than this is dropped from the count
FETCH_MIN = 1.2       # a closure this long, inside the confirm window, is a fetch
CONFIRM_WINDOW = 1.0  # how long after "confirm" a fetch may start

# Three non-overlapping closure bands, so the gestures cannot collide:
#   0.05 - 0.30 s  short blink -> confirm (only inside a direction cooldown)
#   0.30 - 0.50 s  dead zone   -> ignored, the width of an ordinary blink
#   0.50 s and up  long blink  -> counted towards the triple-blink toggle
#   1.20 s and up  very long   -> fetch (only if it STARTS inside the confirm window)
SHORT_MIN = 0.05      # shortest closure that counts as a short blink
SHORT_MAX = 0.30      # longest closure that still counts as a short blink
LONG_MIN = 0.50       # a closure this long feeds the triple-blink counter

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

# eyelid landmarks: (upper lid ids, lower lid ids, corner a, corner b)
EYE_LIDS = {
    "left": ([386, 385, 387], [374, 380, 373], 362, 263),   # your left eye
    "right": ([159, 158, 160], [145, 153, 144], 133, 33),   # your right eye
}


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


def eye_aspect_ratio(landmarks, upper, lower, corner_a, corner_b, w, h):
    """EAR = mean vertical lid gap / horizontal eye width."""
    up_y = sum(landmarks[i].y for i in upper) / len(upper) * h
    lo_y = sum(landmarks[i].y for i in lower) / len(lower) * h
    height = abs(lo_y - up_y)

    ax, ay = landmarks[corner_a].x * w, landmarks[corner_a].y * h
    bx, by = landmarks[corner_b].x * w, landmarks[corner_b].y * h
    width = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5

    return height / max(1e-6, width), height, width


def measure_eyes(landmarks, w, h):
    """EAR for each eye plus the mean, which drives blink detection."""
    ear_l, hgt_l, wid_l = eye_aspect_ratio(landmarks, *EYE_LIDS["left"], w, h)
    ear_r, hgt_r, wid_r = eye_aspect_ratio(landmarks, *EYE_LIDS["right"], w, h)
    return {"ear_l": ear_l, "ear_r": ear_r, "ear": (ear_l + ear_r) / 2.0,
            "hgt_l": hgt_l, "hgt_r": hgt_r,
            "wid_l": wid_l, "wid_r": wid_r}


def eyes_closed_state(ear, was_closed):
    """Hysteresis so a single noisy frame cannot flip the state."""
    if was_closed:
        return ear < EAR_OPEN
    return ear < EAR_CLOSED


# ---------------------------------------------------------------- drawing
def draw_debug(frame, m, centre, mode, state_msg, closed, sys_on, busy):
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

    if not sys_on:
        mode_colour = (80, 80, 80)
        mode_text = "SYS OFF"
    elif closed or busy:
        mode_colour = (90, 90, 90)
        mode_text = f"mode {mode}"
    else:
        mode_colour = (0, 255, 0) if mode == "HOR" else (0, 200, 255)
        mode_text = f"mode {mode}"

    cv2.putText(frame, mode_text, (12, 28), cv2.FONT_HERSHEY_SIMPLEX,
                0.8, mode_colour, 2)
    if state_msg:
        cv2.putText(frame, state_msg, (12, 58),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)

    if not sys_on:
        cv2.putText(frame, "SYSTEM OFF - blink 3x to start", (12, 88),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 120, 255), 2)
    elif closed:
        cv2.putText(frame, "EYES CLOSED", (12, 88),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)


def draw_eye_points(frame, landmarks, w, h):
    for upper, lower, ca, cb in EYE_LIDS.values():
        for i in upper:
            x, y = int(landmarks[i].x * w), int(landmarks[i].y * h)
            cv2.circle(frame, (x, y), 2, (0, 255, 0), -1)
        for i in lower:
            x, y = int(landmarks[i].x * w), int(landmarks[i].y * h)
            cv2.circle(frame, (x, y), 2, (0, 0, 255), -1)
        for i in (ca, cb):
            x, y = int(landmarks[i].x * w), int(landmarks[i].y * h)
            cv2.circle(frame, (x, y), 3, (255, 255, 0), -1)


def draw_ear(frame, eyes, closed):
    h, w = frame.shape[:2]
    x0 = w - 230
    y0 = 12

    cv2.rectangle(frame, (x0 - 8, y0 - 4), (w - 8, y0 + 78), (35, 35, 35), -1)

    colour = (0, 0, 255) if closed else (0, 255, 120)
    cv2.putText(frame, f"EAR L {eyes['ear_l']:.3f}", (x0, y0 + 16),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1)
    cv2.putText(frame, f"EAR R {eyes['ear_r']:.3f}", (x0, y0 + 36),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1)
    cv2.putText(frame, f"mean  {eyes['ear']:.3f}  {'CLOSED' if closed else 'open'}",
                (x0, y0 + 56), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)

    bar_x, bar_y, bar_w, bar_h = x0, y0 + 62, 140, 8
    cv2.rectangle(frame, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h), (70, 70, 70), -1)
    frac = max(0.0, min(1.0, eyes["ear"] / 0.40))
    cv2.rectangle(frame, (bar_x, bar_y), (bar_x + int(bar_w * frac), bar_y + bar_h),
                  colour, -1)
    for thr, tick in ((EAR_CLOSED, (0, 0, 255)), (EAR_OPEN, (0, 255, 0))):
        tx = bar_x + int(bar_w * min(1.0, thr / 0.40))
        cv2.line(frame, (tx, bar_y - 3), (tx, bar_y + bar_h + 3), tick, 1)


def draw_blink_state(frame, short_count, long_count, busy):
    """Blink counters: short accumulates forever, long resets after 3s idle."""
    h, w = frame.shape[:2]
    x0 = w - 230
    y0 = 96

    cv2.rectangle(frame, (x0 - 8, y0 - 4), (w - 8, y0 + 52), (35, 35, 35), -1)

    cv2.putText(frame, f"short {short_count}", (x0, y0 + 16),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (140, 220, 255), 1)

    dots = "".join("*" if i < long_count else "." for i in range(3))
    cv2.putText(frame, f"long  {dots}", (x0, y0 + 36),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 210, 120), 1)

    if busy:
        cv2.putText(frame, busy, (x0, y0 + 66),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 200), 1)


def draw_numbers(frame, m, centre, x_err, y_err, trig_dir, held, sw_held, action):
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
    p = argparse.ArgumentParser(description="Eye tracking with gaze and blink control")
    p.add_argument("--dx0", type=float, default=None, help="resting d.x - c.x")
    p.add_argument("--dy0", type=float, default=None, help="resting d.y - c.y")
    p.add_argument("--left-px", type=float, default=LEFT_PX)
    p.add_argument("--right-px", type=float, default=RIGHT_PX)
    p.add_argument("--up-px", type=float, default=UP_PX)
    p.add_argument("--down-px", type=float, default=DOWN_PX)
    p.add_argument("--center-px", type=float, default=CENTER_PX)
    p.add_argument("--ear-open", type=float, default=EAR_OPEN,
                   help="EAR above this counts as open")
    p.add_argument("--ear-closed", type=float, default=EAR_CLOSED,
                   help="EAR below this counts as closed")
    p.add_argument("--short-min", type=float, default=SHORT_MIN,
                   help="shortest closure that counts as a confirm blink")
    p.add_argument("--short-max", type=float, default=SHORT_MAX,
                   help="longest closure that still counts as a confirm blink")
    p.add_argument("--long-min", type=float, default=LONG_MIN,
                   help="closure length that feeds the triple-blink counter")
    p.add_argument("--fetch-min", type=float, default=FETCH_MIN,
                   help="closure length that counts as a fetch")
    p.add_argument("--flip-y", action="store_true", help="invert the up/down test")
    p.add_argument("--calib", action="store_true", help="calibrate even if dx0/dy0 given")
    p.add_argument("--no-preview", action="store_true", help="run headless")
    p.add_argument("--plain", action="store_true",
                   help="raw camera image only, no overlay at all")
    return p.parse_args()


def main():
    global EAR_OPEN, EAR_CLOSED, SHORT_MIN, SHORT_MAX, LONG_MIN, FETCH_MIN
    args = parse_args()

    EAR_OPEN = args.ear_open
    EAR_CLOSED = args.ear_closed
    if EAR_CLOSED >= EAR_OPEN:
        EAR_CLOSED = EAR_OPEN - 0.02

    SHORT_MIN = args.short_min
    SHORT_MAX = args.short_max
    LONG_MIN = args.long_min
    FETCH_MIN = args.fetch_min
    # keep the three bands sane and non-overlapping
    if SHORT_MAX <= SHORT_MIN:
        SHORT_MAX = SHORT_MIN + 0.05
    if LONG_MIN <= SHORT_MAX:
        LONG_MIN = SHORT_MAX + 0.05
    if FETCH_MIN <= LONG_MIN:
        FETCH_MIN = LONG_MIN + 0.2

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

    # ---- system state
    sys_on = True                 # runs as soon as the script starts
    mode = "HOR"
    calib_announced = False

    trig_dir = None
    trig_start = 0.0
    switch_start = None
    cooldown_until = 0.0

    # ---- blink bookkeeping
    eyes_closed = False
    close_start = 0.0
    short_count = 0               # short blinks, accumulates forever
    long_count = 0                # long blinks, resets after TRIPLE_GAP idle
    last_long_at = 0.0
    confirm_until = 0.0
    fetch_active = False
    fetch_close_start = 0.0
    back_cooldown_until = 0.0

    m = None
    eyes = None
    action = "-"
    busy = ""
    x_err = y_err = 0.0
    held = sw_held = 0.0

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break

            frame = cv2.flip(frame, 1)
            h, w = frame.shape[:2]
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result = face_mesh.process(rgb)

            m = None
            eyes = None
            lm = None
            face_seen = False
            if result.multi_face_landmarks:
                face_seen = True
                lm = result.multi_face_landmarks[0].landmark
                m = measure(lm, w, h)
                eyes = measure_eyes(lm, w, h)
                was_closed = eyes_closed
                eyes_closed = eyes_closed_state(eyes["ear"], eyes_closed)

            now = time.time()

            # -------------------------------------------- calibration
            if not cal.done():
                if cal.needs_prompt():
                    cal.do_prompt(now)
                label, progress = cal.feed(m, now)
                if show_preview and not args.plain:
                    draw_calibration(frame, label, max(0.0, progress), m)
                    if eyes is not None:
                        draw_ear(frame, eyes, eyes_closed)
                if cal.done() and not calib_announced:
                    speak("system on")
                    calib_announced = True

            elif not face_seen:
                trig_dir, trig_start, switch_start = None, 0.0, None
                action = "no face"
                busy = ""

            else:
                # ---------------------------------------- blink bookkeeping
                # classify the closure that just ended:
                #   0.10-0.30 s -> short blink (confirm)
                #   0.30-0.50 s -> dead zone, ignored
                #   >= 0.50 s   -> long blink (feeds the triple counter)
                short_blink = False       # 0.10-0.30 s -> confirm
                long_blink = False        # >= 0.50 s  -> triple-blink counter
                closed_for = 0.0
                if eyes_closed:
                    if close_start == 0.0:
                        close_start = now
                    closed_for = now - close_start
                else:
                    if close_start != 0.0:
                        closed_for = now - close_start
                        if SHORT_MIN <= closed_for <= SHORT_MAX:
                            short_blink = True
                            short_count += 1
                        elif closed_for >= LONG_MIN:
                            long_blink = True
                        # 0.30-0.50 s is the dead zone: deliberately ignored
                        close_start = 0.0

                # a long blink older than TRIPLE_GAP drops the whole count
                if long_count and (now - last_long_at) > TRIPLE_GAP:
                    long_count = 0

                # fetch: only possible inside the confirm window
                if (not fetch_active and eyes_closed
                        and confirm_until > 0.0 and close_start > 0.0
                        and close_start <= confirm_until
                        and closed_for >= FETCH_MIN):
                    fetch_active = True
                    fetch_close_start = close_start
                    cooldown_until = 0.0
                    confirm_until = 0.0
                    speak("fetch")

                if fetch_active:
                    busy = "fetch"
                    action = "fetch: waiting for eyes to open"
                    if not eyes_closed:
                        long_enough = now - fetch_close_start
                        speak("back")
                        back_cooldown_until = now + long_enough
                        cooldown_until = back_cooldown_until
                        fetch_active = False
                        busy = ""

                elif now < back_cooldown_until:
                    busy = f"back {back_cooldown_until - now:.1f}s"
                    action = "back cooldown"
                    trig_dir, trig_start, switch_start = None, 0.0, None

                elif short_blink and now < cooldown_until:
                    # short blink inside the direction cooldown -> confirm
                    speak("confirm")
                    cooldown_until = now + COOLDOWN_SECONDS
                    confirm_until = now + CONFIRM_WINDOW
                    long_count = 0
                    busy = f"confirm {CONFIRM_WINDOW:.0f}s"
                    action = "confirm: fetch window open"

                elif now < cooldown_until:
                    busy = f"cooldown {cooldown_until - now:.1f}s"
                    action = "direction cooldown"
                    trig_dir, trig_start, switch_start = None, 0.0, None

                else:
                    busy = ""

                    # -------------------------------- triple blink toggles the system
                    if long_blink:
                        long_count += 1
                        last_long_at = now
                    if long_count >= 3:
                        long_count = 0
                        sys_on = not sys_on
                        speak("on" if sys_on else "off")
                        cooldown_until = now + COOLDOWN_SECONDS
                        trig_dir, trig_start, switch_start = None, 0.0, None

                    # -------------------------------- gaze, only while ON
                    if sys_on:
                        x_err = m["dx"] - cal.dx0
                        y_err = m["dy"] - cal.dy0
                        if args.flip_y:
                            y_err = -y_err

                        spoken = None
                        if mode == "HOR":
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
                                    confirm_until = 0.0
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
                                    confirm_until = 0.0
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
                    else:
                        trig_dir, trig_start, switch_start = None, 0.0, None
                        held = sw_held = 0.0
                        action = "system off"

            # -------------------------------------------- window
            if show_preview:
                display = frame.copy()
                if not args.plain:
                    if cal.done():
                        if m is not None:
                            draw_debug(display, m, (cal.dx0, cal.dy0), mode,
                                       action, eyes_closed, sys_on, bool(busy))
                            draw_eye_points(display, lm, w, h)
                            draw_ear(display, eyes, eyes_closed)
                            draw_blink_state(display, short_count, long_count, busy)
                            draw_numbers(display, m, (cal.dx0, cal.dy0),
                                         x_err, y_err, trig_dir, held, sw_held, action)
                        else:
                            cv2.putText(display, "no face", (12, 28),
                                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)

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
