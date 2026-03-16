# RetroArch Watcher Bridge for Arduino UNO Q

This package ports your RetroArch watcher into an Arduino UNO Q App Lab layout:

- `app.yaml` – App Lab manifest
- `python/main.py` – watcher + HTTP UI + Python→MCU Bridge publisher
- `python/requirements.txt` – Python dependencies
- `sketch/sketch.ino` – MCU sketch exposing Bridge RPC handlers and LED state feedback
- `sketch/sketch.yaml` – board/libraries manifest
- `profiles/*.yaml` – copied game profiles

## Bridge behavior

The Python side publishes:

- lifecycle state (`starting`, `disconnected`, `waiting_content`, `switching_game`, `playing`)
- current game slug
- latest payload JSON
- heartbeat unix time
- common gameplay fields when present (`p1_score`, `p2_score`, `p1_hp`, `p2_hp`, `p1_shadow`, `p2_shadow`, and judgment counters)

The MCU sketch:

- exposes `Bridge.provide(...)` handlers for those values
- keeps the latest telemetry in RAM for other MCU-side logic
- uses the builtin LED to reflect lifecycle state

## Environment variables

The Python app still honors the existing watcher env vars:

- `RETROARCH_HOST`
- `RETROARCH_PORT`
- `RETROARCH_TIMEOUT`
- `RETROARCH_CONNECT_RETRY_SECONDS`
- `RETROARCH_TITLE_RETRY_SECONDS`
- `WATCHER_HTTP_HOST`
- `WATCHER_HTTP_PORT`
- `PROFILES_DIR`
- `DEBUG_TITLE`
- `AUTO_SCAFFOLD_PROFILE`
- `SCAFFOLD_MARK_GENERATED`
