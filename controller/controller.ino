#include <WiFi.h>
#include <WiFiClient.h>
#include <WiFiServer.h>
#include <BleGamepad.h>

constexpr bool DEBUG_SERIAL = true;

#define DBG_BEGIN(baud) do { if (DEBUG_SERIAL) Serial.begin(baud); } while (0)
#define DBG_PRINT(x)    do { if (DEBUG_SERIAL) Serial.print(x); } while (0)
#define DBG_PRINTLN(x)  do { if (DEBUG_SERIAL) Serial.println(x); } while (0)

BleGamepad bleGamepad("IControlThem p1", "MAIM", 100);

// =========================
// Wi-Fi settings
// =========================
constexpr char WIFI_SSID[] = "WIFI_SSID";
constexpr char WIFI_PASS[] = "WIFI_PASS";
constexpr uint16_t HTTP_PORT = 4210;
constexpr char WATCHER_HOST[] = "WATCHER_HOST";
constexpr uint16_t WATCHER_PORT = 42069;
constexpr uint32_t HEARTBEAT_INTERVAL_MS = 5000;
constexpr char CONTROLLER_ID[] = "p1";   // Change per controller: p1, p2, p3, etc.
constexpr char CONTROLLER_NAME[] = "IControlThem p1";

// =========================
// D-pad pins (digital -> BLE hat switch)
// =========================
constexpr uint8_t PIN_UP    = 16;
constexpr uint8_t PIN_RIGHT = 17;
constexpr uint8_t PIN_DOWN  = 18;
constexpr uint8_t PIN_LEFT  = 19;

// =========================
// Buttons in this exact order become BUTTON_1..BUTTON_8
// 1=B, 2=A, 3=Y, 4=X, 5=L, 6=R, 7=Select, 8=Start
// D-pad uses the BLE hat switch report
// =========================
constexpr uint8_t BTN_COUNT = 8;
constexpr uint8_t btnPins[BTN_COUNT] = {
  21, // B
  22, // A
  23, // Y
  13, // X
  14, // L
  15, // R
  32, // Select
  33  // Start
};

// =========================
// Independent rumble outputs
// One GPIO per MOSFET driver input
// =========================
constexpr uint8_t RUMBLE_COUNT = 3;
constexpr uint8_t rumblePins[RUMBLE_COUNT] = {
  25, // Motor 1
  26, // Motor 2
  27  // Motor 3
};

// =========================
// Single controllable onboard blue LED
// ESP32-WROOM dev boards commonly expose this on GPIO 2
// =========================
constexpr uint8_t PIN_STATUS_LED = 2;
constexpr uint8_t PIN_LED_R = PIN_STATUS_LED;
constexpr uint8_t PIN_LED_G = PIN_STATUS_LED;
constexpr uint8_t PIN_LED_B = PIN_STATUS_LED;

// =========================
// BLE state tracking
// =========================
uint8_t lastHat = DPAD_CENTERED;
bool lastBtn[BTN_COUNT] = {false};

// =========================
// HTTP
// =========================
WiFiServer httpServer(HTTP_PORT);
char httpBodyBuffer[256];
unsigned long lastHeartbeatMs = 0;
bool initialRegisterSent = false;

// =========================
// Per-motor state
// =========================
struct MotorState {
  bool timedRumbleActive = false;
  unsigned long timedRumbleUntilMs = 0;

  bool pulseActive = false;
  bool pulseOutputState = false;
  unsigned long pulseNextToggleMs = 0;
  uint16_t pulseOnMs = 0;
  uint16_t pulseOffMs = 0;
  uint16_t pulseEdgesRemaining = 0;

  bool repeatingPatternActive = false;
  bool repeatingOutputState = false;
  unsigned long repeatingNextToggleMs = 0;
  uint16_t repeatingOnMs = 0;
  uint16_t repeatingOffMs = 0;
};

MotorState motors[RUMBLE_COUNT];

// =========================
// Single blue LED state
// =========================
enum LedMode : uint8_t {
  LED_MODE_OFF = 0,
  LED_MODE_ON = 1,
  LED_MODE_BLINK = 2,
  LED_MODE_PULSE = 3,
};

