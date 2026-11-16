"""
Gaze + blink controller for an assistive arm.

Positions are measured as a RATIO inside the eye opening, not as pixels, so
sitting closer or further from the camera does not change the numbers.

Per eye, the iris is located inside the eyelid box:

        eye_outer ─────────────── eye_inner
              |    height (h)    |
              |------- iris -----|
                    width (w)

    h_ratio = (iris_x - outer_x) / (inner_x - outer_x)     0 at the outer
                                                           1 at the inner corner
    v_ratio = (iris_y - upper_y) / (lower_y - upper_y)     0 at the top lid
                                                           1 at the bottom lid

The right eye is mirrored before averaging, so both eyes agree on direction.
Calibration records the resting ratios; every reading afterwards is a
difference from that baseline.

    gaze_h - h0 greater than LEFT_R  -> left
    gaze_h - h0 less    than RIGHT_R -> right
    gaze_v - v0 less    than UP_R    -> up
    gaze_v - v0 greater than DOWN_R  -> down

GESTURES
    long blink     eyes shut 0.50 s or more. Three of them switch the arming
                   on or off; the count is dropped after 3 s of silence.
    short blink    eyes shut 0.05 - 0.30 s. Two in a row toggles fetch mode.
    0.30 - 0.50 s  dead zone, ignored, so an ordinary blink does nothing.

FETCH MODE
    Off -> on   two short blinks, speaker says fetch
    On  -> off  two short blinks, speaker says stop, waits, then says back

ARM HOOKS
    on_left on_right on_up on_down   a direction was confirmed
    on_centre                        eyes came back to rest
    on_fetch on_stop on_back         fetch mode changes
    on_system_on on_system_off       power toggle

Point e (midpoint of the upper eyelids) is still drawn, but only as a guide.
The decision comes from the ratios, never from e.

This build prints nothing to the console except the arm hooks.

Usage:
    python eye_test.py
    python eye_test.py --h0 0.50 --v0 0.55     # reuse a calibration
    python eye_test.py --flip-y                # invert up/down if needed

Keys:
    ENTER saves the raw frame, Q or ESC quits.
    Everything runs locally. No image leaves this machine.
"""

import argparse
import os
import subprocess
import time

import cv2
import mediapipe as mp

# ---- timing
HOLD_SECONDS = 1.0
CONFIRM_SECONDS = 1.0
BLINK_GAP = 2.0
STOP_GAP = 1.0
TRIPLE_GAP = 3.0
CALIB_WAIT_SECONDS = 2.0
CALIB_SECONDS = 3.0

# ---- gaze thresholds, in ratio units. A full sweep across the eye opening
# ---- is 1.000, so 0.12 means the iris travelled 12% of the eye width.
LEFT_R = 0.12
RIGHT_R = 0.12
UP_R = 0.12
DOWN_R = 0.12
CENTER_R = 0.05           # inside this band counts as no direction

# ---- blink bands, seconds
SHORT_MIN = 0.05
SHORT_MAX = 0.30
LONG_MIN = 0.50

# ---- eye aspect ratio, decides open or shut
EAR_OPEN = 0.150
EAR_CLOSED = 0.090

VIEW_W, VIEW_H = 1280, 720
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
FRAME_DIR = os.path.join(PROJECT_DIR, "saved_frames")


# ---------------------------------------------------------------- arm hooks
class ArmActions:
    """Every event the arm needs. Subclass it and pass to main()."""

    def on_left(self):
        print("[arm] LEFT")

    def on_right(self):
        print("[arm] RIGHT")

    def on_up(self):
        print("[arm] UP")

    def on_down(self):
        print("[arm] DOWN")

    def on_centre(self):
        pass

    def on_fetch(self):
        print("[arm] FETCH")

    def on_stop(self):
        print("[arm] STOP")

    def on_back(self):
        print("[arm] BACK")

    def on_system_on(self):
        print("[arm] SYSTEM ON")

    def on_system_off(self):
        print("[arm] SYSTEM OFF")


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

SPEAK_BUSY = 1.2
_speaking_until = 0.0


def speak(text, busy_for=SPEAK_BUSY):
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

# mirrored image: 468s sit on the viewer's left = YOUR right eye
MY_RIGHT_IRIS = [468, 469, 470, 471, 472]
MY_LEFT_IRIS = [473, 474, 475, 476, 477]

# per eye: upper lid ids, lower lid ids, corner a, corner b
EYE_BOX = {
    "left": ([386, 385, 387], [374, 380, 373], 362, 263),
    "right": ([159, 158, 160], [145, 153, 144], 133, 33),
}


