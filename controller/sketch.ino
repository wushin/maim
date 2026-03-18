#include <WiFi.h>
#include <WiFiUdp.h>
#include <BleGamepad.h>

BleGamepad bleGamepad("I Control Them", "MAIM", 100);

// =========================
// Wi-Fi settings
// =========================
constexpr char WIFI_SSID[] = "YOUR_WIFI_SSID";
constexpr char WIFI_PASS[] = "YOUR_WIFI_PASSWORD";
constexpr uint16_t UDP_PORT = 4210;

// =========================
// D-pad pins (digital -> X/Y axes)
// =========================
constexpr uint8_t PIN_UP    = 16;
constexpr uint8_t PIN_RIGHT = 17;
constexpr uint8_t PIN_DOWN  = 18;
constexpr uint8_t PIN_LEFT  = 19;

// =========================
// Buttons in this exact order become BUTTON_1..BUTTON_8
// 1=B, 2=A, 3=Y, 4=X, 5=L, 6=R, 7=Select, 8=Start
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
// RGB LED pins
// Change as needed for your wiring
// =========================
constexpr uint8_t PIN_LED_R = 4;
constexpr uint8_t PIN_LED_G = 5;
constexpr uint8_t PIN_LED_B = 12;

// =========================
// BLE state tracking
// =========================
int16_t lastX = 0;
int16_t lastY = 0;
bool lastBtn[BTN_COUNT] = {false};

// =========================
// UDP
// =========================
WiFiUDP udp;
char udpBuffer[256];

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
// Lifecycle LED blink state
// =========================
bool lifecycleBlinkEnabled = false;
unsigned long lifecycleBlinkNextMs = 0;
bool lifecycleBlinkState = false;

// =========================
// Helpers
// =========================
static inline bool pressed(uint8_t pin) {
  return digitalRead(pin) == LOW;
}

static int16_t axisFromPair(bool negative, bool positive) {
  if (negative && positive) return 0;
  if (negative) return -32767;
  if (positive) return 32767;
  return 0;
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
  refreshMotorOutput(motorIndex);
}

void startMotorTimedRumble(int motorIndex, uint16_t durationMs) {
  if (!validMotorIndex(motorIndex)) return;
  motors[motorIndex].timedRumbleActive = true;
  motors[motorIndex].timedRumbleUntilMs = millis() + durationMs;
  refreshMotorOutput(motorIndex);
}

void stopMotorPulse(int motorIndex) {
  if (!validMotorIndex(motorIndex)) return;
  motors[motorIndex].pulseActive = false;
  motors[motorIndex].pulseOutputState = false;
  motors[motorIndex].pulseEdgesRemaining = 0;
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
  refreshMotorOutput(motorIndex);
}

void stopMotorRepeatingPattern(int motorIndex) {
  if (!validMotorIndex(motorIndex)) return;
  motors[motorIndex].repeatingPatternActive = false;
  motors[motorIndex].repeatingOutputState = false;
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

void setRgb(bool r, bool g, bool b) {
  digitalWrite(PIN_LED_R, r ? HIGH : LOW);
  digitalWrite(PIN_LED_G, g ? HIGH : LOW);
  digitalWrite(PIN_LED_B, b ? HIGH : LOW);
}

void rgbOff() {
  setRgb(false, false, false);
}

void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  Serial.print("Connecting to Wi-Fi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
    Serial.print(".");
  }
  Serial.println();
  Serial.print("Wi-Fi connected. IP: ");
  Serial.println(WiFi.localIP());
}

void ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;

  Serial.println("Wi-Fi disconnected, reconnecting...");
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && (millis() - start) < 10000UL) {
    delay(100);
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("Wi-Fi reconnected. IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("Wi-Fi reconnect timed out.");
  }
}

void setLifecycleState(const String& state) {
  lifecycleBlinkEnabled = false;
  lifecycleBlinkState = false;

  if (state == "starting") {
    setRgb(false, false, true);
  } else if (state == "disconnected") {
    setRgb(true, false, false);
    lifecycleBlinkEnabled = true;
    lifecycleBlinkNextMs = millis() + 300;
    lifecycleBlinkState = true;
  } else if (state == "waiting_content") {
    setRgb(true, true, false);
  } else if (state == "switching_game") {
    setRgb(true, false, true);
  } else if (state == "playing") {
    setRgb(false, true, false);
  } else {
    rgbOff();
  }
}

