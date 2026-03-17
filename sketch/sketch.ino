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
constexpr unsigned long MARQUEE_STEP_MS = 35;
constexpr uint8_t GLYPH_WIDTH = 5;
constexpr uint8_t GLYPH_SPACING = 1;
constexpr uint8_t MAX_OUTPUT_CHANNELS = 16;

volatile uint8_t lifecycleState = STATE_STARTING;
String currentGame = "";
String lastPayloadJson = "{}";
String lastTriggerEvent = "";
uint32_t lastUnixTime = 0;
uint32_t triggerEventCount = 0;

ArduinoLEDMatrix matrix;
uint8_t frame[MATRIX_HEIGHT][MATRIX_WIDTH] = {};
unsigned long lastMarqueeStepMs = 0;
uint16_t marqueeSubpixelOffset = 0;

enum OutputBehavior : uint8_t {
  OUTPUT_BEHAVIOR_IDLE = 0,
  OUTPUT_BEHAVIOR_SET = 1,
  OUTPUT_BEHAVIOR_TIMED_SET = 2,
  OUTPUT_BEHAVIOR_PULSE = 3,
};

struct OutputChannel {
  bool configured = false;
  uint8_t pin = 0;
  bool activeHigh = true;
  bool currentOn = false;
  OutputBehavior behavior = OUTPUT_BEHAVIOR_IDLE;
  unsigned long nextChangeAt = 0;
  unsigned long onMs = 0;
  unsigned long offMs = 0;
  unsigned long durationMs = 0;
  uint16_t pulsesRemaining = 0;
};

OutputChannel outputChannels[MAX_OUTPUT_CHANNELS];

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

void applyLed(bool on) {
  digitalWrite(LED_BUILTIN, on ? LOW : HIGH);
}

void setRgb(bool red, bool green, bool blue) {
  digitalWrite(LED3_R, red ? LOW : HIGH);
  digitalWrite(LED3_G, green ? LOW : HIGH);
  digitalWrite(LED3_B, blue ? LOW : HIGH);
}

void rgbOff() {
  setRgb(false, false, false);
}

bool readGlyphPixel(const uint8_t* glyph, uint8_t row, uint8_t col) {
  return (glyph[row] >> (GLYPH_WIDTH - 1 - col)) & 0x01;
}

