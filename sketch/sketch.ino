#include <Arduino.h>
#include <Arduino_RouterBridge.h>
#include <Arduino_LED_Matrix.h>

namespace {
constexpr uint8_t STATE_STARTING = 0;
constexpr uint8_t STATE_DISCONNECTED = 1;    // Waiting for RetroArch
constexpr uint8_t STATE_WAITING_CONTENT = 2; // Connected Waiting for Content
constexpr uint8_t STATE_SWITCHING_GAME = 3;
constexpr uint8_t STATE_PLAYING = 4;

constexpr uint8_t MATRIX_HEIGHT = 8;
constexpr uint8_t MATRIX_WIDTH = 12;
constexpr unsigned long MARQUEE_STEP_MS = 35; // fast marquee speed
constexpr uint8_t GLYPH_WIDTH = 5;
constexpr uint8_t GLYPH_SPACING = 1;

volatile uint8_t lifecycleState = STATE_STARTING;
uint8_t previousLifecycleState = 255;

String currentGame = "";
String lastPayloadJson = "{}";
String lastTriggerEvent = "";
uint32_t lastUnixTime = 0;
uint32_t triggerEventCount = 0;

struct TriggerPinRule {
  const char* eventName;
  uint8_t pin;
  uint8_t level;
};

constexpr uint8_t PIN_HP = 5;
constexpr uint8_t PIN_NICE = 6;

const TriggerPinRule TRIGGER_RULES[] = {
  { "hp_down", PIN_HP, HIGH },
  { "hp_up",   PIN_HP, LOW  },
  { "nice",    PIN_NICE, HIGH },
  { "miss",    PIN_NICE, LOW  },
};

ArduinoLEDMatrix matrix;
uint8_t frame[MATRIX_HEIGHT][MATRIX_WIDTH] = {};
unsigned long lastMarqueeStepMs = 0;
uint16_t marqueeSubpixelOffset = 0;

// 5x7 glyphs packed as row bitmaps, MSB on the left.
const uint8_t GLYPH_M[MATRIX_HEIGHT] = {
  0b10001,
  0b11011,
  0b10101,
  0b10001,
  0b10001,
  0b10001,
  0b10001,
  0b00000,
};

const uint8_t GLYPH_A[MATRIX_HEIGHT] = {
  0b01110,
  0b10001,
  0b10001,
  0b11111,
  0b10001,
  0b10001,
  0b10001,
  0b00000,
};

const uint8_t GLYPH_I[MATRIX_HEIGHT] = {
  0b11111,
  0b00100,
  0b00100,
  0b00100,
  0b00100,
  0b00100,
  0b11111,
  0b00000,
};

const uint8_t* const MAIM_GLYPHS[] = {
  GLYPH_M,
  GLYPH_A,
  GLYPH_I,
  GLYPH_M,
};

constexpr uint8_t GLYPH_COUNT = sizeof(MAIM_GLYPHS) / sizeof(MAIM_GLYPHS[0]);
constexpr uint8_t MESSAGE_WIDTH = (GLYPH_COUNT * (GLYPH_WIDTH + GLYPH_SPACING)) + MATRIX_WIDTH;

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

bool readGlyphPixel(const uint8_t* glyph, uint8_t row, uint8_t col) {
  return (glyph[row] >> (GLYPH_WIDTH - 1 - col)) & 0x01;
}

bool readMessageColumn(uint8_t row, uint16_t column) {
  if (column < MATRIX_WIDTH) {
    return false; // leading gap so the word scrolls cleanly onto the display
  }

  const uint16_t shifted = column - MATRIX_WIDTH;
  const uint8_t glyphBlockWidth = GLYPH_WIDTH + GLYPH_SPACING;
  const uint8_t glyphIndex = shifted / glyphBlockWidth;
  const uint8_t glyphColumn = shifted % glyphBlockWidth;

  if (glyphIndex >= GLYPH_COUNT) {
    return false; // trailing gap after the word exits the display
  }

  if (glyphColumn >= GLYPH_WIDTH) {
    return false; // spacer column between letters
  }

  return readGlyphPixel(MAIM_GLYPHS[glyphIndex], row, glyphColumn);
}

void renderMarqueeFrame() {
  const uint16_t baseColumn = marqueeSubpixelOffset / 2;
  const bool halfStep = (marqueeSubpixelOffset & 0x01) != 0;

  for (uint8_t y = 0; y < MATRIX_HEIGHT; ++y) {
    for (uint8_t x = 0; x < MATRIX_WIDTH; ++x) {
      const uint16_t leftColumn = (baseColumn + x) % MESSAGE_WIDTH;
      bool pixel = readMessageColumn(y, leftColumn);

      if (halfStep) {
        const uint16_t rightColumn = (leftColumn + 1) % MESSAGE_WIDTH;
        const bool rightPixel = readMessageColumn(y, rightColumn);

        // Checkerboard temporal blend between adjacent columns.
        // This fakes a half-column shift on a 1-bit matrix and makes the
        // marquee feel smoother than a hard 1-column jump.
        pixel = ((x + y) & 0x01) ? rightPixel : pixel;
      }

      frame[y][x] = pixel ? 1 : 0;
    }
  }

  matrix.renderBitmap(frame, MATRIX_HEIGHT, MATRIX_WIDTH);
}

void updateMarquee(unsigned long now) {
  if (now - lastMarqueeStepMs >= MARQUEE_STEP_MS) {
    lastMarqueeStepMs = now;
    marqueeSubpixelOffset = (marqueeSubpixelOffset + 1) % (MESSAGE_WIDTH * 2);
  }

  renderMarqueeFrame();
}

void set_lifecycle_state(int stateCode) {
  lifecycleState = static_cast<uint8_t>(stateCode);
}

void set_current_game(String game) {
  currentGame = game;
}

void set_game_title(String game) {
  currentGame = game;
}

void set_payload_json(String payload) {
  lastPayloadJson = payload;
}

void set_unix_time(unsigned long unixTime) {
  lastUnixTime = unixTime;
}

void trigger_event(String eventName) {
  lastTriggerEvent = eventName;
  triggerEventCount++;

  for (const auto& rule : TRIGGER_RULES) {
    if (eventName == rule.eventName) {
      digitalWrite(rule.pin, rule.level);
      return;
    }
  }
}

int get_lifecycle_state() { return lifecycleState; }
String get_current_game() { return currentGame; }
String get_game_title() { return currentGame; }
String get_payload_json() { return lastPayloadJson; }
unsigned long get_unix_time() { return lastUnixTime; }
String get_last_trigger_event() { return lastTriggerEvent; }
unsigned long get_trigger_event_count() { return triggerEventCount; }
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  applyLed(false);
  pinMode(PIN_HP, OUTPUT);
  pinMode(PIN_NICE, OUTPUT);

  digitalWrite(PIN_HP, LOW);
  digitalWrite(PIN_NICE, LOW);

  matrix.begin();
  renderMarqueeFrame();

  Bridge.begin();

  Bridge.provide("set_lifecycle_state", set_lifecycle_state);
  Bridge.provide("set_current_game", set_current_game);
  Bridge.provide("set_game_title", set_game_title);
  Bridge.provide("set_payload_json", set_payload_json);
  Bridge.provide("set_unix_time", set_unix_time);
  Bridge.provide("trigger_event", trigger_event);

  Bridge.provide("get_lifecycle_state", get_lifecycle_state);
  Bridge.provide("get_current_game", get_current_game);
  Bridge.provide("get_game_title", get_game_title);
  Bridge.provide("get_payload_json", get_payload_json);
  Bridge.provide("get_unix_time", get_unix_time);
  Bridge.provide("get_last_trigger_event", get_last_trigger_event);
  Bridge.provide("get_trigger_event_count", get_trigger_event_count);
}

void loop() {
  const unsigned long now = millis();

  updateMarquee(now);

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
