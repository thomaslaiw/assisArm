"""
Gaze + blink controller for an assistive arm.

Calibration records the resting offset of the pupil midpoint (d) from the
nose tip (c):
    dx0 = d.x - c.x          dy0 = d.y - c.y

GESTURES
    long blink     eyes shut for 0.50 s or more
                   Three of them switch the system on or off. The count is
                   dropped when 3 s pass without another long blink.
    short blink    eyes shut for 0.05 - 0.30 s
                   Two in a row toggles fetch mode.
    anything between 0.30 and 0.50 s is a dead zone and is ignored, so an
    ordinary blink cannot trigger anything.

FETCH MODE
    Off -> on   two short blinks, speaker says "fetch"
    On  -> off  two short blinks, speaker says "stop", waits STOP_GAP, then
                says "back"

DIRECTIONS
    Only while the system is on and the speaker is quiet.
    The gaze is compared with the calibrated centre:
        x greater than dx0 + LEFT_PX   -> left
        x less    than dx0 - RIGHT_PX  -> right
        y less    than dy0 - UP_PX     -> up
        x inside the centre band and the eyes half shut -> down

    "Half shut" means the eyelid gap has fallen below HALF_EYE_RATIO of the
    gap measured during calibration, so it follows your own eye shape rather
    than a fixed pixel count. Half shutting only becomes "down" when the
    horizontal error is already inside the centre band; otherwise the normal
    left/right test still wins.
    Holding a direction for HOLD_SECONDS speaks it. Keeping the eyes in that
    same zone for another CONFIRM_SECONDS without a blink makes the speaker
    say "confirm".

    Leaving the zone - into another direction or back to the centre - wipes
    the slate. Nothing is remembered between visits, so:
        left  -> "left" -> stay 2s -> "confirm" -> leave -> left -> "left" ...
        left  -> "left" -> leave early -> nothing, the visit never counted
    A confirm also drops the zone immediately, so the eyes have to leave and
    return before that direction can be spoken a second time.

ARM HOOKS
    Every confirmed event calls a method on an ArmActions instance:
        on_left  on_right  on_up  on_down     a direction was confirmed
        on_centre                             eyes came back to rest
        on_fetch  on_stop  on_back            fetch mode changes
        on_system_on  on_system_off           power toggle
    Subclass ArmActions and pass it to main() to drive the real arm. The stock
    class just prints, so a normal run shows what the arm would have done.
    Nothing about the gaze logic changes when you wire the arm in.

This build prints nothing to the console except the arm hooks.
Use eye_test_verbose.py when the numbers are needed for tuning.

Usage:
    python eye_test.py                      # calibrate then run
    python eye_test.py --dx0 -232 --dy0 -38 # reuse a previous centre
    python eye_test.py --flip-y             # invert up/down if needed

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

# ---- timing
HOLD_SECONDS = 1.0        # hold a direction this long to hear it
CONFIRM_SECONDS = 1.0     # keep holding it this much longer to hear "confirm"
BLINK_GAP = 2.0           # two short blinks must be this close together
STOP_GAP = 1.0            # pause between "stop" and "back"
FETCH_GRACE = 1.0         # after fetch starts, ignore eye closures this long
TRIPLE_GAP = 3.0          # long blinks older than this drop the count
CALIB_WAIT_SECONDS = 2.0
CALIB_SECONDS = 3.0

# ---- gaze thresholds, pixels of travel from the calibrated centre
LEFT_PX = 8.0
RIGHT_PX = 8.0
UP_PX = 10.0
DOWN_PX = 5.0
CENTER_PX = 8.0          # inside this band counts as no direction

# ---- blink bands, seconds
SHORT_MIN = 0.05
SHORT_MAX = 0.30
LONG_MIN = 0.50

# ---- half shut eyes mean "down"
HALF_EYE_RATIO = 0.5   # eyelid gap below this fraction of the calibrated gap

# ---- eye aspect ratio, used to decide open or shut
EAR_OPEN = 0.150
EAR_CLOSED = 0.090

VIEW_W, VIEW_H = 1280, 720
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
FRAME_DIR = os.path.join(PROJECT_DIR, "saved_frames")


# ---------------------------------------------------------------- arm hooks
class ArmActions:
    """
    Every hook the robotic arm needs. Subclass this, or replace the instance
    passed to main(), and the arm code plugs in without touching the gaze
    logic. Each hook is called exactly once per event.

        on_left / on_right / on_up / on_down   a direction was confirmed
        on_centre                              the eyes came back to rest
        on_fetch / on_stop / on_back           fetch mode changes
        on_system_on / on_system_off           power toggle
        on_shutdown                            the program is closing

    The defaults only print, so a stock run tells you what the arm would do.
    """

    def on_left(self):
        print("[arm] LEFT")

    def on_right(self):
        print("[arm] RIGHT")

    def on_up(self):
        print("[arm] UP")

    def on_down(self):
        print("[arm] DOWN")

    def on_centre(self):
        print("[arm] CENTRE")

    def on_fetch(self):
        print("[arm] FETCH - arm taking control")

    def on_stop(self):
        print("[arm] STOP - arm holding still")

    def on_back(self):
        print("[arm] BACK - arm returning home")

    def on_system_on(self):
        print("[arm] SYSTEM ON")

    def on_system_off(self):
        print("[arm] SYSTEM OFF")

    def on_shutdown(self):
        print("[arm] SHUTDOWN - program closing")


DIRECTION_HOOKS = {
    "left": "on_left",
    "right": "on_right",
    "up": "on_up",
    "down": "on_down",
}

# ---------------------------------------------------------------- speech
_PS_SPEAK = (
    "Add-Type -AssemblyName System.Speech; "
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "$s.Rate = 0; "
    "$s.Speak('{text}')"
)

# rough upper bound on how long a spoken word occupies the speaker
SPEAK_BUSY = 1.2
_speaking_until = 0.0


def speak(text, busy_for=SPEAK_BUSY):
    """Say something and keep the gaze logic quiet until it finishes."""
    global _speaking_until
    safe = text.replace("'", "''")
    subprocess.Popen(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
         _PS_SPEAK.format(text=safe)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _speaking_until = max(_speaking_until, time.time() + busy_for)


def speaker_busy(now):
    return now < _speaking_until


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

EYE_LIDS = {
    "left": ([386, 385, 387], [374, 380, 373], 362, 263),
    "right": ([159, 158, 160], [145, 153, 144], 133, 33),
}


def measure(landmarks, w, h):
    """a, b, c, d, e in pixels plus the offsets we care about.

    e is the midpoint of the upper eyelids. It sits on the same horizontal
    line as a, b, c and d, and is currently display-only.
    """
    def centre(ids):
        return (sum(landmarks[i].x for i in ids) / len(ids) * w,
                sum(landmarks[i].y for i in ids) / len(ids) * h)

    a = centre(MY_LEFT_EYE_IRIS)       # my left pupil
    b = centre(MY_RIGHT_EYE_IRIS)      # my right pupil
    c = (landmarks[NOSE_TIP].x * w, landmarks[NOSE_TIP].y * h)
    d = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)

    # e = midpoint of the upper eyelids, on the same line as a, b, c and d
    upper_ids = EYE_LIDS["left"][0] + EYE_LIDS["right"][0]
    e = (sum(landmarks[i].x for i in upper_ids) / len(upper_ids) * w,
         sum(landmarks[i].y for i in upper_ids) / len(upper_ids) * h)

    return {"a": a, "b": b, "c": c, "d": d, "e": e,
            "dx": d[0] - c[0], "dy": d[1] - c[1],
            "ey": e[1] - c[1]}


def eye_aspect_ratio(landmarks, upper, lower, corner_a, corner_b, w, h):
    up_y = sum(landmarks[i].y for i in upper) / len(upper) * h
    lo_y = sum(landmarks[i].y for i in lower) / len(lower) * h
    height = abs(lo_y - up_y)

    ax, ay = landmarks[corner_a].x * w, landmarks[corner_a].y * h
    bx, by = landmarks[corner_b].x * w, landmarks[corner_b].y * h
    width = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5

    return height / max(1e-6, width), height, width


def measure_eyes(landmarks, w, h):
    ear_l, hgt_l, wid_l = eye_aspect_ratio(landmarks, *EYE_LIDS["left"], w, h)
    ear_r, hgt_r, wid_r = eye_aspect_ratio(landmarks, *EYE_LIDS["right"], w, h)
    return {"ear_l": ear_l, "ear_r": ear_r, "ear": (ear_l + ear_r) / 2.0,
            "hgt_l": hgt_l, "hgt_r": hgt_r,
            "wid_l": wid_l, "wid_r": wid_r}


def eyes_closed_state(ear, was_closed):
    """Hysteresis so one noisy frame cannot flip the state."""
    if was_closed:
        return ear < EAR_OPEN
    return ear < EAR_CLOSED


# ---------------------------------------------------------------- drawing
def draw_points(frame, m, centre):
    h, w = frame.shape[:2]

    for key, colour, label in (("a", (0, 255, 0), "a"),
                               ("b", (0, 0, 255), "b"),
                               ("c", (255, 0, 255), "c"),
                               ("d", (255, 255, 0), "d"),
                               ("e", (255, 160, 60), "e")):
        x, y = int(m[key][0]), int(m[key][1])
        cv2.drawMarker(frame, (x, y), colour, cv2.MARKER_CROSS, 16, 2)
        cv2.putText(frame, label, (x + 6, y - 6), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, colour, 2)

    cv2.line(frame, (int(m["c"][0]), int(m["c"][1])),
             (int(m["d"][0]), int(m["d"][1])), (150, 150, 150), 1)
    cv2.line(frame, (int(m["a"][0]), int(m["a"][1])),
             (int(m["b"][0]), int(m["b"][1])), (90, 90, 90), 1)
    cv2.line(frame, (int(m["e"][0]), int(m["e"][1])),
             (int(m["c"][0]), int(m["c"][1])), (160, 120, 60), 1)

    if centre is None:
        return
    dx0, dy0 = centre
    cx, cy = int(m["c"][0] + dx0), int(m["c"][1] + dy0)
    cv2.drawMarker(frame, (cx, cy), (255, 0, 255), cv2.MARKER_DIAMOND, 18, 2)
    for xx in (int(cx - LEFT_PX), int(cx + RIGHT_PX)):
        cv2.line(frame, (xx, 0), (xx, h), (80, 80, 200), 1)
    for yy in (int(cy - UP_PX), int(cy + DOWN_PX)):
        cv2.line(frame, (0, yy), (w, yy), (200, 160, 80), 1)


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


def draw_state(frame, sys_on, fetch_mode, long_count, short_count, status):
    """Top-left: system and mode. Below the EAR box: the blink counters."""
    h, w = frame.shape[:2]

    if sys_on:
        head = "SYS ON"
        head_colour = (0, 255, 0)
    else:
        head = "SYS OFF"
        head_colour = (0, 0, 255)
    cv2.putText(frame, head, (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.85, head_colour, 2)

    mode_text = "FETCH" if fetch_mode else "idle"
    mode_colour = (0, 165, 255) if fetch_mode else (170, 170, 170)
    cv2.putText(frame, mode_text, (12, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                mode_colour, 2)

    if status:
        cv2.putText(frame, status, (12, 86), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (255, 255, 255), 1)

    x0 = w - 230
    y0 = 96
    cv2.rectangle(frame, (x0 - 8, y0 - 4), (w - 8, y0 + 48), (35, 35, 35), -1)
    cv2.putText(frame, f"short {short_count}", (x0, y0 + 16),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (140, 220, 255), 1)
    dots = "".join("*" if i < long_count else "." for i in range(3))
    cv2.putText(frame, f"long  {dots}", (x0, y0 + 38),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 210, 120), 1)


def draw_numbers(frame, m, centre, x_err, y_err, direction, held):
    """Bottom-left readout."""
    h, w = frame.shape[:2]
    lines = []

    if m is not None:
        lines.append(f"c=({m['c'][0]:.0f},{m['c'][1]:.0f})  d=({m['d'][0]:.0f},{m['d'][1]:.0f})")
        lines.append(f"d-c=({m['dx']:+.1f},{m['dy']:+.1f})")
        lines.append(f"e-c y={m['ey']:+.1f}")
        if centre is not None:
            lines.append(f"err=({x_err:+.1f},{y_err:+.1f})")
    else:
        lines.append("no face")

    if direction:
        lines.append(f"dir={direction}  held={held:.1f}s")

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
                    f"c=({m['c'][0]:.0f},{m['c'][1]:.0f}) d=({m['d'][0]:.0f},{m['d'][1]:.0f}) "
                    f"e-c y={m['ey']:+.1f}",
                    (16, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)


# ---------------------------------------------------------------- calibration
class RestCalibration:
    """Records the resting offset of d from c, using the median."""

    def __init__(self):
        self.prompt_at = None
        self.record_start = None
        self.samples = []
        self.height_samples = []
        self.dx0 = None
        self.dy0 = None
        self.eye_h0 = None

    def needs_prompt(self):
        return self.prompt_at is None

    def do_prompt(self, now):
        speak("Please look straight ahead", busy_for=2.0)
        self.prompt_at = now

    def feed(self, m, eyes, now):
        waited = now - self.prompt_at
        if waited < CALIB_WAIT_SECONDS:
            return f"LOOK CENTRE - starting in {max(0.0, CALIB_WAIT_SECONDS - waited):.1f}s", -1.0

        if self.record_start is None:
            self.record_start = now
        progress = (now - self.record_start) / CALIB_SECONDS

        if m is not None:
            self.samples.append((m["dx"], m["dy"]))
        if eyes is not None:
            self.height_samples.append((eyes["hgt_l"] + eyes["hgt_r"]) / 2.0)

        if progress >= 1.0 and self.samples:
            xs = sorted(p[0] for p in self.samples)
            ys = sorted(p[1] for p in self.samples)
            self.dx0 = xs[len(xs) // 2]
            self.dy0 = ys[len(ys) // 2]
            if self.height_samples:
                hs = sorted(self.height_samples)
                self.eye_h0 = hs[len(hs) // 2]

        return "RECORDING CENTRE", min(1.0, progress)

    def done(self):
        return self.dx0 is not None and self.dy0 is not None

    def half_shut(self, eyes):
        """True when the eyelid gap has fallen below HALF_EYE_RATIO of rest."""
        if eyes is None or not self.eye_h0:
            return False
        h = (eyes["hgt_l"] + eyes["hgt_r"]) / 2.0
        return h < self.eye_h0 * HALF_EYE_RATIO


def save_frame(frame):
    try:
        os.makedirs(FRAME_DIR, exist_ok=True)
        name = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}.png"
        path = os.path.join(FRAME_DIR, name)
        return path if cv2.imwrite(path, frame) else None
    except OSError:
        return None


def direction_of(x_err, y_err, flip_y, half_shut=False):
    """
    Pick the dominant axis. Returns '', 'left', 'right', 'up' or 'down'.

    Half shutting the eyes only counts as "down" while the horizontal error is
    already inside the centre band, so a sideways glance always wins.
    """
    if flip_y:
        y_err = -y_err

    horiz = ""
    if x_err > LEFT_PX:
        horiz = "left"
    elif x_err < -RIGHT_PX:
        horiz = "right"

    if half_shut and not horiz and abs(x_err) <= CENTER_PX:
        return "down"

    vert = ""
    if y_err < -UP_PX:
        vert = "up"
    elif y_err > DOWN_PX:
        vert = "down"

    # one axis at a time: whichever error is larger wins
    if horiz and vert:
        return horiz if abs(x_err) >= abs(y_err) else vert
    return horiz or vert


def parse_args():
    p = argparse.ArgumentParser(description="Gaze and blink controller")
    p.add_argument("--dx0", type=float, default=None, help="resting d.x - c.x")
    p.add_argument("--dy0", type=float, default=None, help="resting d.y - c.y")
    p.add_argument("--left-px", type=float, default=LEFT_PX)
    p.add_argument("--right-px", type=float, default=RIGHT_PX)
    p.add_argument("--up-px", type=float, default=UP_PX)
    p.add_argument("--down-px", type=float, default=DOWN_PX)
    p.add_argument("--center-px", type=float, default=CENTER_PX)
    p.add_argument("--hold", type=float, default=HOLD_SECONDS,
                   help="hold a direction this long to hear it")
    p.add_argument("--confirm-after", type=float, default=CONFIRM_SECONDS,
                   help="keep holding it this much longer to hear confirm")
    p.add_argument("--blink-gap", type=float, default=BLINK_GAP,
                   help="two short blinks must be this close together")
    p.add_argument("--ear-open", type=float, default=EAR_OPEN)
    p.add_argument("--ear-closed", type=float, default=EAR_CLOSED)
    p.add_argument("--short-min", type=float, default=SHORT_MIN)
    p.add_argument("--short-max", type=float, default=SHORT_MAX)
    p.add_argument("--long-min", type=float, default=LONG_MIN)
    p.add_argument("--flip-y", action="store_true", help="invert up/down")
    p.add_argument("--calib", action="store_true",
                   help="calibrate even if dx0/dy0 are given")
    p.add_argument("--no-preview", action="store_true", help="run headless")
    p.add_argument("--plain", action="store_true",
                   help="raw camera image only, no overlay")
    return p.parse_args()


def main(actions=None):
    global EAR_OPEN, EAR_CLOSED, SHORT_MIN, SHORT_MAX, LONG_MIN
    if actions is None:
        actions = ArmActions()
    args = parse_args()

    EAR_OPEN = args.ear_open
    EAR_CLOSED = args.ear_closed
    if EAR_CLOSED >= EAR_OPEN:
        EAR_CLOSED = EAR_OPEN - 0.02

    SHORT_MIN = args.short_min
    SHORT_MAX = args.short_max
    LONG_MIN = args.long_min
    if SHORT_MAX <= SHORT_MIN:
        SHORT_MAX = SHORT_MIN + 0.05
    if LONG_MIN <= SHORT_MAX:
        LONG_MIN = SHORT_MAX + 0.05

    fixed = None
    if not args.calib and args.dx0 is not None and args.dy0 is not None:
        fixed = (args.dx0, args.dy0)

    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cap.isOpened():
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, VIEW_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, VIEW_H)

    WIN = "Gaze controller"
    show_preview = not args.no_preview
    if show_preview:
        cv2.namedWindow(WIN, cv2.WINDOW_AUTOSIZE)
        cv2.moveWindow(WIN, 0, 0)

    cal = RestCalibration()
    if fixed:
        cal.dx0, cal.dy0 = fixed

    # ---- state
    sys_on = True
    fetch_mode = False
    fetch_started_at = 0.0
    back_due_at = 0.0           # "stop" has been said, "back" is pending
    calm_announced = False

    # blink bookkeeping
    eyes_closed = False
    close_start = 0.0
    short_count = 0
    long_count = 0
    last_long_at = 0.0
    last_short_at = 0.0
    short_pair_pending = False  # first short blink of a pair is banked

    # gaze bookkeeping
    direction = ""
    dir_start = 0.0
    spoken_direction = ""

    m = None
    eyes = None
    status = ""
    x_err = y_err = 0.0
    held = 0.0

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
                eyes_closed = eyes_closed_state(eyes["ear"], eyes_closed)

            now = time.time()

            # -------------------------------------------- calibration
            if not cal.done():
                if cal.needs_prompt():
                    cal.do_prompt(now)
                label, progress = cal.feed(m, eyes, now)
                if show_preview and not args.plain:
                    draw_calibration(frame, label, max(0.0, progress), m)
                    if eyes is not None:
                        draw_ear(frame, eyes, eyes_closed)
                if cal.done() and not calm_announced:
                    speak("system on")
                    actions.on_system_on()
                    calm_announced = True

            elif not face_seen:
                direction, dir_start = "", 0.0
                status = "no face"

            else:
                # -------------------------------- classify the closure
                short_blink = False
                long_blink = False
                if eyes_closed:
                    if close_start == 0.0:
                        close_start = now
                else:
                    if close_start != 0.0:
                        closed_for = now - close_start
                        if SHORT_MIN <= closed_for <= SHORT_MAX:
                            short_blink = True
                            short_count += 1
                        elif closed_for >= LONG_MIN:
                            long_blink = True
                        # 0.30 - 0.50 s is the dead zone: ignored
                        close_start = 0.0

                # drop stale long blinks
                if long_count and (now - last_long_at) > TRIPLE_GAP:
                    long_count = 0
                # a banked short blink expires if the pair is not completed
                if short_pair_pending and (now - last_short_at) > BLINK_GAP:
                    short_pair_pending = False

                # -------------------------------- fetch: a closure stops it
                # While fetching nothing else listens: no power toggle, no blink
                # pairs. The only signal is the eyes closing, which stops the
                # fetch on that frame once the opening blink has cleared.
                if (fetch_mode and eyes_closed and sys_on
                        and (now - fetch_started_at) >= FETCH_GRACE):
                    speak("stop")
                    actions.on_stop()
                    back_due_at = now + STOP_GAP
                    fetch_mode = False
                    status = "stop (eyes closed)"
                    direction, dir_start = "", 0.0
                    spoken_direction = ""
                    long_count = 0
                    short_pair_pending = False
                    close_start = 0.0

                # -------------------------------- long blinks: system on/off
                if long_blink and not fetch_mode:
                    long_count += 1
                    last_long_at = now
                    if long_count >= 3:
                        long_count = 0
                        sys_on = not sys_on
                        speak("on" if sys_on else "off")
                        if sys_on:
                            actions.on_system_on()
                        else:
                            actions.on_system_off()
                        status = f"system {'on' if sys_on else 'off'}"
                        direction, dir_start = "", 0.0
                        spoken_direction = ""

                # "stop" was spoken earlier; "back" follows after STOP_GAP
                if back_due_at and now >= back_due_at and sys_on:
                    speak("back")
                    actions.on_back()
                    back_due_at = 0.0
                    status = "back"

                # -------------------------------- short blink pairs: fetch
                if short_blink and sys_on and not fetch_mode and not speaker_busy(now):
                    if short_pair_pending:
                        short_pair_pending = False
                        speak("fetch")
                        actions.on_fetch()
                        fetch_mode = True
                        fetch_started_at = now
                        status = "fetch"
                        direction, dir_start = "", 0.0
                        spoken_direction = ""
                    else:
                        short_pair_pending = True
                        last_short_at = now

                # -------------------------------- gaze
                x_err = m["dx"] - cal.dx0
                y_err = m["dy"] - cal.dy0

                if sys_on and not eyes_closed:
                    new_dir = direction_of(x_err, y_err, args.flip_y,
                                           cal.half_shut(eyes))
                else:
                    new_dir = ""

                # Entering a zone starts its clock. Leaving it, into another
                # zone or back to the centre, wipes the slate: nothing is
                # remembered, so the next visit starts from zero again.
                if new_dir != direction:
                    if direction and not new_dir:
                        status = "cancelled (left the zone)"
                    elif direction:
                        status = "cancelled (changed zone)"
                    # "just entered the centre" fires on any arrival at rest,
                    # whether from a direction or straight after a confirm
                    if not new_dir and direction:
                        actions.on_centre()
                    direction = new_dir
                    dir_start = now
                    spoken_direction = ""

                held = (now - dir_start) if direction else 0.0

                # speak the direction once it has been held long enough
                if (direction and not speaker_busy(now)
                        and held >= args.hold
                        and direction != spoken_direction):
                    speak(direction)
                    spoken_direction = direction
                    status = f"say {direction}"

                # keep holding, same zone, no blink -> confirm
                elif (direction and direction == spoken_direction
                      and not speaker_busy(now)
                      and held >= args.hold + args.confirm_after):
                    speak("confirm")
                    status = f"{direction} confirmed"
                    # hand the confirmed direction to the arm
                    hook = DIRECTION_HOOKS.get(direction)
                    if hook:
                        getattr(actions, hook)()
                    # a confirm ends the exchange: drop the direction so the
                    # eyes have to leave and come back before it counts again
                    direction = ""
                    dir_start = 0.0
                    spoken_direction = ""

                elif status == "" or status.startswith("say ") or status.startswith("cancelled"):
                    if direction:
                        status = f"{direction} {held:.1f}s"
                    else:
                        status = "centre"

            # -------------------------------------------- window
            if show_preview:
                display = frame.copy()
                if not args.plain:
                    if cal.done():
                        if m is not None:
                            draw_points(display, m, (cal.dx0, cal.dy0))
                            draw_eye_points(display, lm, w, h)
                            draw_ear(display, eyes, eyes_closed)
                            draw_state(display, sys_on, fetch_mode,
                                       long_count, short_count, status)
                            draw_numbers(display, m, (cal.dx0, cal.dy0),
                                         x_err, y_err, direction, held)
                        else:
                            cv2.putText(display, "no face", (12, 30),
                                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
                            draw_state(display, sys_on, fetch_mode,
                                       long_count, short_count, status)

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
        # the program is closing, however it was closed: let the arm park
        try:
            actions.on_shutdown()
        except Exception as e:
            print(f"[arm] could not park on exit: {e}")
        cap.release()
        if show_preview:
            cv2.destroyAllWindows()


if __name__ == "__main__":
    main()          # stock run: ArmActions prints what the arm would do

    # Wiring the real arm later is just:
    #
    #   from eye_test import ArmActions, main
    #
    #   class MyArm(ArmActions):
    #       def on_left(self):  arm.move(0, -50)
    #       def on_right(self): arm.move(0, +50)
    #       def on_up(self):    arm.move(0, +50, dz=+30)
    #       def on_down(self):  arm.move(0, +50, dz=-30)
    #       def on_fetch(self): arm.grip(close=True)
    #       def on_back(self):  arm.home()
    #
    #   main(MyArm())
