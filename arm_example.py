"""
Example of wiring a robotic arm into the gaze controller.

The gaze script never needs to be edited for this. Subclass ArmActions,
override the hooks you care about, and hand the instance to main().

Run it exactly like the normal script:

    python arm_example.py
    python arm_example.py --dx0 -232 --dy0 -38

Nothing here talks to hardware yet: the methods print and sleep so the timing
can be checked before the real arm SDK is available.
"""

import time

from eye_test import ArmActions, main


class DemoArm(ArmActions):
    """Stand-in for the arm. Replace the bodies with real commands."""

    STEP_PX = 50          # how far one confirmed direction should move

    def _move(self, axis, amount):
        print(f"[arm] move {axis} by {amount:+d}")
        time.sleep(0.4)   # pretend the arm is travelling

    def on_left(self):
        self._move("x", -self.STEP_PX)

    def on_right(self):
        self._move("x", +self.STEP_PX)

    def on_up(self):
        self._move("z", +self.STEP_PX)

    def on_down(self):
        self._move("z", -self.STEP_PX)

    def on_centre(self):
        print("[arm] eyes at rest, holding position")

    def on_fetch(self):
        print("[arm] FETCH: opening gripper and approaching")

    def on_stop(self):
        print("[arm] STOP: freezing current motion")

    def on_back(self):
        print("[arm] BACK: retracting to home")

    def on_system_on(self):
        print("[arm] SYSTEM ON: arm enabled")

    def on_system_off(self):
        print("[arm] SYSTEM OFF: arm parked and disabled")


if __name__ == "__main__":
    main(DemoArm())
