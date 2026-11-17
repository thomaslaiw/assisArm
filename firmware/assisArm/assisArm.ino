/*
 * assisArm - ESP8266 servo controller
 *
 * Board : LOLIN(WEMOS) D1 mini (ESP8266)
 * Servos: 5 x MG996R
 *
 *   stand  (base rotate)   GPIO 14  D5
 *   arm 1  (vertical)      GPIO 4   D2
 *   arm 2  (vertical)      GPIO 0   D3
 *   arm 3  (vertical)      GPIO 16  D0
 *   hand   (gripper)       GPIO 5   D1
 *
 * The board starts a WiFi access point. Join it, then open
 *   http://192.168.4.1/
 * and drag the sliders. No router, no password in the code.
 *
 * HTTP API (same server, useful from Python later):
 *   GET /status                    -> JSON of every angle
 *   GET /set?ch=stand&a=90         -> set one channel, 0-180
 *   GET /home                      -> all channels to HOME_ANGLE
 *   GET /step?ch=arm1&d=5          -> nudge one channel by d degrees
 *
 * POWER: five MG996R need a separate 5-6V supply of at least 5A.
 * Do NOT feed them from the board's 5V pin - it will brown out and reset.
 * Connect the servo supply ground to the board ground, common ground only.
 */

#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <Servo.h>

// ---------------------------------------------------------------- config
const char *AP_SSID = "ESP8266-ARM";
const char *AP_PASS = "88888888";          // at least 8 chars, or "" for open

// If you would rather join your home WiFi than open an access point,
// fill these in and set USE_STA to true.
const bool USE_STA = true;
const char *STA_SSID = "Galaxy A55 5G 78A5";
const char *STA_PASS = "zaq12wsx";

// ---------------------------------------------------------------- servos
struct Channel {
  const char *name;
  uint8_t pin;
  int angle;          // current angle, degrees
  int minAngle;
  int maxAngle;
  int homeAngle;
};

Channel channels[] = {
  //  name      pin            angle  min  max  home
  { "stand",   12,            90,    10, 170,  90 },
  { "arm1",     4,           150,    10, 170, 150 },   // home +60 from centre
  { "arm2",     0,            60,    10, 170,  60 },   // home -30 from centre
  { "arm3",    14,            60,    10, 170,  60 },   // home -30 from centre
  { "hand",     5,            90,    10, 170,  90 },
};
const int CH_COUNT = sizeof(channels) / sizeof(channels[0]);

Servo servos[CH_COUNT];

// MG996R is happy at 50 Hz with 500-2500 us pulses.
const int SERVO_MIN_US = 500;
const int SERVO_MAX_US = 2500;

ESP8266WebServer server(80);

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
  h1 { font-size: 18px; font-weight: 600; margin: 0 0 16px; }
  .ch { background: #1e2126; border-radius: 10px; padding: 12px 14px;
        margin-bottom: 10px; }
  .row { display: flex; align-items: center; gap: 10px; }
  .name { width: 70px; font-size: 14px; color: #9aa0a6; }
  .val { width: 48px; text-align: right; font-variant-numeric: tabular-nums; }
  input[type=range] { flex: 1; }
  button { background: #2d3138; color: #e8eaed; border: 0; border-radius: 6px;
           padding: 6px 12px; font-size: 14px; }
  button:active { background: #3c4148; }
  #home { width: 100%; margin-top: 6px; padding: 10px; }
</style>
</head><body>
<h1>assisArm</h1>
<div id="list"></div>
<button id="home" onclick="fetch('/home')">home all</button>
<script>
const CH = ["stand","arm1","arm2","arm3","hand"];
const list = document.getElementById('list');
CH.forEach(name => {
  const d = document.createElement('div');
  d.className = 'ch';
  d.innerHTML = `<div class="row">
      <span class="name">${name}</span>
      <input type="range" min="10" max="170" value="90" id="s_${name}">
      <span class="val" id="v_${name}">90</span>
    </div>`;
  list.appendChild(d);
  const s = d.querySelector('input');
  const v = d.querySelector('.val');
  s.addEventListener('input', () => { v.textContent = s.value; });
  s.addEventListener('change', () => {
    fetch(`/set?ch=${name}&a=${s.value}`);
  });
});
function refresh() {
  fetch('/status').then(r => r.json()).then(j => {
    CH.forEach(n => {
      const s = document.getElementById('s_' + n);
      const v = document.getElementById('v_' + n);
      if (!s) return;
      s.value = j[n];
      v.textContent = j[n];
    });
  });
}
refresh();
setInterval(refresh, 2000);
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
  out += "}";
  server.send(200, "application/json", out);
}

void handleSet() {
  String name = server.arg("ch");
  int idx = channelIndex(name);
  if (idx < 0) {
    server.send(400, "application/json", "{\"error\":\"unknown channel\"}");
    return;
  }
  int a = server.arg("a").toInt();
  moveTo(idx, a);
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
  int d = server.arg("d").toInt();
  moveTo(idx, channels[idx].angle + d);
  server.send(200, "application/json",
              "{\"ch\":\"" + name + "\",\"a\":" + String(channels[idx].angle) + "}");
}

void handleHome() {
  for (int i = 0; i < CH_COUNT; i++) moveTo(i, channels[i].homeAngle);
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

  // attach servos before WiFi so they hold their home position
  for (int i = 0; i < CH_COUNT; i++) {
    servos[i].attach(channels[i].pin, SERVO_MIN_US, SERVO_MAX_US);
    channels[i].angle = channels[i].homeAngle;
    servos[i].write(channels[i].angle);
  }
  delay(400);

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
      Serial.print("access point: ");
      Serial.println(AP_SSID);
      Serial.print("open http://");
      Serial.println(WiFi.softAPIP());
    }
  } else {
    WiFi.mode(WIFI_AP);
    WiFi.softAP(AP_SSID, AP_PASS);
    Serial.print("access point: ");
    Serial.println(AP_SSID);
    Serial.print("open http://");
    Serial.println(WiFi.softAPIP());
  }

  server.on("/", handleIndex);
  server.on("/status", handleStatus);
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