void updateLifecycleBlink() {
  if (!lifecycleBlinkEnabled) return;
  if ((long)(millis() - lifecycleBlinkNextMs) < 0) return;

  lifecycleBlinkState = !lifecycleBlinkState;
  setRgb(lifecycleBlinkState, false, false);
  lifecycleBlinkNextMs = millis() + 300;
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

void processUdpCommand(String cmd) {
  cmd.trim();
  if (cmd.length() == 0) return;

  Serial.print("UDP cmd: ");
  Serial.println(cmd);

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
    lifecycleBlinkEnabled = false;
    rgbOff();
    return;
  }

  // RUMBLE <motor> <ms>
  // Example: RUMBLE 1 150
  if (cmd.startsWith("RUMBLE ")) {
    int motorId = getToken(cmd, 1).toInt();
    uint16_t ms = (uint16_t)getToken(cmd, 2).toInt();
    int motorIndex = motorIdToIndex(motorId);

    if (validMotorIndex(motorIndex) && ms > 0) {
      startMotorTimedRumble(motorIndex, ms);
    }
    return;
  }

  // RUMBLE_ALL <ms>
  // Example: RUMBLE_ALL 150
  if (cmd.startsWith("RUMBLE_ALL ")) {
    uint16_t ms = (uint16_t)getToken(cmd, 1).toInt();
    if (ms > 0) {
      for (uint8_t i = 0; i < RUMBLE_COUNT; i++) {
        startMotorTimedRumble(i, ms);
      }
    }
    return;
  }

  // PULSE <motor> <on_ms> <off_ms> <count>
  // Example: PULSE 2 120 80 2
  if (cmd.startsWith("PULSE ")) {
    int motorId = getToken(cmd, 1).toInt();
    uint16_t onMs = (uint16_t)getToken(cmd, 2).toInt();
    uint16_t offMs = (uint16_t)getToken(cmd, 3).toInt();
    uint16_t count = (uint16_t)getToken(cmd, 4).toInt();
    int motorIndex = motorIdToIndex(motorId);

    if (validMotorIndex(motorIndex) && onMs > 0 && count > 0) {
      startMotorPulse(motorIndex, onMs, offMs, count);
    }
    return;
  }

  // REPEAT <motor> <on_ms> <off_ms>
  // Example: REPEAT 1 120 280
  if (cmd.startsWith("REPEAT ")) {
    int motorId = getToken(cmd, 1).toInt();
    uint16_t onMs = (uint16_t)getToken(cmd, 2).toInt();
    uint16_t offMs = (uint16_t)getToken(cmd, 3).toInt();
    int motorIndex = motorIdToIndex(motorId);

    if (validMotorIndex(motorIndex) && onMs > 0) {
      startMotorRepeatingPattern(motorIndex, onMs, offMs);
    }
    return;
  }

  // STOP <motor>
  // Example: STOP 3
  if (cmd.startsWith("STOP ")) {
    int motorId = getToken(cmd, 1).toInt();
    int motorIndex = motorIdToIndex(motorId);

    if (validMotorIndex(motorIndex)) {
      stopMotorAllEffects(motorIndex);
    }
    return;
  }

  // STATE <name>
  // Example: STATE playing
  if (cmd.startsWith("STATE ")) {
    String state = cmd.substring(6);
    state.trim();
    setLifecycleState(state);
    return;
  }

  // RGB <r> <g> <b>
  // Example: RGB 1 0 1
  if (cmd.startsWith("RGB ")) {
    bool r = getToken(cmd, 1).toInt() != 0;
    bool g = getToken(cmd, 2).toInt() != 0;
    bool b = getToken(cmd, 3).toInt() != 0;
    lifecycleBlinkEnabled = false;
    setRgb(r, g, b);
    return;
  }
}

void readUdp() {
  int packetSize = udp.parsePacket();
  if (packetSize <= 0) return;

  int len = udp.read(udpBuffer, sizeof(udpBuffer) - 1);
  if (len <= 0) return;

  udpBuffer[len] = '\0';
  processUdpCommand(String(udpBuffer));
}

void updateGamepad() {
  if (!bleGamepad.isConnected()) {
    return;
  }

  const bool up    = pressed(PIN_UP);
  const bool right = pressed(PIN_RIGHT);
  const bool down  = pressed(PIN_DOWN);
  const bool left  = pressed(PIN_LEFT);

  const int16_t x = axisFromPair(left, right);
  const int16_t y = axisFromPair(up, down);

  if (x != lastX) {
    bleGamepad.setX(x);
    lastX = x;
  }

  if (y != lastY) {
    bleGamepad.setY(y);
    lastY = y;
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
  Serial.begin(115200);

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
  cfg.setHatSwitchCount(0);
  cfg.setWhichAxes(true, true, false, false, false, false, false, false);
  cfg.setWhichSpecialButtons(false, false, false, false, false, false, false, false);
  bleGamepad.begin(&cfg);

  connectWiFi();
  udp.begin(UDP_PORT);

  Serial.print("UDP listener ready on port ");
  Serial.println(UDP_PORT);

  setLifecycleState("starting");
}

void loop() {
  ensureWiFi();
  updateGamepad();
  readUdp();
  updateMotors();
  updateLifecycleBlink();
  delay(5);
}
