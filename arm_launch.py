"""
One-click launcher for the arm.

The phone hotspot hands out a fresh subnet every time it restarts, so the
board's address moves and anything hard-coded goes stale. This script finds
the board itself and then starts whatever you asked for.

    python arm_launch.py            # find the arm, then run the eye tracker
    python arm_launch.py --web      # find the arm, then open the web remote
    python arm_launch.py --find     # just report where the arm is
    python arm_launch.py --host 192.168.1.50   # skip the scan

The scan sweeps the /24 this PC is on, so it needs no configuration and works
on any network the arm happens to join.
"""

import argparse
import concurrent.futures
import socket
import subprocess
import sys
import urllib.error
import urllib.request

SCAN_TIMEOUT = 0.6
SCAN_WORKERS = 96
PROBE_PATH = "/status"
EXPECTED_KEYS = ("stand", "arm1", "hand")


def local_subnet():
    """The /24 this machine is on, discovered without sending any packets."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))       # TEST-NET, never actually reached
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    if not ip or ip.startswith(("127.", "169.254.")):
        return None
    return ip.rsplit(".", 1)[0]


def probe(ip):
    """Return the body if this address looks like our arm, else None."""
    try:
        with urllib.request.urlopen(f"http://{ip}{PROBE_PATH}",
                                    timeout=SCAN_TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception:
        return None
    if all(k in body for k in EXPECTED_KEYS):
        return body
    return None


def find_arm(quiet=False):
    """Sweep the local /24 and return (ip, status) for the first arm found."""
    sub = local_subnet()
    if sub is None:
        if not quiet:
            print("no usable network interface; connect to WiFi first")
        return None, None

    if not quiet:
        print(f"scanning {sub}.0/24 for the arm...")

    addresses = [f"{sub}.{i}" for i in range(1, 255)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        for ip, body in zip(addresses, ex.map(probe, addresses)):
            if body is not None:
                if not quiet:
                    print(f"found arm at {ip}")
                    print(f"  {body}")
                return ip, body

    if not quiet:
        print("no arm answered on this network.")
        print("  - is the board powered?")
        print("  - is it joined to the same hotspot as this PC?")
    return None, None


def run(command):
    print()
    print("$ " + " ".join(command))
    print()
    try:
        return subprocess.call(command)
    except KeyboardInterrupt:
        return 0


def main():
    p = argparse.ArgumentParser(description="Find the arm, then run something")
    p.add_argument("--host", default=None,
                   help="skip the scan and use this address")
    p.add_argument("--web", action="store_true",
                   help="start the web remote instead of the eye tracker")
    p.add_argument("--find", action="store_true",
                   help="only report the address, do not start anything")
    p.add_argument("--step", type=int, default=None,
                   help="base rotation per confirmed direction")
    args, rest = p.parse_known_args()

    if args.host:
        host = args.host
        print(f"using given host {host}")
    else:
        host, _ = find_arm()
        if host is None:
            if args.find:
                return 1
            print()
            print("cannot start without the arm. Fix the connection and retry,")
            print("or pass --host if you already know the address.")
            return 1

    if args.find:
        return 0

    here = __file__.rsplit("\\", 1)[0] if "\\" in __file__ else "."
    script = "arm_web.py" if args.web else "arm_wifi.py"
    command = [sys.executable, f"{here}\\{script}", "--host", host]
    if args.step is not None and not args.web:
        command += ["--step", str(args.step)]
    command += rest
    return run(command)


if __name__ == "__main__":
    sys.exit(main())
