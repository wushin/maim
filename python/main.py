#!/usr/bin/env python3
import json
import os
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.parse import unquote, urlparse

import yaml

try:
    from arduino.app_utils import App, Bridge
except ImportError:  # Local/dev fallback.
    class _BridgeStub:
        @staticmethod
        def call(name: str, *args: Any) -> None:
            print(f"[bridge-stub] {name}{args}", file=sys.stderr, flush=True)

    class _AppStub:
        @staticmethod
        def run(user_loop):
            while True:
                user_loop()

    Bridge = _BridgeStub()
    App = _AppStub()

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 55355
DEFAULT_POLL = 0.10
DEFAULT_TIMEOUT = 1.0
DEFAULT_CONNECT_RETRY = 2.0
DEFAULT_TITLE_RETRY = 1.0
DEFAULT_HTTP_HOST = "0.0.0.0"
DEFAULT_HTTP_PORT = 42069
DEFAULT_PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"

STATE_CODES = {
    "starting": 0,
    "disconnected": 1,
    "waiting_content": 2,
    "switching_game": 3,
    "playing": 4,
}

LATEST_PAYLOAD = {
    "timestamp": None,
    "game": None,
    "state": "starting",
    "data": None,
}
LATEST_DEBUG = {
    "active_title": None,
    "profile_path": None,
    "profiles_dir": None,
    "watcher_status": "Starting",
}
STATE_LOCK = threading.Lock()


def debug_enabled() -> bool:
    return os.getenv("DEBUG_TITLE", "").lower() in {"1", "true", "yes", "on"}


def scaffold_enabled() -> bool:
    return os.getenv("AUTO_SCAFFOLD_PROFILE", "").lower() in {"1", "true", "yes", "on"}


def scaffold_mark_generated() -> bool:
    return os.getenv("SCAFFOLD_MARK_GENERATED", "").lower() in {"1", "true", "yes", "on"}


def dbg(msg: str) -> None:
    if debug_enabled():
        print(f"[debug] {msg}", file=sys.stderr, flush=True)


_UNSET = object()


def set_http_state(*, payload=None, active_title=_UNSET, profile_path=_UNSET, profiles_dir=_UNSET, watcher_status=None) -> None:
    with STATE_LOCK:
        if payload is not None:
            LATEST_PAYLOAD.clear()
            LATEST_PAYLOAD.update(payload)
        if active_title is not _UNSET:
            LATEST_DEBUG["active_title"] = active_title
        if profile_path is not _UNSET:
            LATEST_DEBUG["profile_path"] = profile_path
        if profiles_dir is not _UNSET:
            LATEST_DEBUG["profiles_dir"] = profiles_dir
        if watcher_status is not None:
            LATEST_DEBUG["watcher_status"] = watcher_status


def get_http_state() -> dict:
    with STATE_LOCK:
        return {
            "payload": dict(LATEST_PAYLOAD),
            "debug": dict(LATEST_DEBUG),
        }


class ArduinoBridgePublisher:
    def __init__(self) -> None:
        self.last_state: Optional[str] = None
        self.last_game: Optional[str] = None
        self.last_payload_json: Optional[str] = None
        self.last_sent_fields: dict[str, Optional[int]] = {}
        self.last_heartbeat_bucket: Optional[int] = None

    def sync(self, payload: dict) -> None:
        state = str(payload.get("state") or "starting")
        game = str(payload.get("game") or "")
        data = payload.get("data") or {}

        if state != self.last_state:
            self._safe_call("set_lifecycle_state", STATE_CODES.get(state, 0))
            self.last_state = state

        if game != self.last_game:
            self._safe_call("set_game_title", game)
            self.last_game = game

        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if payload_json != self.last_payload_json:
            self._safe_call("set_payload_json", payload_json)
            self.last_payload_json = payload_json

        heartbeat_bucket = int(time.time())
        if heartbeat_bucket != self.last_heartbeat_bucket:
            self._safe_call("set_unix_time", heartbeat_bucket)
            self.last_heartbeat_bucket = heartbeat_bucket

    def _safe_call(self, method: str, *args: Any) -> None:
        try:
            Bridge.call(method, *args)
        except Exception as exc:
            dbg(f"Bridge.call failed for {method}{args}: {exc}")


BRIDGE_PUBLISHER = ArduinoBridgePublisher()
LAST_TRIGGER_VALUES = {}

