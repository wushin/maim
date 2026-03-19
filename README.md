# MAIM Runtime Architecture

This README describes the architecture of the MAIM RetroArch watcher runtime.

The system is a **distributed game experience machine** composed of:

- `main.py` — watcher / telemetry engine / router / HTTP UI
- `controller.ino` — ESP32 controller node (BLE + haptics + HTTP receiver)
- `sketch.ino` — UNO console-side effects engine
- YAML profiles — game-specific telemetry + trigger logic

---

## System Overview

The runtime follows a strict pipeline:

```text
RetroArch telemetry → trigger rules → event names → routing → device-local behavior
```

Each layer has one responsibility.

| Layer | Responsibility |
|------|----------------|
| Telemetry | Raw emulator memory reads |
| Triggers | Interpret gameplay state |
| Events | Hardware / experience vocabulary |
| Routing | Decide destination |
| Receivers | Decide physical expression |

The watcher is a **director/router**, not a behavior database.

---

## Runtime Roles

### `main.py` — Watcher / Director / Router

The watcher:

- Talks to RetroArch via UDP command interface
- Resolves current game and loads the YAML profile
- Reads configured telemetry fields from emulator RAM
- Evaluates trigger conditions
- Emits lifecycle state to the UNO bridge
- Sends HTTP feedback events to controllers
- Maintains a live controller routing registry via HTTP register + heartbeat
- Serves the HTTP status UI and API

It is the **central orchestration runtime**.

---

### `controller.ino` — Controller Node

Each ESP32 controller:

- Acts as a BLE gamepad
- Connects to Wi‑Fi
- Runs an HTTP server for event delivery
- Registers itself with the watcher via HTTP
- Sends periodic HTTP heartbeats
- Executes local haptic and LED routines when receiving HTTP event commands

Controllers are **receivers/interpreters**.

They do **not** know game telemetry semantics.

---

### `sketch.ino` — UNO Console Effects Engine

The UNO:

- Renders lifecycle states visually
- Drives the LED matrix marquee
- Executes pin-level output behaviors
- Accepts `trigger_event` bridge calls

This is the **local experience output path**.

This is where `target: self` events are routed.

---

## Controller Presence and Routing

### Controller Registry

Controllers actively register themselves with the watcher using HTTP:

```
POST /api/controllers/register
```

They maintain presence using periodic:

```
POST /api/controllers/heartbeat
```

The watcher maintains a routing table conceptually like:

```text
routing_table[id] -> (host, port, metadata, last_seen)
```

This registry is:

- Visible in `/status`
- Used during event dispatch
- Updated automatically as controllers heartbeat

No static controller configuration is required.

---

## Target-Aware Routing

Trigger events support both simple and object forms.

### Simple event

```yaml
events:
  - HIT_STRONG
```

### Targeted event

```yaml
events:
  - name: HIT_STRONG
    target: p1
```

### Routing semantics

| target | behavior |
|--------|----------|
| `p1` | send HTTP event to controller `p1` |
| `all` | send to all registered controllers |
| `self` | route to UNO bridge local effects |
| omitted | fall back to legacy configured targets |

---

## Event Delivery Protocol

Controller events are delivered via HTTP JSON.

Example:

```text
POST http://<controller-host>:<port>/event
```

Body:

```json
{ "event": "HIT_STRONG" }
```

Targeting is resolved watcher‑side.

---

## Event Normalization

The watcher supports:

- string events
- object events
- legacy event definitions
- fallback event-name dispatch

Valid examples:

```yaml
- HIT_STRONG
```

```yaml
- name: HIT_STRONG
  target: p1
```

```yaml
- name: HIT_STRONG
  target: self
```

---

## Trigger Engine Flow

```text
snapshot -> compare previous -> condition matched -> normalize event -> route -> dispatch
```

Supported comparisons include:

- `increased`
- `decreased`
- `changed`
- `equal`
- `above`
- `below`
- `crossed_above`
- `crossed_below`
- delta comparisons

---

## Experience Vocabulary Philosophy

Profiles translate **game facts into experience events**.

Example:

Game fact:

```text
p1_hp decreased
```

Experience event:

```text
HIT_STRONG
```

The controller decides how to physically express that event.

---

## Example Modern Profile

```yaml
triggers:
  - name: p1_hp_down
    when:
      field: p1_hp
      compare: decreased
    events:
      - name: HIT_STRONG
        target: p1
```

---

## UNO Local Routing Example

```yaml
events:
  - name: FOG_PULSE
    target: self
```

Watcher behavior:

- does **not** send controller HTTP event
- calls the UNO bridge
- UNO performs the hardware action

---

## Ports

| Port | Purpose |
|------|---------|
| `42069` | HTTP UI |
| Controller HTTP Port | Event delivery + heartbeat |

---

## Architecture Summary

MAIM is a **distributed game experience runtime**.

- PC = telemetry intelligence + routing
- Controllers = tactile feedback nodes
- UNO = atmospheric console effects

Key principles:

- Telemetry stays game-specific
- Events stay experience-specific
- Routing stays centralized
- Behavior stays local

---

## Future Runtime Evolution

Likely next maturity steps:

- Controller TTL / stale eviction
- Routing metrics
- Profile hot reload
- Capability negotiation
- Event priority / rate limiting
