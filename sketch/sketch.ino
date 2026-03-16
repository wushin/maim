#include <Arduino.h>
#include <Arduino_RouterBridge.h>

namespace {
constexpr uint8_t STATE_STARTING = 0;
constexpr uint8_t STATE_DISCONNECTED = 1;    // Waiting for RetroArch
constexpr uint8_t STATE_WAITING_CONTENT = 2; // Connected Waiting for Content
constexpr uint8_t STATE_SWITCHING_GAME = 3;
constexpr uint8_t STATE_PLAYING = 4;

volatile uint8_t lifecycleState = STATE_STARTING;
uint8_t previousLifecycleState = 255;

String currentGame = "";
String lastPayloadJson = "{}";
uint32_t lastUnixTime = 0;

uint16_t p1Score = 0;
uint16_t p2Score = 0;
uint8_t p1HpMain = 0;
uint8_t p1HpShadow = 0;
uint8_t p2HpMain = 0;
uint8_t p2HpShadow = 0;
uint8_t p1Miss = 0;
uint8_t p1Poor = 0;
uint8_t p1Good = 0;
uint8_t p1Nice = 0;
uint8_t p2Miss = 0;
uint8_t p2Poor = 0;
uint8_t p2Good = 0;
uint8_t p2Nice = 0;

unsigned long lastBlinkMs = 0;
bool ledState = false;

void applyLed(bool on) {
  // UNO Q built-in LED is active-low in your original working sketch
  digitalWrite(LED_BUILTIN, on ? LOW : HIGH);
}

void blinkPattern(unsigned long now, unsigned long intervalMs) {
  if (now - lastBlinkMs < intervalMs) {
    return;
  }
  lastBlinkMs = now;
  ledState = !ledState;
  applyLed(ledState);
}

void slowPulsePattern(unsigned long now) {
  // Digital approximation of a slow pulse: long on, long off
  const unsigned long phase = now % 1600;
  const bool on = (phase < 950);
  if (on != ledState) {
    ledState = on;
    applyLed(ledState);
  }
}

void doubleBlinkPattern(unsigned long now) {
  // Two short flashes, then pause
  const unsigned long phase = now % 1300;
  bool on = false;

  if (phase < 90) {
    on = true;
  } else if (phase >= 220 && phase < 310) {
    on = true;
  }

  if (on != ledState) {
    ledState = on;
    applyLed(ledState);
  }
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

void set_current_game(String game) {
  currentGame = game;
}

void set_payload_json(String payload) {
  lastPayloadJson = payload;
}

void set_unix_time(unsigned long unixTime) {
  lastUnixTime = unixTime;
}

void set_p1_score(unsigned int value) { p1Score = static_cast<uint16_t>(value); }
void set_p2_score(unsigned int value) { p2Score = static_cast<uint16_t>(value); }

void set_p1_hp_main(int value) { p1HpMain = static_cast<uint8_t>(value); }
void set_p1_hp_shadow(int value) { p1HpShadow = static_cast<uint8_t>(value); }
void set_p2_hp_main(int value) { p2HpMain = static_cast<uint8_t>(value); }
void set_p2_hp_shadow(int value) { p2HpShadow = static_cast<uint8_t>(value); }

void set_p1_miss(int value) { p1Miss = static_cast<uint8_t>(value); }
void set_p1_poor(int value) { p1Poor = static_cast<uint8_t>(value); }
void set_p1_good(int value) { p1Good = static_cast<uint8_t>(value); }
void set_p1_nice(int value) { p1Nice = static_cast<uint8_t>(value); }

void set_p2_miss(int value) { p2Miss = static_cast<uint8_t>(value); }
void set_p2_poor(int value) { p2Poor = static_cast<uint8_t>(value); }
void set_p2_good(int value) { p2Good = static_cast<uint8_t>(value); }
void set_p2_nice(int value) { p2Nice = static_cast<uint8_t>(value); }

int get_lifecycle_state() { return lifecycleState; }
String get_current_game() { return currentGame; }
String get_payload_json() { return lastPayloadJson; }
unsigned long get_unix_time() { return lastUnixTime; }

int get_p1_score() { return p1Score; }
int get_p2_score() { return p2Score; }

int get_p1_hp_main() { return p1HpMain; }
int get_p1_hp_shadow() { return p1HpShadow; }
int get_p2_hp_main() { return p2HpMain; }
int get_p2_hp_shadow() { return p2HpShadow; }

int get_p1_miss() { return p1Miss; }
int get_p1_poor() { return p1Poor; }
int get_p1_good() { return p1Good; }
int get_p1_nice() { return p1Nice; }

int get_p2_miss() { return p2Miss; }
int get_p2_poor() { return p2Poor; }
int get_p2_good() { return p2Good; }
int get_p2_nice() { return p2Nice; }
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  applyLed(false);

  Bridge.begin();

  Bridge.provide("set_lifecycle_state", set_lifecycle_state);
  Bridge.provide("set_current_game", set_current_game);
  Bridge.provide("set_payload_json", set_payload_json);
  Bridge.provide("set_unix_time", set_unix_time);

  Bridge.provide("set_p1_score", set_p1_score);
  Bridge.provide("set_p2_score", set_p2_score);

  Bridge.provide("set_p1_hp_main", set_p1_hp_main);
  Bridge.provide("set_p1_hp_shadow", set_p1_hp_shadow);
  Bridge.provide("set_p2_hp_main", set_p2_hp_main);
  Bridge.provide("set_p2_hp_shadow", set_p2_hp_shadow);

  Bridge.provide("set_p1_miss", set_p1_miss);
  Bridge.provide("set_p1_poor", set_p1_poor);
  Bridge.provide("set_p1_good", set_p1_good);
  Bridge.provide("set_p1_nice", set_p1_nice);

  Bridge.provide("set_p2_miss", set_p2_miss);
  Bridge.provide("set_p2_poor", set_p2_poor);
  Bridge.provide("set_p2_good", set_p2_good);
  Bridge.provide("set_p2_nice", set_p2_nice);

  Bridge.provide("get_lifecycle_state", get_lifecycle_state);
  Bridge.provide("get_current_game", get_current_game);
  Bridge.provide("get_payload_json", get_payload_json);
  Bridge.provide("get_unix_time", get_unix_time);

  Bridge.provide("get_p1_score", get_p1_score);
  Bridge.provide("get_p2_score", get_p2_score);

  Bridge.provide("get_p1_hp_main", get_p1_hp_main);
  Bridge.provide("get_p1_hp_shadow", get_p1_hp_shadow);
  Bridge.provide("get_p2_hp_main", get_p2_hp_main);
  Bridge.provide("get_p2_hp_shadow", get_p2_hp_shadow);

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

  if (lifecycleState != previousLifecycleState) {
    previousLifecycleState = lifecycleState;
    lastBlinkMs = now;
    ledState = false;
    applyLed(false);
  }

  switch (lifecycleState) {
    case STATE_STARTING:
      blinkPattern(now, 150);
      break;

    case STATE_DISCONNECTED:
      slowPulsePattern(now);
      break;

    case STATE_WAITING_CONTENT:
      doubleBlinkPattern(now);
      break;

    case STATE_SWITCHING_GAME:
      blinkPattern(now, 90);
      break;

    case STATE_PLAYING:
      solidOnPattern();
      break;

    default:
      blinkPattern(now, 500);
      break;
  }
}