LedMode ledMode = LED_MODE_ON;
bool ledOutputState = true;
unsigned long ledNextToggleMs = 0;
uint16_t ledBlinkOnMs = 700;
uint16_t ledBlinkOffMs = 700;
uint16_t ledPulseMs = 80;

// =========================
// Helpers
// =========================
static inline bool pressed(uint8_t pin) {
  return digitalRead(pin) == LOW;
}


bool validMotorIndex(int motorIndex) {
  return motorIndex >= 0 && motorIndex < (int)RUMBLE_COUNT;
}

int motorIdToIndex(int motorId) {
  return motorId - 1; // user-facing IDs are 1..3
}

void setMotorOutput(int motorIndex, bool on) {
  if (!validMotorIndex(motorIndex)) return;
  digitalWrite(rumblePins[motorIndex], on ? HIGH : LOW);
}

void logMotorAction(const char* action, int motorIndex, uint16_t a = 0, uint16_t b = 0, uint16_t c = 0) {
  if (!DEBUG_SERIAL) return;
  DBG_PRINT("[MOTOR] ");
  DBG_PRINT(action);
  DBG_PRINT(" motor=");
  DBG_PRINT(motorIndex + 1);
  if (a > 0) {
    DBG_PRINT(" a=");
    DBG_PRINT(a);
  }
  if (b > 0) {
    DBG_PRINT(" b=");
    DBG_PRINT(b);
  }
  if (c > 0) {
    DBG_PRINT(" c=");
    DBG_PRINTLN(c);
  } else {
    DBG_PRINTLN("");
  }
}

void refreshMotorOutput(int motorIndex) {
  if (!validMotorIndex(motorIndex)) return;

  const MotorState& m = motors[motorIndex];
  const bool shouldBeOn =
    m.repeatingPatternActive ? m.repeatingOutputState :
    m.pulseActive            ? m.pulseOutputState :
    m.timedRumbleActive      ? true :
                               false;

  setMotorOutput(motorIndex, shouldBeOn);
}

void stopMotorTimedRumble(int motorIndex) {
  if (!validMotorIndex(motorIndex)) return;
  motors[motorIndex].timedRumbleActive = false;
  logMotorAction("stop_timed", motorIndex);
  refreshMotorOutput(motorIndex);
}

void startMotorTimedRumble(int motorIndex, uint16_t durationMs) {
  if (!validMotorIndex(motorIndex)) return;
  motors[motorIndex].timedRumbleActive = true;
  motors[motorIndex].timedRumbleUntilMs = millis() + durationMs;
  logMotorAction("start_timed", motorIndex, durationMs);
  refreshMotorOutput(motorIndex);
}

void stopMotorPulse(int motorIndex) {
  if (!validMotorIndex(motorIndex)) return;
  motors[motorIndex].pulseActive = false;
  motors[motorIndex].pulseOutputState = false;
  motors[motorIndex].pulseEdgesRemaining = 0;
  logMotorAction("stop_pulse", motorIndex);
  refreshMotorOutput(motorIndex);
}

void startMotorPulse(int motorIndex, uint16_t onMs, uint16_t offMs, uint16_t count) {
  if (!validMotorIndex(motorIndex) || count == 0 || onMs == 0) return;

  MotorState& m = motors[motorIndex];
  m.pulseActive = true;
  m.pulseOutputState = true;
  m.pulseOnMs = onMs;
  m.pulseOffMs = offMs;
  m.pulseEdgesRemaining = (count * 2) - 1; // first ON already applied
  m.pulseNextToggleMs = millis() + onMs;
  logMotorAction("start_pulse", motorIndex, onMs, offMs, count);
  refreshMotorOutput(motorIndex);
}

void startMotorRepeatingPattern(int motorIndex, uint16_t onMs, uint16_t offMs) {
  if (!validMotorIndex(motorIndex) || onMs == 0) return;

  MotorState& m = motors[motorIndex];
  m.repeatingPatternActive = true;
  m.repeatingOutputState = true;
  m.repeatingOnMs = onMs;
  m.repeatingOffMs = offMs;
  m.repeatingNextToggleMs = millis() + onMs;
  logMotorAction("start_repeat", motorIndex, onMs, offMs);
  refreshMotorOutput(motorIndex);
}

