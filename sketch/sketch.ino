// SPDX-License-Identifier: MPL-2.0
#include <Arduino.h>
#include <Arduino_RouterBridge.h>

namespace {
constexpr uint8_t STATE_STARTING = 0;
constexpr uint8_t STATE_DISCONNECTED = 1;      // Waiting for RetroArch
constexpr uint8_t STATE_WAITING_CONTENT = 2;   // Connected Waiting for Content
constexpr uint8_t STATE_SWITCHING_GAME = 3;
constexpr uint8_t STATE_PLAYING = 4;
constexpr uint8_t STATE_ERROR = 255;

constexpr unsigned long HEARTBEAT_TIMEOUT_MS = 3500;
constexpr unsigned long DOUBLE_BLINK_GAP_MS = 120;
constexpr unsigned long DOUBLE_BLINK_PAUSE_MS = 900;
constexpr unsigned long FLICKER_STEP_MS = 70;
constexpr unsigned long ERROR_BLINK_MS = 120;

volatile uint8_t lifecycleState = STATE_STARTING;
String currentGame = "";
String lastPayloadJson = "{}";
uint32_t lastUnixTime = 0;
unsigned long lastHeartbeatSeenMs = 0;

uint16_t p1Score = 0;
uint16_t p2Score = 0;
uint8_t p1Hp = 0;
uint8_t p2Hp = 0;
uint8_t p1Shadow = 0;
uint8_t p2Shadow = 0;
uint8_t p1Miss = 0;
uint8_t p1Poor = 0;
uint8_t p1Good = 0;
uint8_t p1Nice = 0;
uint8_t p2Miss = 0;
uint8_t p2Poor = 0;
uint8_t p2Good = 0;
uint8_t p2Nice = 0;

unsigned long lastLedStepMs = 0;
bool ledState = false;

void applyLed(bool on) {
  // On UNO Q builtin LED is active low, same as the demo example.
  digitalWrite(LED_BUILTIN, on ? LOW : HIGH);
}

void blinkPattern(unsigned long now, unsigned long intervalMs) {
  if (now - lastLedStepMs < intervalMs) {
    return;
  }
  lastLedStepMs = now;
  ledState = !ledState;
  applyLed(ledState);
}

void slowPulsePattern(unsigned long now) {
  // Digital-only approximation of a slow pulse:
  // long on / long off gives a calm, readable heartbeat-like cadence.
  constexpr unsigned long pulseCycleMs = 1600;
  const unsigned long phase = now % pulseCycleMs;
  const bool on = (phase < 900);
  if (on != ledState) {
    ledState = on;
    applyLed(ledState);
  }
}

void doubleBlinkPattern(unsigned long now) {
  // Two short flashes, then a pause.
  constexpr unsigned long cycleMs = (DOUBLE_BLINK_GAP_MS * 2) + 180 + DOUBLE_BLINK_PAUSE_MS;
  const unsigned long phase = now % cycleMs;

  bool on = false;
  if (phase < 90) {
    on = true;
  } else if (phase >= 90 + DOUBLE_BLINK_GAP_MS && phase < 180 + DOUBLE_BLINK_GAP_MS) {
    on = true;
  }

  if (on != ledState) {
    ledState = on;
    applyLed(ledState);
  }
}

void rapidFlickerPattern(unsigned long now) {
  blinkPattern(now, FLICKER_STEP_MS);
}

void solidOnPattern() {
  if (!ledState) {
    ledState = true;
    applyLed(true);
  }
}

void set_lifecycle_state(int stateCode) {
  lifecycleState = static_cast<uint8_t>(stateCode);
}

void set_game_title(String title) {
  currentGame = title;
}

void set_payload_json(String payload) {
  lastPayloadJson = payload;
}

void set_unix_time(unsigned long unixTime) {
  lastUnixTime = unixTime;
  lastHeartbeatSeenMs = millis();
}

void set_p1_score(unsigned int value) { p1Score = static_cast<uint16_t>(value); }
void set_p2_score(unsigned int value) { p2Score = static_cast<uint16_t>(value); }
void set_p1_hp(int value) { p1Hp = static_cast<uint8_t>(value); }
void set_p2_hp(int value) { p2Hp = static_cast<uint8_t>(value); }
void set_p1_shadow(int value) { p1Shadow = static_cast<uint8_t>(value); }
void set_p2_shadow(int value) { p2Shadow = static_cast<uint8_t>(value); }
void set_p1_miss(int value) { p1Miss = static_cast<uint8_t>(value); }
void set_p1_poor(int value) { p1Poor = static_cast<uint8_t>(value); }
void set_p1_good(int value) { p1Good = static_cast<uint8_t>(value); }
void set_p1_nice(int value) { p1Nice = static_cast<uint8_t>(value); }
void set_p2_miss(int value) { p2Miss = static_cast<uint8_t>(value); }
void set_p2_poor(int value) { p2Poor = static_cast<uint8_t>(value); }
void set_p2_good(int value) { p2Good = static_cast<uint8_t>(value); }
void set_p2_nice(int value) { p2Nice = static_cast<uint8_t>(value); }

String get_game_title() { return currentGame; }
String get_payload_json() { return lastPayloadJson; }
int get_lifecycle_state() { return lifecycleState; }
unsigned long get_unix_time() { return lastUnixTime; }
unsigned int get_p1_score() { return p1Score; }
unsigned int get_p2_score() { return p2Score; }
int get_p1_hp() { return p1Hp; }
int get_p2_hp() { return p2Hp; }
int get_p1_shadow() { return p1Shadow; }
int get_p2_shadow() { return p2Shadow; }
int get_p1_miss() { return p1Miss; }
int get_p1_poor() { return p1Poor; }
int get_p1_good() { return p1Good; }
int get_p1_nice() { return p1Nice; }
int get_p2_miss() { return p2Miss; }
int get_p2_poor() { return p2Poor; }
int get_p2_good() { return p2Good; }
int get_p2_nice() { return p2Nice; }
}  // namespace

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  applyLed(false);
  lastHeartbeatSeenMs = millis();

  Bridge.begin();
  Bridge.provide("set_lifecycle_state", set_lifecycle_state);
  Bridge.provide("set_game_title", set_game_title);
  Bridge.provide("set_payload_json", set_payload_json);
  Bridge.provide("set_unix_time", set_unix_time);

  Bridge.provide("set_p1_score", set_p1_score);
  Bridge.provide("set_p2_score", set_p2_score);
  Bridge.provide("set_p1_hp", set_p1_hp);
  Bridge.provide("set_p2_hp", set_p2_hp);
  Bridge.provide("set_p1_shadow", set_p1_shadow);
  Bridge.provide("set_p2_shadow", set_p2_shadow);
  Bridge.provide("set_p1_miss", set_p1_miss);
  Bridge.provide("set_p1_poor", set_p1_poor);
  Bridge.provide("set_p1_good", set_p1_good);
  Bridge.provide("set_p1_nice", set_p1_nice);
  Bridge.provide("set_p2_miss", set_p2_miss);
  Bridge.provide("set_p2_poor", set_p2_poor);
  Bridge.provide("set_p2_good", set_p2_good);
  Bridge.provide("set_p2_nice", set_p2_nice);

  Bridge.provide("get_game_title", get_game_title);
  Bridge.provide("get_payload_json", get_payload_json);
  Bridge.provide("get_lifecycle_state", get_lifecycle_state);
  Bridge.provide("get_unix_time", get_unix_time);
  Bridge.provide("get_p1_score", get_p1_score);
  Bridge.provide("get_p2_score", get_p2_score);
  Bridge.provide("get_p1_hp", get_p1_hp);
  Bridge.provide("get_p2_hp", get_p2_hp);
  Bridge.provide("get_p1_shadow", get_p1_shadow);
  Bridge.provide("get_p2_shadow", get_p2_shadow);
  Bridge.provide("get_p1_miss", get_p1_miss);
  Bridge.provide("get_p1_poor", get_p1_poor);
  Bridge.provide("get_p1_good", get_p1_good);
  Bridge.provide("get_p1_nice", get_p1_nice);
  Bridge.provide("get_p2_miss", get_p2_miss);
  Bridge.provide("get_p2_poor", get_p2_poor);
  Bridge.provide("get_p2_good", get_p2_good);
  Bridge.provide("get_p2_nice", get_p2_nice);
}

void loop() {
  const unsigned long now = millis();
  const bool heartbeatStale = (now - lastHeartbeatSeenMs) > HEARTBEAT_TIMEOUT_MS;

  if (heartbeatStale && lifecycleState != STATE_STARTING) {
    blinkPattern(now, ERROR_BLINK_MS);
    return;
  }

  switch (lifecycleState) {
    case STATE_STARTING:
      blinkPattern(now, 200);
      break;
    case STATE_DISCONNECTED:
      slowPulsePattern(now);
      break;
    case STATE_WAITING_CONTENT:
      doubleBlinkPattern(now);
      break;
    case STATE_SWITCHING_GAME:
      rapidFlickerPattern(now);
      break;
    case STATE_PLAYING:
      solidOnPattern();
      break;
    default:
      blinkPattern(now, ERROR_BLINK_MS);
      break;
  }
}
