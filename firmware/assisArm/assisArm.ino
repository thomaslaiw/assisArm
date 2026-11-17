/*
 * assisArm - ESP8266 servo controller
 *
 * Board : LOLIN(WEMOS) D1 mini (ESP8266)
 * Servos: 5 x MG996R
 *
 *   stand   base rotate   GPIO 12  D6
 *   arm1    vertical      GPIO 4   D2
 *   arm2    vertical      GPIO 0   D3
 *   arm3    vertical      GPIO 14  D5
 *   hand    gripper       GPIO 5   D1
 *
 * ---------------------------------------------------------------- pose
 *
 * The three vertical joints are driven by one pose, not by raw angles.
 * Two numbers describe everything:
 *
 *   f = how far the arm is extended    0 (folded) .. 40 (fully out)
 *   v = how low the arm is             0 (top)    .. 35 (bottom)
 *
 * and the servo angles follow from them:
 *
 *   arm3 = 90 - f
 *   arm2 = 90 - (f + 8 + v)
 *   arm1 = 90 + 90 - v
 *
 * Set f and v, and the angles are computed. This keeps the joints in step
 * no matter whether the request came from the web page, the eye tracker, or
 * a person poking the API with curl.
 *
 * ---------------------------------------------------------------- HTTP
 *
 *   GET /                     slider page, one per joint, plus the pose
 *   GET /status               every servo angle, plus f and v
 *   GET /pose                 just f and v
 *
 *   pose commands (preferred - they keep the joints coordinated)
 *   GET /extend?d=5           f += d   (clamped 0..40)
 *   GET /retract?d=5          f -= d
 *   GET /lower?d=5            v += d   (clamped 0..35)
 *   GET /raise?d=5            v -= d
 *   GET /reset                f = 0, v = 0, stand and hand home
 *
 *   direct control (for the sliders and for debugging)
 *   GET /set?ch=stand&a=90    one servo, clamped to its own limits
 *   GET /step?ch=stand&d=10   nudge one servo
 *   GET /home                 every servo to its home angle, f = v = 0
 *
 * POWER: five MG996R draw up to 2.5 A each at stall. They need their own
 * 5-6 V supply of at least 5 A with a common ground to the board. Feeding
 * them from the board's 5 V pin browns out the board and resets it.
 */

#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <Servo.h>

// ---------------------------------------------------------------- config
const char *AP_SSID = "ESP8266-ARM";
const char *AP_PASS = "88888888";

const bool USE_STA = true;
const char *STA_SSID = "Galaxy A55 5G 78A5";
const char *STA_PASS = "zaq12wsx";

// ---------------------------------------------------------------- servos
struct Channel {
  const char *name;
  uint8_t pin;
  int angle;
  int minAngle;
  int maxAngle;
  int homeAngle;
};

Channel channels[] = {
  //  name      pin          angle  min  max  home
  { "stand",   12,            90,   10, 170,  90 },
  { "arm1",     4,           180,    0, 180, 180 },
  { "arm2",     0,            82,    0, 180,  82 },
  { "arm3",    14,            90,    0, 180,  90 },
  { "hand",     5,            90,   10, 170,  90 },
};
const int CH_COUNT = sizeof(channels) / sizeof(channels[0]);

Servo servos[CH_COUNT];

const int SERVO_MIN_US = 500;
const int SERVO_MAX_US = 2500;

ESP8266WebServer server(80);

// ---------------------------------------------------------------- pose state
int poseF = 40;         // 0 folded .. 40 fully extended
int poseV = 15;         // 0 top .. 30 bottom

const int F_MIN = 0;
const int F_MAX = 40;
const int V_MIN = 0;
const int V_MAX = 30;

// ---------------------------------------------------------------- helpers
int channelIndex(const String &name) {
  for (int i = 0; i < CH_COUNT; i++) {
    if (name.equalsIgnoreCase(channels[i].name)) return i;
  }
  return -1;
}

void applyAngle(int idx) {
  Channel &c = channels[idx];
  if (c.angle < c.minAngle) c.angle = c.minAngle;
  if (c.angle > c.maxAngle) c.angle = c.maxAngle;
  servos[idx].write(c.angle);
}