void stopMotorRepeatingPattern(int motorIndex) {
  if (!validMotorIndex(motorIndex)) return;
  motors[motorIndex].repeatingPatternActive = false;
  motors[motorIndex].repeatingOutputState = false;
  logMotorAction("stop_repeat", motorIndex);
  refreshMotorOutput(motorIndex);
}

void stopMotorAllEffects(int motorIndex) {
  if (!validMotorIndex(motorIndex)) return;

  motors[motorIndex].timedRumbleActive = false;

  motors[motorIndex].pulseActive = false;
  motors[motorIndex].pulseOutputState = false;
  motors[motorIndex].pulseEdgesRemaining = 0;

  motors[motorIndex].repeatingPatternActive = false;
  motors[motorIndex].repeatingOutputState = false;

  logMotorAction("stop_all_effects", motorIndex);
  refreshMotorOutput(motorIndex);
}

void stopAllMotors() {
  for (uint8_t i = 0; i < RUMBLE_COUNT; i++) {
    stopMotorAllEffects(i);
  }
}

void updateMotors() {
  const unsigned long now = millis();

  for (uint8_t i = 0; i < RUMBLE_COUNT; i++) {
    MotorState& m = motors[i];

    if (m.timedRumbleActive && (long)(now - m.timedRumbleUntilMs) >= 0) {
      m.timedRumbleActive = false;
    }

    if (m.pulseActive && (long)(now - m.pulseNextToggleMs) >= 0) {
      if (m.pulseEdgesRemaining == 0) {
        m.pulseActive = false;
        m.pulseOutputState = false;
      } else {
        m.pulseOutputState = !m.pulseOutputState;
        m.pulseNextToggleMs = now + (m.pulseOutputState ? m.pulseOnMs : m.pulseOffMs);
        m.pulseEdgesRemaining--;
      }
    }

    if (m.repeatingPatternActive && (long)(now - m.repeatingNextToggleMs) >= 0) {
      m.repeatingOutputState = !m.repeatingOutputState;
      m.repeatingNextToggleMs = now + (m.repeatingOutputState ? m.repeatingOnMs : m.repeatingOffMs);
    }

    refreshMotorOutput(i);
  }
}

void setStatusLed(bool on) {
  digitalWrite(PIN_STATUS_LED, on ? HIGH : LOW);
  ledOutputState = on;
}

void setRgb(bool r, bool g, bool b) {
  setStatusLed(r || g || b);
}

void rgbOff() {
  setStatusLed(false);
}

void setLedOff() {
  ledMode = LED_MODE_OFF;
  setStatusLed(false);
}

void setLedOn() {
  ledMode = LED_MODE_ON;
  setStatusLed(true);
}

void startLedBlink(uint16_t onMs, uint16_t offMs) {
  if (onMs == 0) onMs = 700;
  if (offMs == 0) offMs = onMs;
  ledMode = LED_MODE_BLINK;
  ledBlinkOnMs = onMs;
  ledBlinkOffMs = offMs;
  setStatusLed(true);
  ledNextToggleMs = millis() + ledBlinkOnMs;
}

void startLedPulse(uint16_t pulseMs) {
  if (pulseMs == 0) pulseMs = 80;
  ledMode = LED_MODE_PULSE;
  ledPulseMs = pulseMs;
  setStatusLed(true);
  ledNextToggleMs = millis() + ledPulseMs;
}

void updateStatusLed() {
  if ((long)(millis() - ledNextToggleMs) < 0) return;

  if (ledMode == LED_MODE_BLINK) {
    const bool nextState = !ledOutputState;
    setStatusLed(nextState);
    ledNextToggleMs = millis() + (nextState ? ledBlinkOnMs : ledBlinkOffMs);
    return;
  }

  if (ledMode == LED_MODE_PULSE) {
    setLedOff();
    return;
  }
}

void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  DBG_PRINT("[WIFI] Connecting to SSID: ");
  DBG_PRINTLN(WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  DBG_PRINT("Connecting to Wi-Fi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
    DBG_PRINT(".");
  }
  DBG_PRINTLN("");
  DBG_PRINT("[WIFI] Connected. IP: ");
  DBG_PRINTLN(WiFi.localIP());
}

void ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;

  DBG_PRINTLN("[WIFI] Disconnected, reconnecting...");
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && (millis() - start) < 10000UL) {
    delay(100);
  }

  if (WiFi.status() == WL_CONNECTED) {
    DBG_PRINT("[WIFI] Reconnected. IP: ");
    DBG_PRINTLN(WiFi.localIP());
  } else {
    DBG_PRINTLN("[WIFI] Reconnect timed out.");
  }
}

void setLifecycleState(const String& state) {
  DBG_PRINT("[STATE] lifecycle -> ");
  DBG_PRINTLN(state);

  if (state == "starting") {
    setLedOn();
  } else if (state == "disconnected") {
    setLedOff();
  } else if (state == "waiting_content") {
    startLedBlink(700, 700);
  } else if (state == "switching_game") {
    startLedPulse(80);
  } else if (state == "playing") {
    startLedPulse(80);
  } else {
    setLedOff();
  }
}

void updateLifecycleBlink() {
  updateStatusLed();
}

String getToken(const String& input, int index) {
  int start = 0;
  int current = 0;

  while (true) {
    while (start < input.length() && input[start] == ' ') {
      start++;
    }
    if (start >= input.length()) return "";

    int end = input.indexOf(' ', start);
    if (end < 0) end = input.length();

    if (current == index) {
      return input.substring(start, end);
    }

    current++;
    start = end + 1;
  }
}

void handleNamedEvent(const String& cmd) {
  DBG_PRINT("[EVENT] Named event: ");
  DBG_PRINTLN(cmd);
  if (cmd == "HIT_STRONG") {
    for (uint8_t i = 0; i < RUMBLE_COUNT; i++) {
      startMotorTimedRumble(i, 140);
    }
    return;
  }

  if (cmd == "HIT_LIGHT") {
    startMotorTimedRumble(1, 50); // motor 2 only
    return;
  }

  if (cmd == "DOUBLE_TAP") {
    startMotorPulse(0, 40, 40, 2);
    startMotorPulse(2, 40, 40, 2);
    return;
  }

  if (cmd == "RUMBLE_ALERT_ON") {
    startMotorRepeatingPattern(0, 120, 280);
    return;
  }

  if (cmd == "RUMBLE_ALERT_OFF") {
    stopMotorRepeatingPattern(0);
    return;
  }
}

