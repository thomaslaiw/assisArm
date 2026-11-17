"""
Local web remote for the assisArm.

Runs a small server on this PC that proxies to the ESP8266, so the browser
only ever talks to localhost. When the phone hotspot hands out a new address
you change ARM_HOST in one place, not in every bookmark.

    python arm_web.py
    python arm_web.py --arm 192.168.151.157 --port 8080

Then open http://localhost:8080/ on this machine, or
http://<this-pc-ip>:8080/ from a phone on the same network.

Buttons: up, down, left, right, fetch, stop, home. Each press is a single
step, the same size the eye tracker uses.
"""

import argparse
import json
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARM_HOST = "192.168.151.157"
LISTEN_PORT = 8080

STEP_SIDE = 10        # degrees per left/right press
STEP_ARM = 4          # degrees per up/down press, per joint
CLAW_OPEN = 40
CLAW_CLOSED = 120
TIMEOUT = 2.0

PAGE = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>assisArm</title>
<style>
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin: 0; padding: 16px; font-family: system-ui, sans-serif;
         background: #14161a; color: #e8eaed; user-select: none; }
  h1 { font-size: 16px; font-weight: 600; margin: 0 0 4px; color: #9aa0a6; }
  .grid { display: grid; grid-template-columns: repeat(3, 1fr);
          gap: 10px; max-width: 420px; margin: 16px auto; }
  button { border: 0; border-radius: 12px; background: #262a31; color: #e8eaed;
           font-size: 22px; padding: 26px 0; font-weight: 500; }
  button:active { background: #3a4048; }
  .up    { grid-column: 2; grid-row: 1; }
  .left  { grid-column: 1; grid-row: 2; }
  .stop  { grid-column: 2; grid-row: 2; background: #4a2626; font-size: 16px; }
  .right { grid-column: 3; grid-row: 2; }
  .down  { grid-column: 2; grid-row: 3; }
  .wide  { grid-column: 1 / span 3; padding: 18px 0; font-size: 16px; }
  .row   { display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
           max-width: 420px; margin: 10px auto; }
  .row button { font-size: 16px; padding: 18px 0; }
  #status { max-width: 420px; margin: 18px auto 0; padding: 12px;
            background: #1e2126; border-radius: 10px; font-size: 13px;
            font-family: ui-monospace, monospace; color: #9aa0a6;
            white-space: pre-wrap; line-height: 1.6; }
  .err { color: #ff6b6b; }
</style>
</head><body>
<h1>assisArm</h1>
<div class="grid">
  <button class="up"    onclick="go('up')">&#9650;</button>
  <button class="left"  onclick="go('left')">&#9664;</button>
  <button class="stop"  onclick="go('stop')">STOP</button>
  <button class="right" onclick="go('right')">&#9654;</button>
  <button class="down"  onclick="go('down')">&#9660;</button>
</div>
<div class="row">
  <button onclick="go('fetch')">fetch (open)</button>
  <button onclick="go('close')">close claw</button>
</div>
<div class="row">
  <button onclick="go('home')">HOME</button>
  <button onclick="refresh()">refresh</button>
</div>
<div id="status">connecting...</div>
<script>
async function go(action) {
  try {
    const r = await fetch('/do?action=' + action, {cache: 'no-store'});
    const j = await r.json();
    render(j);
  } catch (e) {
    document.getElementById('status').innerHTML =
      '<span class="err">' + e.message + '</span>';
  }
}
function render(j) {
  const el = document.getElementById('status');
  if (j.error) { el.innerHTML = '<span class="err">' + j.error + '</span>'; return; }
  el.textContent = j.angles ? JSON.stringify(j.angles, null, 0) : '(no data)';
}
async function refresh() {
  try {
    const r = await fetch('/status', {cache: 'no-store'});
    render(await r.json());
  } catch (e) {
    document.getElementById('status').innerHTML =
      '<span class="err">' + e.message + '</span>';
  }
}
refresh();
setInterval(refresh, 3000);
</script>
</body></html>"""


def arm_get(path):
    """Call the ESP8266 and return parsed JSON, or raise."""
    url = f"http://{ARM_HOST}{path}"
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def do_action(action):
    """Translate a button press into one or more arm calls."""
    if action == "up":
        # arm1 up is +, arm2 up is -: the joints are mounted in opposition
        arm_get("/step?ch=arm1&d=+{}".format(STEP_ARM))
        return arm_get("/step?ch=arm2&d=-{}".format(STEP_ARM))
    if action == "down":
        arm_get("/step?ch=arm1&d=-{}".format(STEP_ARM))
        return arm_get("/step?ch=arm2&d=+{}".format(STEP_ARM))
    if action == "left":
        return arm_get("/step?ch=stand&d=-{}".format(STEP_SIDE))
    if action == "right":
        return arm_get("/step?ch=stand&d=+{}".format(STEP_SIDE))
    if action == "fetch":
        return arm_get("/set?ch=hand&a={}".format(CLAW_OPEN))
    if action == "close":
        return arm_get("/set?ch=hand&a={}".format(CLAW_CLOSED))
    if action == "stop":
        return arm_get("/set?ch=hand&a={}".format(CLAW_CLOSED))
    if action == "home":
        return arm_get("/home")
    raise ValueError("unknown action")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass                     # keep the console quiet

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, PAGE, "text/html")
            return

        if parsed.path == "/status":
            try:
                self._send(200, json.dumps({"angles": arm_get("/status")}))
            except Exception as e:
                self._send(200, json.dumps({"error": f"arm unreachable: {e}"}))
            return

        if parsed.path == "/do":
            action = (parse_qs(parsed.query).get("action") or [""])[0]
            try:
                result = do_action(action)
                angles = arm_get("/status")
                self._send(200, json.dumps({"ok": True, "last": result,
                                            "angles": angles}))
            except Exception as e:
                self._send(200, json.dumps({"error": f"{action}: {e}"}))
            return

        self._send(404, json.dumps({"error": "not found"}))


def main():
    global ARM_HOST, LISTEN_PORT

    p = argparse.ArgumentParser(description="Local web remote for assisArm")
    p.add_argument("--arm", default=ARM_HOST, help="ESP8266 address")
    p.add_argument("--port", type=int, default=LISTEN_PORT, help="port to listen on")
    p.add_argument("--host", default="0.0.0.0",
                   help="interface to bind (0.0.0.0 lets phones connect)")
    args = p.parse_args()

    ARM_HOST = args.arm
    LISTEN_PORT = args.port

    try:
        angles = arm_get("/status")
        print(f"arm ok   : {angles}")
    except Exception as e:
        print(f"arm warn : unreachable ({e})")
        print("           the page will still load, buttons will report the error")

    print()
    print(f"open     : http://localhost:{LISTEN_PORT}/")
    print(f"from phone: http://<this-pc-ip>:{LISTEN_PORT}/")
    print("ctrl+C to stop")
    print()

    server = ThreadingHTTPServer((args.host, LISTEN_PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