def process_triggers(profile: dict, current: dict):
    triggers = profile.get("triggers") or []
    for rule in triggers:
        field = rule.get("field")
        op = rule.get("op")
        event = rule.get("send")

        if field not in current:
            continue

        value = current.get(field)
        prev = LAST_TRIGGER_VALUES.get(field)

        LAST_TRIGGER_VALUES[field] = value

        if prev is None or value is None:
            continue

        fire = False

        if op == "<" and value < prev:
            fire = True
        elif op == ">" and value > prev:
            fire = True
        elif op == "=" and value == prev:
            fire = True

        if fire:
            try:
                Bridge.call("trigger_event", str(event))
            except Exception:
                pass



def html_page() -> str:
    return """<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>RetroArch Watcher</title>
<style>
:root {
  color-scheme: dark;
  --bg: #111827;
  --panel: #1f2937;
  --panel2: #0f172a;
  --text: #e5e7eb;
  --muted: #94a3b8;
  --border: #334155;
}
* { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, sans-serif; background: var(--bg); color: var(--text); }
header { padding: 16px 20px; border-bottom: 1px solid var(--border); background: var(--panel2); }
main { display: grid; grid-template-columns: 360px 1fr; gap: 16px; padding: 16px; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 14px; padding: 14px; box-shadow: 0 8px 24px rgba(0,0,0,.22); }
h1, h2 { margin: 0 0 12px 0; }
.small { color: var(--muted); font-size: 13px; }
.grid { display: grid; gap: 8px; }
.label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
.value { background: rgba(255,255,255,.03); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-family: ui-monospace, monospace; overflow-wrap: anywhere; }
.notification { display: block; border-radius: 12px; padding: 12px 14px; border: 1px solid var(--border); margin-bottom: 12px; }
.notification .title { font-weight: 700; margin-bottom: 4px; }
.notification .desc { color: var(--text); font-size: 14px; }
.notification.starting { background: rgba(56,189,248,.10); border-color: rgba(56,189,248,.35); }
.notification.waiting_content,.notification.switching_game { background: rgba(245,158,11,.12); border-color: rgba(245,158,11,.40); }
.notification.disconnected { background: rgba(239,68,68,.12); border-color: rgba(239,68,68,.40); }
.notification.playing { background: rgba(34,197,94,.12); border-color: rgba(34,197,94,.40); }
.notification.unknown { background: rgba(148,163,184,.12); border-color: rgba(148,163,184,.40); }
.state-list { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
.state-item { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 10px 12px; border-radius: 10px; border: 1px solid var(--border); background: rgba(255,255,255,.03); }
.state-item .left { display: flex; align-items: center; gap: 10px; min-width: 0; }
.state-dot { width: 10px; height: 10px; border-radius: 999px; background: #64748b; flex: 0 0 auto; }
.state-text { display: flex; flex-direction: column; min-width: 0; }
.state-name { font-weight: 600; }
.state-desc { color: var(--muted); font-size: 12px; }
.state-badge { font-size: 11px; padding: 4px 8px; border-radius: 999px; border: 1px solid var(--border); color: var(--muted); }
.state-item.active { border-width: 2px; }
.state-item.active .state-badge { color: white; font-weight: 700; }
.state-item.active.playing { border-color: rgba(34,197,94,.5); background: rgba(34,197,94,.12); }
.state-item.active.playing .state-dot,.state-item.active.playing .state-badge { background: #22c55e; }
.state-item.active.waiting_content { border-color: rgba(245,158,11,.5); background: rgba(245,158,11,.12); }
.state-item.active.waiting_content .state-dot,.state-item.active.waiting_content .state-badge { background: #f59e0b; }
.state-item.active.switching_game { border-color: rgba(168,85,247,.5); background: rgba(168,85,247,.12); }
.state-item.active.switching_game .state-dot,.state-item.active.switching_game .state-badge { background: #a855f7; }
.state-item.active.disconnected,.state-item.active.starting { border-color: rgba(239,68,68,.5); background: rgba(239,68,68,.12); }
.state-item.active.disconnected .state-dot,.state-item.active.disconnected .state-badge,.state-item.active.starting .state-dot,.state-item.active.starting .state-badge { background: #ef4444; }
.state-item.active.unknown { border-color: rgba(56,189,248,.5); background: rgba(56,189,248,.12); }
.state-item.active.unknown .state-dot,.state-item.active.unknown .state-badge { background: #38bdf8; }
button { cursor: pointer; border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; color: var(--text); background: #2563eb; font-weight: 600; }
button.secondary { background: #334155; }
button:disabled { opacity: .5; cursor: default; }
select, textarea { width: 100%; border-radius: 10px; border: 1px solid var(--border); background: #0b1220; color: var(--text); padding: 10px; font: inherit; }
textarea { min-height: 520px; resize: vertical; font-family: ui-monospace, monospace; line-height: 1.35; }
.toolbar { display: flex; gap: 10px; align-items: center; margin-bottom: 12px; }
pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, monospace; }
@media (max-width: 900px) { main { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1>RetroArch Watcher</h1>
  <div class=\"small\">Status + profile editor + Arduino bridge</div>
</header>
<main>
  <section class=\"card\">
    <h2>Lifecycle</h2>
    <div id=\"notificationBox\" class=\"notification starting visible\">
      <div id=\"notificationTitle\" class=\"title\">Starting</div>
      <div id=\"notificationDesc\" class=\"desc\">Watcher is starting up.</div>
    </div>
    <div class=\"grid\" style=\"margin-bottom:12px;\">
      <div><div class=\"label\">Watcher</div><div id=\"watcherStatus\" class=\"value\">Starting</div></div>
      <div><div class=\"label\">Current Game</div><div id=\"currentGame\" class=\"value\">—</div></div>
      <div><div class=\"label\">Active Title</div><div id=\"activeTitle\" class=\"value\">—</div></div>
      <div><div class=\"label\">Profile Path</div><div id=\"profilePath\" class=\"value\">—</div></div>
    </div>
    <ul id=\"stateList\" class=\"state-list\">
      <li class=\"state-item\" data-state=\"starting\"><div class=\"left\"><span class=\"state-dot\"></span><div class=\"state-text\"><span class=\"state-name\">Starting</span><span class=\"state-desc\">Watcher booting up</span></div></div><span class=\"state-badge\">idle</span></li>
      <li class=\"state-item\" data-state=\"disconnected\"><div class=\"left\"><span class=\"state-dot\"></span><div class=\"state-text\"><span class=\"state-name\">Waiting for RetroArch</span><span class=\"state-desc\">RetroArch not responding</span></div></div><span class=\"state-badge\">idle</span></li>
      <li class=\"state-item\" data-state=\"waiting_content\"><div class=\"left\"><span class=\"state-dot\"></span><div class=\"state-text\"><span class=\"state-name\">Connected Waiting for Content</span><span class=\"state-desc\">Connected, no game loaded yet</span></div></div><span class=\"state-badge\">idle</span></li>
      <li class=\"state-item\" data-state=\"switching_game\"><div class=\"left\"><span class=\"state-dot\"></span><div class=\"state-text\"><span class=\"state-name\">Switching Game</span><span class=\"state-desc\">Title changed, reloading profile</span></div></div><span class=\"state-badge\">idle</span></li>
      <li class=\"state-item\" data-state=\"playing\"><div class=\"left\"><span class=\"state-dot\"></span><div class=\"state-text\"><span class=\"state-name\">Current Game</span><span class=\"state-desc\">Telemetry active</span></div></div><span class=\"state-badge\">idle</span></li>
    </ul>
    <div style=\"margin-top:12px;\"><div class=\"label\">Most Recent JSON</div><div class=\"value\"><pre id=\"latestJson\">{}</pre></div></div>
  </section>
  <section class=\"card\">
    <h2>Profiles</h2>
    <div class=\"toolbar\"><select id=\"profileSelect\"></select><button id=\"refreshProfiles\" class=\"secondary\">Refresh</button><button id=\"saveProfile\">Save</button></div>
    <div class=\"small\" id=\"editorStatus\">Choose a profile.</div>
    <textarea id=\"profileEditor\" spellcheck=\"false\"></textarea>
  </section>
</main>
<script>
const STATUS_NOTIFICATIONS = {
  starting: { title: "Watcher Starting", desc: "The watcher is booting and waiting to establish its first state." },
  disconnected: { title: "Waiting for RetroArch", desc: "RetroArch is not responding right now. The watcher will keep retrying." },
  waiting_content: { title: "Connected, Waiting for Content", desc: "RetroArch is reachable, but no game content is loaded yet." },
  switching_game: { title: "Switching Game", desc: "A title change was detected. Reloading the matching profile." },
  playing: { title: "Current Game Active", desc: "Watcher is connected, a profile is loaded, and live telemetry is updating." },
  unknown: { title: "Unknown State", desc: "The watcher returned a state the UI does not recognize yet." }
};
const els = {
  watcherStatus: document.getElementById("watcherStatus"), currentGame: document.getElementById("currentGame"), activeTitle: document.getElementById("activeTitle"), profilePath: document.getElementById("profilePath"), latestJson: document.getElementById("latestJson"), profileSelect: document.getElementById("profileSelect"), refreshProfiles: document.getElementById("refreshProfiles"), saveProfile: document.getElementById("saveProfile"), profileEditor: document.getElementById("profileEditor"), editorStatus: document.getElementById("editorStatus"), notificationBox: document.getElementById("notificationBox"), notificationTitle: document.getElementById("notificationTitle"), notificationDesc: document.getElementById("notificationDesc"), stateList: document.getElementById("stateList"),
};
function renderNotification(state) {
  const key = STATUS_NOTIFICATIONS[state] ? state : "unknown";
  const meta = STATUS_NOTIFICATIONS[key];
  els.notificationBox.className = "notification " + key + " visible";
  els.notificationTitle.textContent = meta.title;
  els.notificationDesc.textContent = meta.desc;
}
function renderStateList(state) {
  const key = STATUS_NOTIFICATIONS[state] ? state : "unknown";
  els.stateList.querySelectorAll(".state-item").forEach((item) => {
    const itemState = item.dataset.state;
    const isActive = itemState === key;
    item.classList.remove("active", "starting", "disconnected", "waiting_content", "switching_game", "playing", "unknown");
    if (isActive) item.classList.add("active", key);
    item.querySelector(".state-badge").textContent = isActive ? "active" : "idle";
  });
}
async function loadStatus() {
  const res = await fetch("/status", { cache: "no-store" });
  const data = await res.json();
  const payload = data.payload || {};
  const debug = data.debug || {};
  const state = payload.state || "starting";
  renderNotification(state); renderStateList(state);
  els.watcherStatus.textContent = debug.watcher_status || "—";
  els.currentGame.textContent = payload.game || "—";
  els.activeTitle.textContent = debug.active_title || "—";
  els.profilePath.textContent = debug.profile_path || "—";
  els.latestJson.textContent = JSON.stringify(payload, null, 2);
}
async function loadProfiles(selectedName) {
  const res = await fetch("/api/profiles", { cache: "no-store" });
  const data = await res.json();
  const profiles = data.profiles || [];
  const current = selectedName || els.profileSelect.value || "";
  els.profileSelect.innerHTML = "";
  for (const name of profiles) {
    const opt = document.createElement("option");
    opt.value = name; opt.textContent = name; els.profileSelect.appendChild(opt);
  }
  if (profiles.length === 0) { els.editorStatus.textContent = "No profiles found."; els.profileEditor.value = ""; return; }
  const target = profiles.includes(current) ? current : profiles[0];
  els.profileSelect.value = target; await loadProfile(target);
}
async function loadProfile(name) {
  if (!name) return;
  const res = await fetch("/api/profile/" + encodeURIComponent(name), { cache: "no-store" });
  const data = await res.json();
  if (!res.ok) { els.editorStatus.textContent = data.error || "Failed to load profile."; return; }
  els.profileEditor.value = data.content || "";
  els.editorStatus.textContent = "Editing " + name;
}
async function saveProfile() {
  const name = els.profileSelect.value;
  if (!name) return;
  els.saveProfile.disabled = true;
  try {
    const res = await fetch("/api/profile/" + encodeURIComponent(name), { method: "POST", headers: { "Content-Type": "text/plain; charset=utf-8" }, body: els.profileEditor.value });
    const data = await res.json();
    if (!res.ok) { els.editorStatus.textContent = data.error || "Save failed."; return; }
    els.editorStatus.textContent = "Saved " + name;
    await loadStatus();
  } finally { els.saveProfile.disabled = false; }
}
els.profileSelect.addEventListener("change", () => loadProfile(els.profileSelect.value));
els.refreshProfiles.addEventListener("click", () => loadProfiles());
els.saveProfile.addEventListener("click", saveProfile);
loadProfiles().catch(console.error); loadStatus().catch(console.error); setInterval(() => loadStatus().catch(console.error), 1000); setInterval(() => loadProfiles(els.profileSelect.value).catch(console.error), 5000);
</script>
</body>
</html>
"""