void processCommand(String cmd, const char* sourceTag) {
  cmd.trim();
  if (cmd.length() == 0) return;

  DBG_PRINT("[");
  DBG_PRINT(sourceTag);
  DBG_PRINT("] -> ");
  DBG_PRINTLN(cmd);

  if (cmd == "HIT_STRONG" ||
      cmd == "HIT_LIGHT" ||
      cmd == "DOUBLE_TAP" ||
      cmd == "RUMBLE_ALERT_ON" ||
      cmd == "RUMBLE_ALERT_OFF") {
    handleNamedEvent(cmd);
    return;
  }

  if (cmd == "STOP_ALL") {
    stopAllMotors();
    return;
  }

  if (cmd == "LED OFF") {
    setLedOff();
    return;
  }

  if (cmd == "LED ON") {
    setLedOn();
    return;
  }

  if (cmd.startsWith("LED BLINK ")) {
    uint16_t onMs = (uint16_t)getToken(cmd, 2).toInt();
    uint16_t offMs = (uint16_t)getToken(cmd, 3).toInt();
    startLedBlink(onMs, offMs);
    DBG_PRINT("[LED] blink on="); DBG_PRINT(onMs);
    DBG_PRINT(" off="); DBG_PRINTLN(offMs);
    return;
  }

  if (cmd.startsWith("LED PULSE ")) {
    uint16_t pulseMs = (uint16_t)getToken(cmd, 2).toInt();
    startLedPulse(pulseMs);
    DBG_PRINT("[LED] pulse ms="); DBG_PRINTLN(pulseMs);
    return;
  }

  if (cmd.startsWith("RUMBLE ")) {
    int motorId = getToken(cmd, 1).toInt();
    uint16_t ms = (uint16_t)getToken(cmd, 2).toInt();
    int motorIndex = motorIdToIndex(motorId);

    if (validMotorIndex(motorIndex) && ms > 0) {
      startMotorTimedRumble(motorIndex, ms);
    } else {
      DBG_PRINTLN("[WARN] Invalid RUMBLE command.");
    }
    return;
  }

  if (cmd.startsWith("RUMBLE_ALL ")) {
    uint16_t ms = (uint16_t)getToken(cmd, 1).toInt();
    if (ms > 0) {
      for (uint8_t i = 0; i < RUMBLE_COUNT; i++) {
        startMotorTimedRumble(i, ms);
      }
    } else {
      DBG_PRINTLN("[WARN] Invalid RUMBLE_ALL command.");
    }
    return;
  }

  if (cmd.startsWith("PULSE ")) {
    int motorId = getToken(cmd, 1).toInt();
    uint16_t onMs = (uint16_t)getToken(cmd, 2).toInt();
    uint16_t offMs = (uint16_t)getToken(cmd, 3).toInt();
    uint16_t count = (uint16_t)getToken(cmd, 4).toInt();
    int motorIndex = motorIdToIndex(motorId);

    if (validMotorIndex(motorIndex) && onMs > 0 && count > 0) {
      startMotorPulse(motorIndex, onMs, offMs, count);
    } else {
      DBG_PRINTLN("[WARN] Invalid PULSE command.");
    }
    return;
  }

  if (cmd.startsWith("REPEAT ")) {
    int motorId = getToken(cmd, 1).toInt();
    uint16_t onMs = (uint16_t)getToken(cmd, 2).toInt();
    uint16_t offMs = (uint16_t)getToken(cmd, 3).toInt();
    int motorIndex = motorIdToIndex(motorId);

    if (validMotorIndex(motorIndex) && onMs > 0) {
      startMotorRepeatingPattern(motorIndex, onMs, offMs);
    } else {
      DBG_PRINTLN("[WARN] Invalid REPEAT command.");
    }
    return;
  }

  if (cmd.startsWith("STOP ")) {
    int motorId = getToken(cmd, 1).toInt();
    int motorIndex = motorIdToIndex(motorId);

    if (validMotorIndex(motorIndex)) {
      stopMotorAllEffects(motorIndex);
    } else {
      DBG_PRINTLN("[WARN] Invalid STOP command.");
    }
    return;
  }

  if (cmd.startsWith("STATE ")) {
    setLifecycleState(getToken(cmd, 1));
    return;
  }

  if (cmd.startsWith("RGB ")) {
    int r = getToken(cmd, 1).toInt();
    int g = getToken(cmd, 2).toInt();
    int b = getToken(cmd, 3).toInt();
    setRgb(r != 0, g != 0, b != 0);
    DBG_PRINT("[RGB] r="); DBG_PRINT(r);
    DBG_PRINT(" g="); DBG_PRINT(g);
    DBG_PRINT(" b="); DBG_PRINTLN(b);
    return;
  }

  DBG_PRINT("[WARN] Unhandled command: ");
  DBG_PRINTLN(cmd);
}

String buildControllerJson() {
  String json = "{";
  json += "\"id\":\"";
  json += CONTROLLER_ID;
  json += "\",\"name\":\"";
  json += CONTROLLER_NAME;
  json += "\",\"role\":\"controller\"";
  json += ",\"host\":\"";
  json += WiFi.localIP().toString();
  json += "\",\"port\":";
  json += String(HTTP_PORT);
  json += "}";
  return json;
}

bool postJsonToWatcher(const char* path, const String& json) {
  WiFiClient client;
  DBG_PRINT("[HTTP] POST http://");
  DBG_PRINT(WATCHER_HOST);
  DBG_PRINT(":");
  DBG_PRINT(WATCHER_PORT);
  DBG_PRINT(path);
  DBG_PRINT(" body=");
  DBG_PRINTLN(json);

  if (!client.connect(WATCHER_HOST, WATCHER_PORT)) {
    DBG_PRINTLN("[HTTP] connect failed");
    return false;
  }

  client.print("POST ");
  client.print(path);
  client.print(" HTTP/1.1\r\nHost: ");
  client.print(WATCHER_HOST);
  client.print(":");
  client.print(WATCHER_PORT);
  client.print("\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: ");
  client.print(json.length());
  client.print("\r\n\r\n");
  client.print(json);

  unsigned long deadline = millis() + 3000;
  while (!client.available() && client.connected() && (long)(millis() - deadline) < 0) {
    delay(5);
  }

  String statusLine = client.readStringUntil('\n');
  statusLine.trim();
  DBG_PRINT("[HTTP] response ");
  DBG_PRINTLN(statusLine);

  while (client.connected() || client.available()) {
    while (client.available()) {
      client.read();
    }
    delay(1);
  }
  client.stop();
  return statusLine.indexOf(" 200 ") >= 0 || statusLine.indexOf(" 201 ") >= 0 || statusLine.indexOf(" 204 ") >= 0;
}

