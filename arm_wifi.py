"""
Wire the gaze controller to the ESP8266 arm over WiFi.

The firmware on the D1 mini runs a small HTTP server (see
firmware/assisArm/assisArm.ino). This file turns confirmed gaze events into
HTTP calls, so eyeblinks and held gaze actually move the arm.

Usage:
    python arm_wifi.py                     # calibrate, then run
    python arm_wifi.py --dx0 -232 --dy0 -38
    python arm_wifi.py --host 192.168.4.1 --step 10

Connect the PC to the ESP8266-ARM access point first, or set --host to the
board's address on your own network if you flashed it in station mode.

Only these events are wired so far:
    left confirm  -> stand rotates left
    right confirm -> stand rotates right
    fetch         -> claw opens
    stop          -> claw closes

up / down / back are left as no-ops until the arm's geometry is settled.
"""

import argparse
import time
import urllib.error
import urllib.request

from eye_test import ArmActions, main

DEFAULT_HOST = "192.168.244.157"
DEFAULT_STEP = 10          # degrees per confirmed direction
CLAW_OPEN = 40             # servo angle that opens the gripper
CLAW_CLOSED = 120          # servo angle that closes the gripper

TIMEOUT = 0.8              # seconds; keep short so a dead network cannot stall gaze
NO_REPLY_WARN = 2.0        # say "waiting" every time this many seconds pass unanswered
WAITING_WORD = "waiting"


class WifiArm(ArmActions):
    """Sends HTTP requests to the ESP8266 when the gaze controller fires."""

    def __init__(self, host=DEFAULT_HOST, step=DEFAULT_STEP):
        self.base = f"http://{host}"
        self.step = step
        self.reachable = None      # None = untested, True/False after first call
        self.last_error = ""
        self.unanswered_since = None   # when contact was lost; None = healthy
        self.last_warning = 0.0        # when we last said "waiting"

    # ------------------------------------------------------------ transport
    def _get(self, path):
        url = self.base + path
        now = time.time()
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
                body = r.read().decode("utf-8", "replace")
            # contact restored: drop the accumulation
            if self.unanswered_since is not None:
                print(f"[arm] contact restored after {now - self.unanswered_since:.1f}s")
            self.unanswered_since = None
            self.reachable = True
            return body
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if self.reachable is not False:
                self.last_error = str(e)
                print(f"[arm] cannot reach {self.base} ({e})")
            self.reachable = False

            # start the clock on first failure, then warn once every NO_REPLY_WARN
            if self.unanswered_since is None:
                self.unanswered_since = now
                self.last_warning = now
            elapsed = now - self.unanswered_since
            if elapsed >= NO_REPLY_WARN and (now - self.last_warning) >= NO_REPLY_WARN:
                from eye_test import speak
                speak(WAITING_WORD)
                self.last_warning = now
                print(f"[arm] still unreachable after {elapsed:.1f}s")
            return None

    def _step_channel(self, channel, degrees):
        self._get(f"/step?ch={channel}&d={degrees}")

    def _set_channel(self, channel, angle):
        self._get(f"/set?ch={channel}&a={angle}")

    # ------------------------------------------------------------ hooks
    def on_left(self):
        # the base servo is mounted mirrored, so a positive step turns it right
        print(f"[arm] left  -> stand {self.step:+d}")
        self._step_channel("stand", -self.step)

    def on_right(self):
        print(f"[arm] right -> stand {self.step:+d}")
        self._step_channel("stand", +self.step)

    def on_fetch(self):
        print(f"[arm] claw open -> {CLAW_OPEN}")
        self._set_channel("hand", CLAW_OPEN)

    def on_stop(self):
        print(f"[arm] claw close -> {CLAW_CLOSED}")
        self._set_channel("hand", CLAW_CLOSED)

    # deliberately unimplemented for now
    def on_up(self):
        print("[arm] up: not wired yet")

    def on_down(self):
        print("[arm] down: not wired yet")

    def on_back(self):
        # the back gesture retracts the claw; it is not a reset
        print("[arm] back -> retract claw")
        self._set_channel("hand", CLAW_OPEN)

    def on_shutdown(self):
        # the program is closing: every joint goes home
        print("[arm] shutdown -> home")
        self._get("/home")

    def on_centre(self):
        pass          # too noisy to send to the arm

    def on_system_on(self):
        print("[arm] system on")

    def on_system_off(self):
        # switching the system off parks the arm, so it does not sit in a
        # half-extended pose while nothing is watching the eyes
        print("[arm] system off -> home")
        self._get("/home")


def parse_arm_args():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--host", default=DEFAULT_HOST,
                   help="ESP8266 address (default 192.168.4.1 on its own AP)")
    p.add_argument("--step", type=int, default=DEFAULT_STEP,
                   help="degrees of base rotation per confirmed direction")
    known, rest = p.parse_known_args()
    return known, rest


def main_wifi():
    arm_args, rest = parse_arm_args()

    # hand the remaining flags back to eye_test's own parser
    import sys
    sys.argv = [sys.argv[0]] + rest

    arm = WifiArm(host=arm_args.host, step=arm_args.step)
    print(f"arm endpoint : {arm.base}")
    print(f"base step    : {arm.step} deg")
    print(f"claw open/close : {CLAW_OPEN} / {CLAW_CLOSED} deg")
    print()

    # a quick reachability check, so a wrong IP shows up before calibration
    status = arm._get("/status")
    if status:
        print(f"arm answered : {status}")
    else:
        print("warning: no answer from the arm. Check the WiFi connection;")
        print("         the gaze test will still run, it just will not move anything.")
    print()

    main(arm)


if __name__ == "__main__":
    main_wifi()
