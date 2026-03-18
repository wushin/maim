# MAIM Runtime Architecture

This README describes the architecture of the MAIM RetroArch watcher runtime.

The system is a **distributed game experience machine** composed of:

- `main.py` — watcher / telemetry engine / router / HTTP UI
- `controller.ino` — ESP32 controller node (BLE + haptics + UDP receiver)
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
- Sends UDP feedback events
- Discovers controller nodes dynamically
- Maintains a live routing registry
- Serves the HTTP status UI and API

It is the **central orchestration runtime**.

---

### `controller.ino` — Controller Node

Each ESP32 controller:

- Acts as a BLE gamepad
- Connects to Wi-Fi
- Listens for UDP events on port `4210`
- Replies to discovery probes on port `4211`
- Executes local haptic and LED routines

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

## Discovery and Routing

### Discovery Registry

The watcher continuously probes the network.

Controllers reply like this:

```text
HELLO_CONTROLLER
id=p1
host=<ip>
port=4210
```

The watcher maintains a routing table conceptually like:

```text
routing_table[id] -> (ip, port, metadata, last_seen)
```

This registry is:

- Visible in `/status`
- Used during event dispatch
- Updated automatically as controllers appear

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
| `p1` | send only to the latest discovered controller `p1` |
| `all` | send to all discovered controllers |
| `self` | route to UNO bridge local effects |
| omitted | fall back to legacy configured UDP targets |

---

## Wire Protocol Rule

UDP payload contains **only the event name**.

Example:

```text
HIT_STRONG
```

Targeting is never encoded into packets.

Routing is entirely watcher-side.

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

- does **not** send UDP
- calls the UNO bridge
- UNO performs the hardware action

---

## Ports

| Port | Purpose |
|------|---------|
| `42069` | HTTP UI |
| `4210` | Controller feedback UDP |
| `4211` | Discovery |

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