void sendRegister() {
  if (WiFi.status() != WL_CONNECTED) return;
  const bool ok = postJsonToWatcher("/api/controllers/register", buildControllerJson());
  DBG_PRINT("[REGISTER] ");
  DBG_PRINTLN(ok ? "ok" : "failed");
  if (ok) initialRegisterSent = true;
}

void sendHeartbeat() {
  if (WiFi.status() != WL_CONNECTED) return;
  const bool ok = postJsonToWatcher("/api/controllers/heartbeat", buildControllerJson());
  DBG_PRINT("[HEARTBEAT] ");
  DBG_PRINTLN(ok ? "ok" : "failed");
}

void maybeSendHeartbeat() {
  const unsigned long now = millis();

  if (!initialRegisterSent) {
    sendRegister();
    lastHeartbeatMs = now;
    return;
  }

  if ((unsigned long)(now - lastHeartbeatMs) >= HEARTBEAT_INTERVAL_MS) {
    sendHeartbeat();
    lastHeartbeatMs = now;
  }
}

void sendHttpResponse(WiFiClient& client, int code, const char* contentType, const String& body) {
  client.print("HTTP/1.1 ");
  client.print(code);
  switch (code) {
    case 200: client.print(" OK"); break;
    case 400: client.print(" Bad Request"); break;
    case 404: client.print(" Not Found"); break;
    case 405: client.print(" Method Not Allowed"); break;
    default:  client.print(" OK"); break;
  }
  client.print("\r\nContent-Type: ");
  client.print(contentType);
  client.print("\r\nConnection: close\r\nContent-Length: ");
  client.print(body.length());
  client.print("\r\n\r\n");
  client.print(body);
}

void readHttp() {
  WiFiClient client = httpServer.available();
  if (!client) return;

  client.setTimeout(1000);
  String requestLine = client.readStringUntil('\n');
  requestLine.trim();
  if (requestLine.length() == 0) {
    client.stop();
    return;
  }

  DBG_PRINT("[HTTP] request ");
  DBG_PRINTLN(requestLine);

  int firstSpace = requestLine.indexOf(' ');
  int secondSpace = requestLine.indexOf(' ', firstSpace + 1);
  if (firstSpace <= 0 || secondSpace <= firstSpace) {
    sendHttpResponse(client, 400, "text/plain", "bad request\n");
    client.stop();
    return;
  }

  String method = requestLine.substring(0, firstSpace);
  String path = requestLine.substring(firstSpace + 1, secondSpace);
  int contentLength = 0;

  while (client.connected()) {
    String headerLine = client.readStringUntil('\n');
    headerLine.trim();
    if (headerLine.length() == 0) break;

    if (headerLine.startsWith("Content-Length:")) {
      contentLength = headerLine.substring(15).toInt();
    }
  }

  String body = "";
  if (contentLength > 0) {
    const int maxLen = (int)sizeof(httpBodyBuffer) - 1;
    int want = contentLength > maxLen ? maxLen : contentLength;
    int got = 0;
    unsigned long deadline = millis() + 1000;
    while (got < want && (long)(millis() - deadline) < 0) {
      while (client.available() && got < want) {
        httpBodyBuffer[got++] = (char)client.read();
      }
      if (got >= want) break;
      delay(1);
    }
    httpBodyBuffer[got] = '\0';
    body = String(httpBodyBuffer);
  }

  if (method == "GET" && path == "/") {
    sendHttpResponse(client, 200, "text/plain", "controller ok\n");
    client.stop();
    return;
  }

  if (method == "GET" && path == "/status") {
    sendHttpResponse(client, 200, "application/json", buildControllerJson());
    client.stop();
    return;
  }

  if (method == "POST" && path == "/event") {
    DBG_PRINT("[HTTP] /event body=");
    DBG_PRINTLN(body);

    String eventValue = body;
    int keyPos = body.indexOf("\"event\"");
    if (keyPos >= 0) {
      int colonPos = body.indexOf(':', keyPos);
      int q1 = body.indexOf('"', colonPos + 1);
      int q2 = body.indexOf('"', q1 + 1);
      if (colonPos >= 0 && q1 >= 0 && q2 > q1) {
        eventValue = body.substring(q1 + 1, q2);
      }
    }

    eventValue.trim();
    processCommand(eventValue, "HTTP");
    sendHttpResponse(client, 200, "application/json", "{\"ok\":true}\n");
    client.stop();
    return;
  }

  if (path == "/event") {
    sendHttpResponse(client, 405, "text/plain", "method not allowed\n");
    client.stop();
    return;
  }

  DBG_PRINT("[HTTP] 404 path=");
  DBG_PRINTLN(path);
  sendHttpResponse(client, 404, "text/plain", "not found\n");
  client.stop();
}