class WatcherHTTPHandler(BaseHTTPRequestHandler):
    server_version = "RetroArchWatcherHTTP/1.0"

    def _send_json(self, obj: dict, status: int = 200) -> None:
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, html: str, status: int = 200) -> None:
        raw = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _profiles_dir(self) -> Path:
        state = get_http_state()
        raw = state["debug"].get("profiles_dir") or str(DEFAULT_PROFILES_DIR)
        return Path(raw)

    def _profile_list(self) -> list[str]:
        pdir = self._profiles_dir()
        if not pdir.exists():
            return []
        return sorted(p.name for p in pdir.glob("*.yaml"))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        state = get_http_state()
        if path == "/":
            self._send_html(html_page())
            return
        if path == "/status":
            self._send_json(state)
            return
        if path == "/api":
            self._send_json(state["payload"])
            return
        if path == "/api/profiles":
            self._send_json({"profiles": self._profile_list()})
            return
        if path.startswith("/api/profile/"):
            name = unquote(path[len("/api/profile/"):]).strip()
            if not name.endswith(".yaml") or "/" in name or "\\" in name or name.startswith("."):
                self._send_json({"error": "invalid_profile_name"}, status=400)
                return
            profile_path = self._profiles_dir() / name
            if not profile_path.exists():
                self._send_json({"error": "profile_not_found", "profile": name}, status=404)
                return
            self._send_json({"profile": name, "path": str(profile_path), "content": profile_path.read_text(encoding="utf-8")})
            return
        if path == "/health":
            payload = state["payload"]
            http_state = payload.get("state")
            status = 200 if http_state in {"playing", "waiting_content"} else 503
            self._send_json({"ok": status == 200, "state": http_state, "game": payload.get("game"), "timestamp": payload.get("timestamp")}, status=status)
            return
        self._send_json({"error": "not_found", "path": path}, status=404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/profile/"):
            name = unquote(path[len("/api/profile/"):]).strip()
            if not name.endswith(".yaml") or "/" in name or "\\" in name or name.startswith("."):
                self._send_json({"error": "invalid_profile_name"}, status=400)
                return
            pdir = self._profiles_dir()
            pdir.mkdir(parents=True, exist_ok=True)
            profile_path = pdir / name
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            try:
                parsed_yaml = yaml.safe_load(raw)
                if parsed_yaml is None:
                    parsed_yaml = {}
                if not isinstance(parsed_yaml, dict):
                    raise ValueError("YAML root must be a mapping/object.")
            except Exception as exc:
                self._send_json({"error": f"invalid_yaml: {exc}"}, status=400)
                return
            profile_path.write_text(raw, encoding="utf-8")
            self._send_json({"ok": True, "profile": name, "path": str(profile_path)})
            return
        self._send_json({"error": "not_found", "path": path}, status=404)

    def log_message(self, format, *args):
        return


def start_http_server(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), WatcherHTTPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"HTTP endpoint listening on http://{host}:{port}", file=sys.stderr, flush=True)
    return server