def eye_ratios(landmarks, w, h):
    """
    Iris position inside each eye opening, in 0-1 ratio units.

    Returns dict with h and v per eye plus their averages, and the pixel
    lid gap used for open/shut detection.
    """
    def pt(i):
        return landmarks[i].x * w, landmarks[i].y * h

    def one_eye(iris_ids, upper_ids, lower_ids, ca, cb):
        iris_x = sum(landmarks[i].x for i in iris_ids) / len(iris_ids) * w
        iris_y = sum(landmarks[i].y for i in iris_ids) / len(iris_ids) * h

        ax, ay = pt(ca)
        bx, by = pt(cb)
        up_y = sum(landmarks[i].y for i in upper_ids) / len(upper_ids) * h
        lo_y = sum(landmarks[i].y for i in lower_ids) / len(lower_ids) * h

        span_x = bx - ax
        span_y = lo_y - up_y
        hr = (iris_x - ax) / span_x if abs(span_x) > 1e-6 else 0.5
        vr = (iris_y - up_y) / span_y if abs(span_y) > 1e-6 else 0.5
        return hr, vr, abs(span_y), abs(span_x)

    lh, lv, lgap, lwid = one_eye(MY_LEFT_IRIS, *EYE_BOX["left"])
    rh, rv, rgap, rwid = one_eye(MY_RIGHT_IRIS, *EYE_BOX["right"])

    # Both eyes are described inner corner -> outer corner, so the raw ratio
    # grows toward the ear. Invert both, so a look to YOUR left raises the
    # value for each eye and the average stays meaningful.
    lh = 1.0 - lh
    rh = 1.0 - rh

    return {
        "h": (lh + rh) / 2.0,
        "v": (lv + rv) / 2.0,
        "lh": lh, "rh": rh,
        "lv": lv, "rv": rv,
        "gap": (lgap + rgap) / 2.0,
        "wid": (lwid + rwid) / 2.0,
    }


def eye_aspect_ratio(ratios):
    """EAR = vertical lid gap / horizontal eye width."""
    return ratios["gap"] / max(1e-6, ratios["wid"])


def eyes_closed_state(ear, was_closed):
    if was_closed:
        return ear < EAR_OPEN
    return ear < EAR_CLOSED


def lid_midpoint(landmarks, w, h):
    """Point e, the midpoint of both upper eyelids. Display only."""
    ids = EYE_BOX["left"][0] + EYE_BOX["right"][0]
    return (sum(landmarks[i].x for i in ids) / len(ids) * w,
            sum(landmarks[i].y for i in ids) / len(ids) * h)


# ---------------------------------------------------------------- drawing
def draw_eye_boxes(frame, landmarks, w, h):
    """Outline each eye opening and mark the iris centre."""
    for iris_ids, box, colour in (
            (MY_LEFT_IRIS, EYE_BOX["left"], (0, 255, 0)),
            (MY_RIGHT_IRIS, EYE_BOX["right"], (0, 0, 255))):
        upper, lower, ca, cb = box
        ax = int(landmarks[ca].x * w), int(landmarks[ca].y * h)
        bx = int(landmarks[cb].x * w), int(landmarks[cb].y * h)
        up_y = int(sum(landmarks[i].y for i in upper) / len(upper) * h)
        lo_y = int(sum(landmarks[i].y for i in lower) / len(lower) * h)
        cv2.rectangle(frame, (min(ax[0], bx[0]), up_y),
                      (max(ax[0], bx[0]), lo_y), colour, 1)

        ix = int(sum(landmarks[i].x for i in iris_ids) / len(iris_ids) * w)
        iy = int(sum(landmarks[i].y for i in iris_ids) / len(iris_ids) * h)
        cv2.drawMarker(frame, (ix, iy), colour, cv2.MARKER_CROSS, 14, 2)