void updateGamepad() {
  if (!bleGamepad.isConnected()) {
    return;
  }

  const bool up    = pressed(PIN_UP);
  const bool right = pressed(PIN_RIGHT);
  const bool down  = pressed(PIN_DOWN);
  const bool left  = pressed(PIN_LEFT);

  uint8_t hat = DPAD_CENTERED;

  if (up && right)      hat = DPAD_UP_RIGHT;
  else if (right && down) hat = DPAD_DOWN_RIGHT;
  else if (down && left)  hat = DPAD_DOWN_LEFT;
  else if (left && up)    hat = DPAD_UP_LEFT;
  else if (up)           hat = DPAD_UP;
  else if (right)        hat = DPAD_RIGHT;
  else if (down)         hat = DPAD_DOWN;
  else if (left)         hat = DPAD_LEFT;

  if (hat != lastHat) {
    bleGamepad.setHat1(hat);
    lastHat = hat;
  }

  for (uint8_t i = 0; i < BTN_COUNT; i++) {
    const bool cur = pressed(btnPins[i]);
    if (cur != lastBtn[i]) {
      const uint8_t id = (uint8_t)(BUTTON_1 + i);
      if (cur) bleGamepad.press(id);
      else     bleGamepad.release(id);
      lastBtn[i] = cur;
    }
  }
}

void setup() {
  DBG_BEGIN(115200);

  pinMode(PIN_UP, INPUT_PULLUP);
  pinMode(PIN_RIGHT, INPUT_PULLUP);
  pinMode(PIN_DOWN, INPUT_PULLUP);
  pinMode(PIN_LEFT, INPUT_PULLUP);

  for (uint8_t i = 0; i < BTN_COUNT; i++) {
    pinMode(btnPins[i], INPUT_PULLUP);
  }

  for (uint8_t i = 0; i < RUMBLE_COUNT; i++) {
    pinMode(rumblePins[i], OUTPUT);
    digitalWrite(rumblePins[i], LOW);
  }

  pinMode(PIN_LED_R, OUTPUT);
  pinMode(PIN_LED_G, OUTPUT);
  pinMode(PIN_LED_B, OUTPUT);
  rgbOff();

  BleGamepadConfiguration cfg;
  cfg.setButtonCount(BTN_COUNT);
  cfg.setHatSwitchCount(1);
  cfg.setWhichAxes(false, false, false, false, false, false, false, false);
  cfg.setWhichSpecialButtons(false, false, false, false, false, false, false, false);
  bleGamepad.begin(&cfg);

  connectWiFi();
  httpServer.begin();

  DBG_PRINT("HTTP listener ready on port ");
  DBG_PRINTLN(HTTP_PORT);
  DBG_PRINT("Controller ID: ");
  DBG_PRINTLN(CONTROLLER_ID);
  DBG_PRINT("Watcher: ");
  DBG_PRINT(WATCHER_HOST);
  DBG_PRINT(":");
  DBG_PRINTLN(WATCHER_PORT);

  setLifecycleState("starting");
  sendRegister();
  lastHeartbeatMs = millis();
}

void loop() {
  ensureWiFi();
  if (WiFi.status() == WL_CONNECTED && !initialRegisterSent) {
    sendRegister();
  }
  updateGamepad();
  readHttp();
  maybeSendHeartbeat();
  updateMotors();
  updateLifecycleBlink();
  delay(5);
}