class RA:
    def __init__(self, host: str, port: int, timeout: float = DEFAULT_TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout

    def cmd(self, command: str) -> str:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        try:
            sock.sendto(command.encode(), (self.host, self.port))
            data, _ = sock.recvfrom(4096)
            decoded = data.decode(errors="replace").strip()
            dbg(f"{command} -> {decoded!r}")
            return decoded
        finally:
            sock.close()

    def status(self) -> str:
        return self.cmd("GET_STATUS")

    def title(self) -> str:
        try:
            return self.cmd("GET_TITLE")
        except (TimeoutError, socket.timeout):
            dbg("GET_TITLE timed out")
            return ""
        except OSError as exc:
            dbg(f"GET_TITLE socket error: {exc}")
            return ""

    def read_u8(self, addr: int):
        try:
            response = self.cmd(f"READ_CORE_MEMORY {addr:04x} 1")
            if "-1" in response:
                return None
            return int(response.split()[-1], 16)
        except (TimeoutError, socket.timeout):
            dbg(f"READ_CORE_MEMORY {addr:04x} timed out")
            return None
        except (OSError, ValueError, IndexError) as exc:
            dbg(f"READ_CORE_MEMORY {addr:04x} failed: {exc}")
            return None

    def read_u16(self, addr: int, endian: str = "little"):
        a = self.read_u8(addr)
        b = self.read_u8(addr + 1)
        if a is None or b is None:
            return None
        return a | (b << 8) if endian == "little" else (a << 8) | b


def slug(title: str) -> str:
    text = title.lower().strip()
    text = re.sub(r"\.[a-z0-9]+$", "", text)
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def clean_title(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    upper = text.upper()
    if upper in {"N/A", "NULL"}:
        return ""
    text = re.sub(r"[\s|,;_-]*crc(?:32)?\s*=\s*[0-9a-fA-F]+.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[\s|,;_-]*md5\s*=\s*[0-9a-fA-F]+.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[\s|,;_-]*sha1\s*=\s*[0-9a-fA-F]+.*$", "", text, flags=re.IGNORECASE)
    if re.fullmatch(r"(crc(?:32)?|md5|sha1)\s*=\s*[0-9a-fA-F]+", text, flags=re.IGNORECASE):
        return ""
    parts = [p.strip() for p in re.split(r"[|,;]", text) if p.strip()]
    if parts:
        for part in parts:
            if not re.search(r"^(crc(?:32)?|md5|sha1)\s*=", part, flags=re.IGNORECASE):
                text = part
                break
    text = re.sub(r"([._-]?v?\d+(?:[._-]\d+)+)$", "", text, flags=re.IGNORECASE)
    text = text.strip(" -_|,;")
    dbg(f"clean_title -> {text!r} from raw {raw!r}")
    return text


def parse_system_from_status(status: str) -> Optional[str]:
    raw = (status or "").strip()
    if not raw or " " not in raw or "," not in raw:
        return None
    try:
        payload = raw.split(" ", 2)[2]
        parts = [p.strip() for p in payload.split(",")]
        if parts and parts[0]:
            system = parts[0].lower()
            system = re.sub(r"[^a-z0-9_+-]+", "", system)
            return system or None
    except Exception as exc:
        dbg(f"parse_system_from_status failed: {exc}")
    return None


def parse_status_for_title(status: str) -> str:
    raw = (status or "").strip()
    if not raw:
        return ""
    upper = raw.upper()
    for bad in ("CONTENTLESS", "NO GAME", "NO CONTENT", "NOT RUNNING", "NULL", "N/A"):
        if bad in upper:
            dbg(f"status says no content: {raw!r}")
            return ""
    if " " in raw and "," in raw:
        try:
            payload = raw.split(" ", 2)[2]
            parts = [p.strip() for p in payload.split(",")]
            if len(parts) >= 2:
                candidate = clean_title(parts[1])
                if candidate:
                    return candidate
            for part in parts:
                candidate = clean_title(part)
                if candidate:
                    return candidate
        except Exception as exc:
            dbg(f"parse_status_for_title structured parse failed: {exc}")
    for splitter in ("|", ","):
        if splitter in raw:
            parts = [p.strip() for p in raw.split(splitter) if p.strip()]
            for part in parts:
                candidate = clean_title(part)
                if candidate:
                    return candidate
    return ""


def wait_for_ra(client: RA, retry_seconds: float = DEFAULT_CONNECT_RETRY) -> None:
    while True:
        try:
            status = client.status()
            set_http_state(watcher_status="Connected to RetroArch")
            print(f"Connected to RetroArch: {status}", file=sys.stderr, flush=True)
            return
        except Exception as exc:
            set_http_state(watcher_status="Waiting for RetroArch")
            print(f"Waiting for RetroArch... {exc}", file=sys.stderr, flush=True)
            time.sleep(retry_seconds)


def resolve_title(client: RA) -> Tuple[Optional[str], str]:
    raw_title = client.title().strip()
    title = clean_title(raw_title)
    dbg(f"resolve_title GET_TITLE raw={raw_title!r} cleaned={title!r}")
    if title:
        return title, "ok"
    try:
        status = client.status()
    except (TimeoutError, socket.timeout, OSError):
        return None, "timeout"
    except Exception:
        return None, "timeout"
    parsed = parse_status_for_title(status)
    if parsed:
        return parsed, "ok"
    return None, "no_content"


def get_status_system(client: RA) -> Optional[str]:
    try:
        return parse_system_from_status(client.status())
    except Exception as exc:
        dbg(f"get_status_system failed: {exc}")
        return None


def create_profile_scaffold(profile_path: Path, game_slug: str, system: Optional[str]) -> bool:
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"game": game_slug, "poll_seconds": DEFAULT_POLL, "fields": {}}
    if system:
        payload["system"] = system
    if scaffold_mark_generated():
        payload["generated"] = True
    try:
        with profile_path.open("x", encoding="utf-8") as handle:
            yaml.safe_dump(payload, handle, sort_keys=False)
        return True
    except FileExistsError:
        return False


def load_profile(profile_path: Path) -> dict:
    return yaml.safe_load(profile_path.read_text(encoding="utf-8"))


def profile_has_fields(profile: dict) -> bool:
    fields = profile.get("fields")
    return isinstance(fields, dict) and len(fields) > 0


def wait_for_profile(client: RA, profiles_dir: Path, retry_seconds: float = DEFAULT_TITLE_RETRY, scaffolded_once: Optional[set] = None):
    if scaffolded_once is None:
        scaffolded_once = set()
    while True:
        try:
            title, state = resolve_title(client)
        except Exception as exc:
            dbg(f"wait_for_profile resolve_title exception: {exc}")
            title, state = None, "timeout"
        if state == "timeout":
            set_http_state(payload=make_payload(None, "disconnected", None), active_title=None, profile_path=None, watcher_status="Waiting for RetroArch")
            publish_bridge_payload(get_http_state()["payload"])
            time.sleep(retry_seconds)
            continue
        if state == "no_content":
            set_http_state(payload=make_payload(None, "waiting_content", None), active_title=None, profile_path=None, watcher_status="Connected Waiting for Content")
            publish_bridge_payload(get_http_state()["payload"])
            time.sleep(retry_seconds)
            continue
        assert title is not None
        game_slug = slug(title)
        profile_path = profiles_dir / f"{game_slug}.yaml"
        if profile_path.exists():
            profile = load_profile(profile_path)
            if profile_has_fields(profile):
                set_http_state(active_title=title, profile_path=str(profile_path), watcher_status=f"Current Game: {title}")
                return profile, title
            set_http_state(active_title=title, profile_path=str(profile_path), watcher_status=f"Current Game: {title} (Profile Empty)")
            time.sleep(retry_seconds)
            continue
        if scaffold_enabled() and game_slug and game_slug not in scaffolded_once:
            system = get_status_system(client)
            create_profile_scaffold(profile_path, game_slug, system)
            scaffolded_once.add(game_slug)
            set_http_state(active_title=title, profile_path=str(profile_path), watcher_status=f"Current Game: {title} (Scaffolded)")
            time.sleep(retry_seconds)
            continue
        set_http_state(active_title=title, profile_path=str(profile_path), watcher_status=f"Current Game: {title}")
        time.sleep(retry_seconds)


def snapshot(client: RA, fields: dict) -> dict:
    out = {}
    for name, spec in fields.items():
        addr = spec["addr"]
        if isinstance(addr, str):
            addr = int(addr, 0)
        if spec["type"] == "u8":
            out[name] = client.read_u8(addr)
        elif spec["type"] == "u16":
            out[name] = client.read_u16(addr, spec.get("endian", "little"))
        else:
            out[name] = None
    return out


def make_payload(game_title: Optional[str], state: str, data) -> dict:
    return {"timestamp": time.time(), "game": slug(game_title) if game_title else None, "state": state, "data": data}


def publish_bridge_payload(payload: dict) -> None:
    BRIDGE_PUBLISHER.sync(payload)


def emit_payload(game_title: Optional[str], state: str, data) -> None:
    payload = make_payload(game_title, state, data)
    set_http_state(payload=payload)
    publish_bridge_payload(payload)
    print(json.dumps(payload, separators=(",", ":")), flush=True)


def reconnect_and_reload_profile(client: RA, profiles_dir: Path, connect_retry: float, title_retry: float, scaffolded_once: set):
    wait_for_ra(client, retry_seconds=connect_retry)
    return wait_for_profile(client, profiles_dir, retry_seconds=title_retry, scaffolded_once=scaffolded_once)


def user_loop() -> None:
    global RUNTIME
    if RUNTIME is None:
        return
    RUNTIME.tick()


class Runtime:
    def __init__(self) -> None:
        self.host = os.getenv("HOST_IP", DEFAULT_HOST)
        self.port = int(os.getenv("RETROARCH_PORT", DEFAULT_PORT))
        self.timeout = float(os.getenv("RETROARCH_TIMEOUT", DEFAULT_TIMEOUT))
        self.profiles_dir = Path(os.getenv("PROFILES_DIR", str(DEFAULT_PROFILES_DIR)))
        self.connect_retry = float(os.getenv("RETROARCH_CONNECT_RETRY_SECONDS", DEFAULT_CONNECT_RETRY))
        self.title_retry = float(os.getenv("RETROARCH_TITLE_RETRY_SECONDS", DEFAULT_TITLE_RETRY))
        self.http_host = os.getenv("WATCHER_HTTP_HOST", DEFAULT_HTTP_HOST)
        self.http_port = int(os.getenv("WATCHER_HTTP_PORT", DEFAULT_HTTP_PORT))
        self.client = RA(self.host, self.port, timeout=self.timeout)
        self.scaffolded_once: set[str] = set()
        self.profile: Optional[dict] = None
        self.active_title: Optional[str] = None
        self.poll = DEFAULT_POLL
        self.fields: dict = {}
        self.last_data = None
        self.last_state = None
        self.next_due = 0.0

    def start(self) -> None:
        set_http_state(payload=make_payload(None, "starting", None), profiles_dir=str(self.profiles_dir), profile_path=None, active_title=None, watcher_status="Starting")
        publish_bridge_payload(get_http_state()["payload"])
        start_http_server(self.http_host, self.http_port)
        self.profile, self.active_title = reconnect_and_reload_profile(self.client, self.profiles_dir, self.connect_retry, self.title_retry, self.scaffolded_once)
        self.poll = float(self.profile.get("poll_seconds", DEFAULT_POLL))
        self.fields = self.profile["fields"]
        self.next_due = time.monotonic()

    def tick(self) -> None:
        if time.monotonic() < self.next_due:
            time.sleep(0.01)
            return
        self.next_due = time.monotonic() + self.poll
        try:
            current_title, state = resolve_title(self.client)
            if state == "timeout":
                if self.last_state != "disconnected":
                    emit_payload(None, "disconnected", None)
                    self.last_state = "disconnected"
                set_http_state(active_title=None, profile_path=None, watcher_status="Waiting for RetroArch")
                self.profile, self.active_title = reconnect_and_reload_profile(self.client, self.profiles_dir, self.connect_retry, self.title_retry, self.scaffolded_once)
                self._reload_profile_state()
                return
            if state == "no_content":
                if self.last_state != "waiting_content":
                    emit_payload(None, "waiting_content", None)
                    self.last_state = "waiting_content"
                set_http_state(active_title=None, profile_path=None, watcher_status="Connected Waiting for Content")
                self.profile, self.active_title = reconnect_and_reload_profile(self.client, self.profiles_dir, self.connect_retry, self.title_retry, self.scaffolded_once)
                self._reload_profile_state()
                return
            assert current_title is not None
            if slug(current_title) != slug(self.active_title or ""):
                emit_payload(self.active_title, "switching_game", None)
                set_http_state(watcher_status=f"Current Game: {current_title}")
                self.profile, self.active_title = wait_for_profile(self.client, self.profiles_dir, retry_seconds=self.title_retry, scaffolded_once=self.scaffolded_once)
                self._reload_profile_state()
                return
            current = snapshot(self.client, self.fields)
            process_triggers(self.profile, current)
            if current != self.last_data or self.last_state != "playing":
                set_http_state(watcher_status=f"Current Game: {self.active_title}")
                emit_payload(self.active_title, "playing", current)
                self.last_data = current
                self.last_state = "playing"
        except (TimeoutError, socket.timeout, OSError) as exc:
            if self.last_state != "disconnected":
                emit_payload(self.active_title, "disconnected", None)
                self.last_state = "disconnected"
            set_http_state(watcher_status="Waiting for RetroArch")
            print(f"RetroArch connection lost: {exc}", file=sys.stderr, flush=True)
            self.profile, self.active_title = reconnect_and_reload_profile(self.client, self.profiles_dir, self.connect_retry, self.title_retry, self.scaffolded_once)
            self._reload_profile_state()

    def _reload_profile_state(self) -> None:
        assert self.profile is not None
        self.poll = float(self.profile.get("poll_seconds", DEFAULT_POLL))
        self.fields = self.profile["fields"]
        self.last_data = None
        self.last_state = None
        self.next_due = time.monotonic() + min(self.poll, 0.10)


RUNTIME: Optional[Runtime] = None


def main() -> None:
    global RUNTIME
    RUNTIME = Runtime()
    RUNTIME.start()
    App.run(user_loop=user_loop)


if __name__ == "__main__":
    main()