def draw_state(frame, ratios, baseline, sys_on, fetch_mode, long_count,
               short_count, status):
    h, w = frame.shape[:2]

    head, head_colour = ("SYS ON", (0, 255, 0)) if sys_on else ("SYS OFF", (0, 0, 255))
    cv2.putText(frame, head, (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.85, head_colour, 2)

    mode_text = "FETCH" if fetch_mode else "idle"
    mode_colour = (0, 165, 255) if fetch_mode else (170, 170, 170)
    cv2.putText(frame, mode_text, (12, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.7, mode_colour, 2)

    if status:
        cv2.putText(frame, status, (12, 86), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (255, 255, 255), 1)

    # right side: live ratios against the baseline
    x0 = w - 250
    y0 = 12
    cv2.rectangle(frame, (x0 - 8, y0 - 4), (w - 8, y0 + 96), (35, 35, 35), -1)

    if ratios is not None:
        cv2.putText(frame, f"h {ratios['h']:.3f}", (x0, y0 + 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (140, 220, 255), 1)
        cv2.putText(frame, f"v {ratios['v']:.3f}", (x0, y0 + 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (140, 220, 255), 1)
        if baseline is not None:
            dh = ratios["h"] - baseline[0]
            dv = ratios["v"] - baseline[1]
            cv2.putText(frame, f"dh {dh:+.3f}", (x0, y0 + 54),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            cv2.putText(frame, f"dv {dv:+.3f}", (x0, y0 + 72),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        cv2.putText(frame, f"ear {eye_aspect_ratio(ratios):.3f}", (x0, y0 + 90),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 120), 1)

    dots = "".join("*" if i < long_count else "." for i in range(3))
    cv2.putText(frame, f"short {short_count}  long {dots}", (x0, y0 + 116),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 210, 120), 1)


def draw_calibration(frame, label, progress, ratios):
    h, w = frame.shape[:2]
    cv2.rectangle(frame, (0, 0), (w, 90), (40, 40, 40), -1)
    cv2.putText(frame, label, (16, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2)

    bar_w = int((w - 32) * max(0.0, min(1.0, progress)))
    cv2.rectangle(frame, (16, 56), (w - 16, 76), (70, 70, 70), -1)
    if progress > 0:
        cv2.rectangle(frame, (16, 56), (16 + bar_w, 76), (0, 255, 0), -1)

    if ratios is not None:
        cv2.putText(frame, f"h {ratios['h']:.3f}  v {ratios['v']:.3f}",
                    (16, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)


# ---------------------------------------------------------------- calibration
class RestCalibration:
    """Records the resting ratios h0 and v0, using the median."""

    def __init__(self):
        self.prompt_at = None
        self.record_start = None
        self.hs = []
        self.vs = []
        self.h0 = None
        self.v0 = None

    def needs_prompt(self):
        return self.prompt_at is None

    def do_prompt(self, now, actions=None):
        speak("Please look straight ahead", busy_for=2.0)
        self.prompt_at = now

    def feed(self, ratios, now):
        waited = now - self.prompt_at
        if waited < CALIB_WAIT_SECONDS:
            return f"LOOK CENTRE - starting in {max(0.0, CALIB_WAIT_SECONDS - waited):.1f}s", -1.0

        if self.record_start is None:
            self.record_start = now
        progress = (now - self.record_start) / CALIB_SECONDS

        if ratios is not None:
            self.hs.append(ratios["h"])
            self.vs.append(ratios["v"])

        if progress >= 1.0 and self.hs:
            hs = sorted(self.hs)
            vs = sorted(self.vs)
            self.h0 = hs[len(hs) // 2]
            self.v0 = vs[len(vs) // 2]

        return "RECORDING CENTRE", min(1.0, progress)

    def done(self):
        return self.h0 is not None and self.v0 is not None


def save_frame(frame):
    try:
        os.makedirs(FRAME_DIR, exist_ok=True)
        name = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}.png"
        path = os.path.join(FRAME_DIR, name)
        return path if cv2.imwrite(path, frame) else None
    except OSError:
        return None


def direction_of(dh, dv, flip_y):
    """Pick the dominant axis. Returns '', 'left', 'right', 'up' or 'down'."""
    if flip_y:
        dv = -dv

    horiz = ""
    if dh > LEFT_R:
        horiz = "left"
    elif dh < -RIGHT_R:
        horiz = "right"

    vert = ""
    if dv < -UP_R:
        vert = "up"
    elif dv > DOWN_R:
        vert = "down"

    if horiz and vert:
        return horiz if abs(dh) >= abs(dv) else vert
    return horiz or vert


def parse_args():
    p = argparse.ArgumentParser(description="Gaze and blink controller, ratio based")
    p.add_argument("--h0", type=float, default=None, help="resting horizontal ratio")
    p.add_argument("--v0", type=float, default=None, help="resting vertical ratio")
    p.add_argument("--left-r", type=float, default=LEFT_R)
    p.add_argument("--right-r", type=float, default=RIGHT_R)
    p.add_argument("--up-r", type=float, default=UP_R)
    p.add_argument("--down-r", type=float, default=DOWN_R)
    p.add_argument("--center-r", type=float, default=CENTER_R)
    p.add_argument("--hold", type=float, default=HOLD_SECONDS)
    p.add_argument("--confirm-after", type=float, default=CONFIRM_SECONDS)
    p.add_argument("--blink-gap", type=float, default=BLINK_GAP)
    p.add_argument("--ear-open", type=float, default=EAR_OPEN)
    p.add_argument("--ear-closed", type=float, default=EAR_CLOSED)
    p.add_argument("--short-min", type=float, default=SHORT_MIN)
    p.add_argument("--short-max", type=float, default=SHORT_MAX)
    p.add_argument("--long-min", type=float, default=LONG_MIN)
    p.add_argument("--flip-y", action="store_true", help="invert up/down")
    p.add_argument("--calib", action="store_true", help="calibrate even if h0/v0 given")
    p.add_argument("--no-preview", action="store_true", help="run headless")
    p.add_argument("--plain", action="store_true", help="raw camera image only")
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
    if not args.calib and args.h0 is not None and args.v0 is not None:
        fixed = (args.h0, args.v0)

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
        cal.h0, cal.v0 = fixed

    # ---- state
    sys_on = True
    fetch_mode = False
    back_due_at = 0.0
    calm_announced = False

    eyes_closed = False
    close_start = 0.0
    short_count = 0
    long_count = 0
    last_long_at = 0.0
    last_short_at = 0.0
    short_pair_pending = False

    direction = ""
    dir_start = 0.0
    spoken_direction = ""

    ratios = None
    lm = None
    status = ""
    dh = dv = 0.0
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

            ratios = None
            lm = None
            face_seen = False
            if result.multi_face_landmarks:
                face_seen = True
                lm = result.multi_face_landmarks[0].landmark
                ratios = eye_ratios(lm, w, h)
                eyes_closed = eyes_closed_state(eye_aspect_ratio(ratios), eyes_closed)

            now = time.time()

            # -------------------------------------------- calibration
            if not cal.done():
                if cal.needs_prompt():
                    cal.do_prompt(now)
                label, progress = cal.feed(ratios, now)
                if show_preview and not args.plain:
                    draw_calibration(frame, label, max(0.0, progress), ratios)
                    if lm is not None:
                        draw_eye_boxes(frame, lm, w, h)
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
                        close_start = 0.0

                if long_count and (now - last_long_at) > TRIPLE_GAP:
                    long_count = 0
                if short_pair_pending and (now - last_short_at) > BLINK_GAP:
                    short_pair_pending = False

                # -------------------------------- long blinks: system on/off
                if long_blink:
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
                if short_blink and sys_on and not speaker_busy(now):
                    if short_pair_pending:
                        short_pair_pending = False
                        if fetch_mode:
                            speak("stop")
                            actions.on_stop()
                            back_due_at = now + STOP_GAP
                            fetch_mode = False
                            status = "stop"
                        else:
                            speak("fetch")
                            actions.on_fetch()
                            fetch_mode = True
                            status = "fetch"
                        direction, dir_start = "", 0.0
                        spoken_direction = ""
                    else:
                        short_pair_pending = True
                        last_short_at = now

                # -------------------------------- gaze
                dh = ratios["h"] - cal.h0
                dv = ratios["v"] - cal.v0

                if sys_on and not eyes_closed:
                    new_dir = direction_of(dh, dv, args.flip_y)
                else:
                    new_dir = ""

                if new_dir != direction:
                    if direction and not new_dir:
                        status = "cancelled (left the zone)"
                    elif direction:
                        status = "cancelled (changed zone)"
                    if not new_dir and direction:
                        actions.on_centre()
                    direction = new_dir
                    dir_start = now
                    spoken_direction = ""

                held = (now - dir_start) if direction else 0.0

                if (direction and not speaker_busy(now)
                        and held >= args.hold
                        and direction != spoken_direction):
                    speak(direction)
                    spoken_direction = direction
                    status = f"say {direction}"

                elif (direction and direction == spoken_direction
                      and not speaker_busy(now)
                      and held >= args.hold + args.confirm_after):
                    speak("confirm")
                    status = f"{direction} confirmed"
                    hook = DIRECTION_HOOKS.get(direction)
                    if hook:
                        getattr(actions, hook)()
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
                        if lm is not None:
                            draw_eye_boxes(display, lm, w, h)
                            draw_state(display, ratios, (cal.h0, cal.v0),
                                       sys_on, fetch_mode, long_count,
                                       short_count, status)
                        else:
                            cv2.putText(display, "no face", (12, 30),
                                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
                            draw_state(display, None, None, sys_on, fetch_mode,
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
        cap.release()
        if show_preview:
            cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
