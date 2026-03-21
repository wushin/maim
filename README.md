
# MAIM Runtime Architecture

This README describes the architecture of the MAIM RetroArch watcher runtime.

The system is a **distributed game experience machine** composed of:

- `main.py` — watcher / telemetry engine / router / HTTP UI
- `controller.ino` — ESP32 controller node (BLE + haptics + HTTP receiver)
- `sketch.ino` — UNO console-side effects engine
- YAML profiles — game-specific telemetry + trigger logic + experience routing

---

## System Overview

The runtime follows a strict pipeline:

```text
RetroArch telemetry → trigger rules → experience events → command payloads → routing → device-local behavior
```

Each layer has one responsibility.

| Layer | Responsibility |
|------|----------------|
| Telemetry | Raw emulator memory reads |
| Triggers | Interpret gameplay state |
| Events | Experience vocabulary |
| Commands | Device-understandable instructions |
| Routing | Decide destination |
| Receivers | Execute physical expression |

The watcher is a **director/router**, not a behavior database.

---

## Runtime Roles

### `main.py` — Watcher / Director / Router

The watcher:

- Talks to RetroArch via command interface
- Resolves current game and loads the YAML profile
- Reads configured telemetry fields from emulator RAM
- Evaluates trigger conditions
- Emits lifecycle state to the UNO bridge
- Sends HTTP command payloads to controllers
- Maintains a live controller routing registry via HTTP register + heartbeat
- Serves the HTTP status UI and API

It is the **central orchestration runtime**.

The watcher does **not** know how devices implement feedback.

It only sends **commands declared in the profile**.

---

### `controller.ino` — Controller Node

Each ESP32 controller:

- Acts as a BLE gamepad
- Connects to Wi-Fi
- Runs an HTTP server for command delivery
- Registers itself with the watcher via HTTP
- Sends periodic HTTP heartbeats
- Executes local haptic / LED routines when receiving command strings

Controllers are **generic experience interpreters**.

They only understand **device commands** such as:

```
RUMBLE 1 180
PULSE 2 70 50 2
STOP 1
REPEAT 3 120 80
```

---

### `sketch.ino` — UNO Console Effects Engine

The UNO:

- Renders lifecycle states visually
- Drives the LED matrix marquee
- Executes pin-level atmosphere outputs
- Accepts routed experience events from the watcher

This is the **local experience output path**.

---

## Controller Presence and Routing

Controllers register:

```
POST /api/controllers/register
```

Maintain presence:

```
POST /api/controllers/heartbeat
```

Watcher routing table:

```
routing_table[id] -> (host, port, metadata, last_seen)
```

---

## Modern Event System

Profiles define **named experience events**, each containing **device command payloads**.

```yaml
events:
  p1_hp_down_rumble:
    commands:
      - command: "RUMBLE 1 180"
        target: p1
```

Triggers reference event names.

---

## Event Delivery Protocol

```
POST http://<controller-host>:<port>/event
```

Body:

```json
{ "event": "RUMBLE 1 180" }
```

---

## Architecture Summary

MAIM is a **distributed game experience runtime**.

- UNO Debian = telemetry intelligence + routing
- Controllers = tactile feedback interpreters
- UNO Arduino = atmospheric console effects

Key principles:

- Telemetry stays game-specific
- Experience events stay semantic
- Commands stay device-specific
- Routing stays centralized
- Behavior stays local  