void moveTo(int idx, int angle) {
  channels[idx].angle = angle;
  applyAngle(idx);
}

void moveByName(const char *name, int angle) {
  int idx = channelIndex(String(name));
  if (idx >= 0) moveTo(idx, angle);
}

int clampInt(int v, int lo, int hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

/* Push f and v into the three vertical joints and write them out. */
void applyPose() {
  poseF = clampInt(poseF, F_MIN, F_MAX);
  poseV = clampInt(poseV, V_MIN, V_MAX);

  moveByName("arm3", 90 - poseF);
  moveByName("arm2", 90 - (poseF + 8 + poseV));
  moveByName("arm1", 90 + 90 - poseV);
}

String poseJson() {
  return "{\"f\":" + String(poseF) + ",\"v\":" + String(poseV) + "}";
}

// ---------------------------------------------------------------- web page
const char INDEX_HTML[] PROGMEM = R"HTML(
<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>assisArm</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; padding: 16px;
         background: #14161a; color: #e8eaed; }
  h1 { font-size: 17px; font-weight: 600; margin: 0 0 4px; }
  h2 { font-size: 13px; font-weight: 500; color: #9aa0a6;
       margin: 22px 0 8px; text-transform: uppercase; letter-spacing: .05em; }
  .ch { background: #1e2126; border-radius: 10px; padding: 12px 14px;
        margin-bottom: 10px; }
  .row { display: flex; align-items: center; gap: 10px; }
  .name { width: 62px; font-size: 14px; color: #9aa0a6; }
  .val { width: 46px; text-align: right; font-variant-numeric: tabular-nums; }
  input[type=range] { flex: 1; }
  button { background: #2d3138; color: #e8eaed; border: 0; border-radius: 8px;
           padding: 12px 16px; font-size: 15px; }
  button:active { background: #3c4148; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  #pose { font-family: ui-monospace, monospace; font-size: 13px;
          color: #9aa0a6; margin-top: 14px; }
</style>
</head><body>
<h1>assisArm</h1>

<h2>pose</h2>
<div class="grid2">
  <button onclick="cmd('extend?d=5')">extend +5</button>
  <button onclick="cmd('retract?d=5')">retract -5</button>
  <button onclick="cmd('raise?d=5')">raise (up)</button>
  <button onclick="cmd('lower?d=5')">lower (down)</button>
</div>
<div id="pose">f=0 v=0</div>

<h2>servos</h2>
<div id="list"></div>

<h2>all</h2>
<div class="grid2">
  <button onclick="cmd('home')">home all</button>
  <button onclick="cmd('reset')">reset pose</button>
</div>

<script>
const CH = ["stand","arm1","arm2","arm3","hand"];
const list = document.getElementById('list');
CH.forEach(name => {
  const d = document.createElement('div');
  d.className = 'ch';
  d.innerHTML = `<div class="row">
      <span class="name">${name}</span>
      <input type="range" min="0" max="180" value="90" id="s_${name}">
      <span class="val" id="v_${name}">90</span>
    </div>`;
  list.appendChild(d);
  const s = d.querySelector('input');
  const v = d.querySelector('.val');
  s.addEventListener('input', () => { v.textContent = s.value; });
  s.addEventListener('change', () => { fetch(`/set?ch=${name}&a=${s.value}`); });
});

function cmd(path) { fetch('/' + path).then(refresh); }

function refresh() {
  fetch('/status').then(r => r.json()).then(j => {
    CH.forEach(n => {
      const s = document.getElementById('s_' + n);
      const v = document.getElementById('v_' + n);
      if (!s) return;
      s.value = j[n];
      v.textContent = j[n];
    });
    document.getElementById('pose').textContent = `f=${j.f}  v=${j.v}`;
  });
}
refresh();
setInterval(refresh, 1500);
</script>
</body></html>
)HTML";

// ---------------------------------------------------------------- routes
void handleIndex() {
  server.send_P(200, "text/html", INDEX_HTML);
}

void handleStatus() {
  String out = "{";
  for (int i = 0; i < CH_COUNT; i++) {
    if (i) out += ",";
    out += "\"" + String(channels[i].name) + "\":" + String(channels[i].angle);
  }
  out += ",\"f\":" + String(poseF) + ",\"v\":" + String(poseV) + "}";
  server.send(200, "application/json", out);
}

void handlePose() {
  server.send(200, "application/json", poseJson());
}

void handleExtend() {
  poseF += server.arg("d").toInt();
  if (poseF == 0) poseF = 5;                 // d missing: step by 5
  applyPose();
  server.send(200, "application/json", poseJson());
}

void handleRetract() {
  poseF -= server.arg("d").toInt();
  if (poseF == 0) poseF = -5;
  applyPose();
  server.send(200, "application/json", poseJson());
}

void handleLower() {
  poseV += server.arg("d").toInt();
  if (poseV == 0) poseV = 5;
  applyPose();
  server.send(200, "application/json", poseJson());
}

void handleRaise() {
  poseV -= server.arg("d").toInt();
  if (poseV == 0) poseV = -5;
  applyPose();
  server.send(200, "application/json", poseJson());
}

void handleReset() {
  poseF = 40;
  poseV = 15;
  applyPose();
  moveByName("stand", 90);
  moveByName("hand", 90);
  server.send(200, "application/json", poseJson());
}

void handleSet() {
  String name = server.arg("ch");
  int idx = channelIndex(name);
  if (idx < 0) {
    server.send(400, "application/json", "{\"error\":\"unknown channel\"}");
    return;
  }
  moveTo(idx, server.arg("a").toInt());
  server.send(200, "application/json",
              "{\"ch\":\"" + name + "\",\"a\":" + String(channels[idx].angle) + "}");
}

void handleStep() {
  String name = server.arg("ch");
  int idx = channelIndex(name);
  if (idx < 0) {
    server.send(400, "application/json", "{\"error\":\"unknown channel\"}");
    return;
  }
  moveTo(idx, channels[idx].angle + server.arg("d").toInt());
  server.send(200, "application/json",
              "{\"ch\":\"" + name + "\",\"a\":" + String(channels[idx].angle) + "}");
}

void handleHome() {
  poseF = 40;
  poseV = 15;
  applyPose();
  moveByName("stand", 90);
  moveByName("hand", 90);
  server.send(200, "application/json", "{\"ok\":true}");
}

void handleNotFound() {
  server.send(404, "text/plain", "not found");
}

// ---------------------------------------------------------------- setup
void setup() {
  Serial.begin(115200);
  Serial.println();
  Serial.println("assisArm servo controller");

  for (int i = 0; i < CH_COUNT; i++) {
    servos[i].attach(channels[i].pin, SERVO_MIN_US, SERVO_MAX_US);
    channels[i].angle = channels[i].homeAngle;
    servos[i].write(channels[i].angle);
  }
  delay(400);
  applyPose();          // the pose defaults may differ from the home angles

  if (USE_STA) {
    WiFi.mode(WIFI_STA);
    WiFi.begin(STA_SSID, STA_PASS);
    Serial.print("joining ");
    Serial.println(STA_SSID);
    for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) {
      delay(500);
      Serial.print(".");
    }
    Serial.println();
    if (WiFi.status() == WL_CONNECTED) {
      Serial.print("ready at http://");
      Serial.println(WiFi.localIP());
    } else {
      Serial.println("could not join, falling back to access point");
      WiFi.mode(WIFI_AP);
      WiFi.softAP(AP_SSID, AP_PASS);
      Serial.print("open http://");
      Serial.println(WiFi.softAPIP());
    }
  } else {
    WiFi.mode(WIFI_AP);
    WiFi.softAP(AP_SSID, AP_PASS);
    Serial.print("open http://");
    Serial.println(WiFi.softAPIP());
  }

  server.on("/", handleIndex);
  server.on("/status", handleStatus);
  server.on("/pose", handlePose);
  server.on("/extend", handleExtend);
  server.on("/retract", handleRetract);
  server.on("/lower", handleLower);
  server.on("/raise", handleRaise);
  server.on("/reset", handleReset);
  server.on("/set", handleSet);
  server.on("/step", handleStep);
  server.on("/home", handleHome);
  server.onNotFound(handleNotFound);
  server.begin();
  Serial.println("http server started");
}

void loop() {
  server.handleClient();
}
