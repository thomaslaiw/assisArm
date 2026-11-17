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
import argparse
import threading
import time
import urllib.error
import urllib.request

from eye_test import ArmActions, main

DEFAULT_HOST = "192.168.151.157"
DEFAULT_STEP = 10          # degrees per confirmed left/right
POSE_STEP = 5              # per confirmed up/down, in pose units
FETCH_STEP = 5             # per fetch tick while extending
FETCH_TICK = 0.3           # seconds between extend calls during a fetch
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
        self._fetch_stop = threading.Event()   # set to end the extend loop
        self._fetch_thread = None

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

    def _pose(self, path):
        return self._get(path)

    def _stop_fetch_loop(self):
        """Tell the extend loop to finish, and wait for it."""
        self._fetch_stop.set()
        if self._fetch_thread is not None:
            self._fetch_thread.join(timeout=1.5)
            self._fetch_thread = None

    def _run_fetch_loop(self):
        """
        Walk f down toward 0 while the fetch is active.

        The board clamps f at 0, so the loop simply stops when the pose stops
        changing rather than needing to know the limit itself.
        """
        last = None
        while not self._fetch_stop.is_set():
            self._get(f"/extend?d={FETCH_STEP}")
            now_pose = self._get("/pose")
            if now_pose is not None and now_pose == last:
                print("[arm] fully extended")
                break
            last = now_pose
            if self._fetch_stop.wait(FETCH_TICK):
                break

    # ------------------------------------------------------------ hooks
    def on_left(self):
        # the base servo is mounted mirrored, so a positive step turns it right
        print(f"[arm] left  -> stand {self.step:+d}")
        self._step_channel("stand", -self.step)

    def on_right(self):
        print(f"[arm] right -> stand {self.step:+d}")
        self._step_channel("stand", +self.step)

    def on_fetch(self):
        """Open the claw, then keep reaching out until the fetch is stopped."""
        print(f"[arm] fetch -> claw {CLAW_OPEN}, extending every {FETCH_TICK}s")
        self._set_channel("hand", CLAW_OPEN)

        self._stop_fetch_loop()                 # in case one was still running
        self._fetch_stop.clear()
        self._fetch_thread = threading.Thread(
            target=self._run_fetch_loop, daemon=True)
        self._fetch_thread.start()

    def on_stop(self):
        """End the reach and close the claw."""
        print(f"[arm] stop -> claw {CLAW_CLOSED}, extension halted")
        self._stop_fetch_loop()
        self._set_channel("hand", CLAW_CLOSED)

    def on_up(self):
        print(f"[arm] up   v -{POSE_STEP}")
        self._pose(f"/raise?d={POSE_STEP}")

    def on_down(self):
        print(f"[arm] down v +{POSE_STEP}")
        self._pose(f"/lower?d={POSE_STEP}")

    def on_back(self):
        # nothing wired yet: the back gesture still needs a physical decision
        print("[arm] back (no action yet)")

    def on_shutdown(self):
        # the program is closing: stop any reach in flight, then park
        print("[arm] shutdown -> home")
        self._stop_fetch_loop()
        self._get("/home")

    def on_centre(self):
        pass          # too noisy to send to the arm

    def on_system_on(self):
        print("[arm] system on")

    def on_system_off(self):
        # switching the system off parks the arm, so it does not sit in a
        # half-extended pose while nothing is watching the eyes
        print("[arm] system off -> home")
        self._stop_fetch_loop()
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