bool readMessageColumn(uint8_t row, uint16_t column) {
  if (column < MATRIX_WIDTH) {
    return false;
  }

  const uint16_t shifted = column - MATRIX_WIDTH;
  const uint8_t glyphBlockWidth = GLYPH_WIDTH + GLYPH_SPACING;
  const uint8_t glyphIndex = shifted / glyphBlockWidth;
  const uint8_t glyphColumn = shifted % glyphBlockWidth;

  if (glyphIndex >= GLYPH_COUNT) {
    return false;
  }

  if (glyphColumn >= GLYPH_WIDTH) {
    return false;
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

OutputChannel* findOrCreateChannel(uint8_t pin, bool activeHigh) {
  for (uint8_t i = 0; i < MAX_OUTPUT_CHANNELS; ++i) {
    if (outputChannels[i].configured && outputChannels[i].pin == pin) {
      outputChannels[i].activeHigh = activeHigh;
      return &outputChannels[i];
    }
  }

  for (uint8_t i = 0; i < MAX_OUTPUT_CHANNELS; ++i) {
    if (!outputChannels[i].configured) {
      outputChannels[i].configured = true;
      outputChannels[i].pin = pin;
      outputChannels[i].activeHigh = activeHigh;
      outputChannels[i].currentOn = false;
      outputChannels[i].behavior = OUTPUT_BEHAVIOR_IDLE;
      pinMode(pin, OUTPUT);
      if (activeHigh) {
        digitalWrite(pin, LOW);
      } else {
        digitalWrite(pin, HIGH);
      }
      return &outputChannels[i];
    }
  }

  return nullptr;
}

void writeChannel(OutputChannel& channel, bool on) {
  channel.currentOn = on;
  if (channel.activeHigh) {
    digitalWrite(channel.pin, on ? HIGH : LOW);
  } else {
    digitalWrite(channel.pin, on ? LOW : HIGH);
  }
}

String getTokenValue(const String& source, const String& key) {
  const String prefix = key + "=";
  int start = source.indexOf(prefix);
  if (start < 0) {
    return "";
  }
  start += prefix.length();
  int end = source.indexOf(';', start);
  if (end < 0) {
    end = source.length();
  }
  return source.substring(start, end);
}

bool parseBoolLike(const String& value, bool defaultValue) {
  if (value.length() == 0) {
    return defaultValue;
  }
  if (value == "1" || value == "true" || value == "on" || value == "high") {
    return true;
  }
  if (value == "0" || value == "false" || value == "off" || value == "low") {
    return false;
  }
  return defaultValue;
}

unsigned long parseUnsignedLong(const String& value, unsigned long defaultValue) {
  if (value.length() == 0) {
    return defaultValue;
  }
  return static_cast<unsigned long>(value.toInt());
}

void executeSetAction(OutputChannel& channel, bool on, unsigned long durationMs, unsigned long now) {
  writeChannel(channel, on);
  if (durationMs > 0 && on) {
    channel.behavior = OUTPUT_BEHAVIOR_TIMED_SET;
    channel.durationMs = durationMs;
    channel.nextChangeAt = now + durationMs;
  } else {
    channel.behavior = OUTPUT_BEHAVIOR_SET;
    channel.durationMs = 0;
    channel.nextChangeAt = 0;
  }
}

void executePulseAction(OutputChannel& channel, unsigned long onMs, unsigned long offMs, uint16_t count, unsigned long now) {
  channel.behavior = OUTPUT_BEHAVIOR_PULSE;
  channel.onMs = onMs;
  channel.offMs = offMs;
  channel.pulsesRemaining = count;
  writeChannel(channel, true);
  channel.nextChangeAt = now + onMs;
}

void trigger_event(String command) {
  triggerEventCount++;

  if (command.indexOf('=') < 0) {
    lastTriggerEvent = command;
    return;
  }

  const String eventName = getTokenValue(command, "event");
  const String pinText = getTokenValue(command, "pin");
  const String behavior = getTokenValue(command, "behavior");
  const String active = getTokenValue(command, "active");
  const unsigned long now = millis();

  lastTriggerEvent = eventName.length() ? eventName : command;

  if (pinText.length() == 0 || behavior.length() == 0) {
    return;
  }

  const uint8_t pin = static_cast<uint8_t>(pinText.toInt());
  const bool activeHigh = active != "low";
  OutputChannel* channel = findOrCreateChannel(pin, activeHigh);
  if (channel == nullptr) {
    return;
  }

  if (behavior == "set") {
    const String value = getTokenValue(command, "value");
    const unsigned long durationMs = parseUnsignedLong(getTokenValue(command, "duration_ms"), 0);
    const bool on = parseBoolLike(value, true);
    executeSetAction(*channel, on, durationMs, now);
    return;
  }

  if (behavior == "pulse") {
    const unsigned long onMs = parseUnsignedLong(getTokenValue(command, "on_ms"), 150);
    const unsigned long offMs = parseUnsignedLong(getTokenValue(command, "off_ms"), 150);
    const uint16_t count = static_cast<uint16_t>(parseUnsignedLong(getTokenValue(command, "count"), 1));
    executePulseAction(*channel, onMs, offMs, count, now);
  }
}

void updateOutputChannels(unsigned long now) {
  for (uint8_t i = 0; i < MAX_OUTPUT_CHANNELS; ++i) {
    OutputChannel& channel = outputChannels[i];
    if (!channel.configured) {
      continue;
    }

    if (channel.behavior == OUTPUT_BEHAVIOR_TIMED_SET) {
      if (channel.nextChangeAt != 0 && now >= channel.nextChangeAt) {
        writeChannel(channel, false);
        channel.behavior = OUTPUT_BEHAVIOR_IDLE;
        channel.nextChangeAt = 0;
      }
      continue;
    }

    if (channel.behavior != OUTPUT_BEHAVIOR_PULSE) {
      continue;
    }

    if (channel.nextChangeAt == 0 || now < channel.nextChangeAt) {
      continue;
    }

    if (channel.currentOn) {
      writeChannel(channel, false);
      if (channel.pulsesRemaining > 0) {
        channel.pulsesRemaining--;
      }
      if (channel.pulsesRemaining == 0) {
        channel.behavior = OUTPUT_BEHAVIOR_IDLE;
        channel.nextChangeAt = 0;
      } else {
        channel.nextChangeAt = now + channel.offMs;
      }
    } else {
      writeChannel(channel, true);
      channel.nextChangeAt = now + channel.onMs;
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

  pinMode(LED3_R, OUTPUT);
  pinMode(LED3_G, OUTPUT);
  pinMode(LED3_B, OUTPUT);
  digitalWrite(LED3_R, HIGH);
  digitalWrite(LED3_G, HIGH);
  digitalWrite(LED3_B, HIGH);
  rgbOff();

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
  updateOutputChannels(now);

  switch (lifecycleState) {
    case STATE_STARTING:
      setRgb(true, true, false);
      break;

    case STATE_DISCONNECTED:
      setRgb(true, true, false);
      break;

    case STATE_WAITING_CONTENT:
      setRgb(true, false, false);
      break;

    case STATE_SWITCHING_GAME:
      setRgb(false, false, true);
      break;

    case STATE_PLAYING:
      setRgb(false, true, false);
      break;

    default:
      rgbOff();
      break;
  }

}
