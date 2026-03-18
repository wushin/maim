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
DEFAULT_FEEDBACK_HOST = "255.255.255.255"
DEFAULT_FEEDBACK_PORT = 4210
DEFAULT_DISCOVERY_PORT = 4211
DEFAULT_DISCOVERY_INTERVAL = 5.0
DEFAULT_CONTROLLER_STALE_SECONDS = 15.0

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
_PROFILE_CACHE: dict[str, dict[str, Any]] = {}


def debug_enabled() -> bool:
    return os.getenv("DEBUG_TITLE", "").lower() in {"1", "true", "yes", "on"}


def scaffold_enabled() -> bool:
    return os.getenv("AUTO_SCAFFOLD_PROFILE", "").lower() in {"1", "true", "yes", "on"}


def scaffold_mark_generated() -> bool:
    return os.getenv("SCAFFOLD_MARK_GENERATED", "").lower() in {"1", "true", "yes", "on"}


def dbg(msg: str) -> None:
    if debug_enabled():
        print(f"[debug] {msg}", file=sys.stderr, flush=True)


class ProfileValidationError(ValueError):
    pass


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
        state = {
            "payload": dict(LATEST_PAYLOAD),
            "debug": dict(LATEST_DEBUG),
        }
    state["debug"]["feedback_targets"] = [f"{host}:{port}" for host, port in FEEDBACK_DISPATCHER.targets]
    state["debug"]["discovered_controllers"] = FEEDBACK_DISPATCHER.get_known_controllers()
    return state


class ArduinoBridgePublisher:
    def __init__(self) -> None:
        self.last_state: Optional[str] = None
        self.last_game: Optional[str] = None
        self.last_payload_json: Optional[str] = None
        self.last_heartbeat_bucket: Optional[int] = None

    def sync(self, payload: dict) -> None:
        state = str(payload.get("state") or "starting")
        game = str(payload.get("game") or "")

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


class FeedbackUDPDispatcher:
    def __init__(self, targets: list[tuple[str, int]]) -> None:
        self.targets = [(str(host).strip(), int(port)) for host, port in targets if str(host).strip() and int(port) > 0]
        self.discovery_port = int(os.getenv("FEEDBACK_DISCOVERY_PORT", DEFAULT_DISCOVERY_PORT))
        self.discovery_interval = float(os.getenv("FEEDBACK_DISCOVERY_INTERVAL", DEFAULT_DISCOVERY_INTERVAL))
        self.stale_seconds = float(os.getenv("FEEDBACK_CONTROLLER_STALE_SECONDS", DEFAULT_CONTROLLER_STALE_SECONDS))
        self._controllers: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._started = False

    @classmethod
    def from_env(cls) -> "FeedbackUDPDispatcher":
        raw_targets = os.getenv("FEEDBACK_UDP_TARGETS", "").strip()
        targets: list[tuple[str, int]] = []
        if raw_targets:
            for item in raw_targets.split(","):
                chunk = item.strip()
                if not chunk:
                    continue
                if ":" in chunk:
                    host, port_text = chunk.rsplit(":", 1)
                    try:
                        port = int(port_text.strip())
                    except ValueError:
                        dbg(f"Invalid FEEDBACK_UDP_TARGETS entry ignored: {chunk!r}")
                        continue
                    host = host.strip()
                else:
                    host = chunk
                    port = int(os.getenv("FEEDBACK_UDP_PORT", DEFAULT_FEEDBACK_PORT))
                if host and port > 0:
                    targets.append((host, port))
        if not targets:
            host = os.getenv("FEEDBACK_UDP_HOST", DEFAULT_FEEDBACK_HOST).strip()
            port = int(os.getenv("FEEDBACK_UDP_PORT", DEFAULT_FEEDBACK_PORT))
            if host and port > 0:
                targets.append((host, port))
        return cls(targets)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._discovery_listener_loop, name="feedback-discovery-listener", daemon=True).start()
        threading.Thread(target=self._discovery_probe_loop, name="feedback-discovery-probe", daemon=True).start()

    def send(self, command: str, target_id: Optional[str] = None) -> None:
        command_text = str(command or "").strip()
        if not command_text:
            return
        payload = (command_text + "\n").encode("utf-8")
        if target_id and target_id not in {"all", "self"}:
            target = self._resolve_target(target_id)
            if not target:
                dbg(f"Feedback target {target_id!r} unavailable for command {command_text!r}")
                return
            self._send_udp(target[0], target[1], payload, command_text)
            return
        if target_id == "self":
            dbg(f"Feedback command reserved for self target: {command_text}")
            return
        for host, port in self.targets:
            self._send_udp(host, port, payload, command_text)

    def get_known_controllers(self) -> list[dict[str, Any]]:
        now = time.time()
        out: list[dict[str, Any]] = []
        with self._lock:
            stale_ids = [cid for cid, entry in self._controllers.items() if now - float(entry.get("last_seen", 0)) > self.stale_seconds]
            for cid in stale_ids:
                self._controllers.pop(cid, None)
            for cid, entry in sorted(self._controllers.items()):
                last_seen = float(entry.get("last_seen", 0))
                out.append({
                    "id": cid,
                    "name": entry.get("name") or cid,
                    "host": entry.get("host"),
                    "port": entry.get("port"),
                    "last_seen": last_seen,
                    "age_seconds": round(max(0.0, now - last_seen), 2),
                })
        return out

    def _resolve_target(self, target_id: str) -> Optional[tuple[str, int]]:
        with self._lock:
            entry = self._controllers.get(str(target_id).strip())
            if not entry:
                return None
            return str(entry.get("host")), int(entry.get("port", DEFAULT_FEEDBACK_PORT))

    def _send_udp(self, host: str, port: int, payload: bytes, command_text: str) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.sendto(payload, (host, port))
            dbg(f"Feedback UDP -> {host}:{port} :: {command_text}")
        except Exception as exc:
            dbg(f"Feedback UDP send failed to {host}:{port} for {command_text!r}: {exc}")

    def _discovery_probe_loop(self) -> None:
        while True:
            try:
                payload = b"DISCOVER_CONTROLLERS\n"
                for host, port in self.targets:
                    probe_host = host if host != DEFAULT_FEEDBACK_HOST else "255.255.255.255"
                    self._send_udp(probe_host, self.discovery_port, payload, "DISCOVER_CONTROLLERS")
            except Exception as exc:
                dbg(f"Discovery probe failed: {exc}")
            time.sleep(max(1.0, self.discovery_interval))

    def _discovery_listener_loop(self) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("0.0.0.0", self.discovery_port))
                while True:
                    data, addr = sock.recvfrom(2048)
                    self._handle_discovery_packet(data.decode("utf-8", errors="replace"), addr[0])
        except Exception as exc:
            dbg(f"Discovery listener failed: {exc}")

    def _handle_discovery_packet(self, text: str, source_ip: str) -> None:
        raw = (text or "").strip()
        if not raw:
            return
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if not lines:
            return
        head = lines[0].upper()
        if head not in {"HELLO", "HELLO_CONTROLLER", "CONTROLLER_HELLO", "DISCOVER_REPLY", "I_AM_CONTROLLER"}:
            return
        meta: dict[str, str] = {}
        for line in lines[1:]:
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            meta[k.strip().lower()] = v.strip()
        controller_id = meta.get("id") or meta.get("controller_id")
        if not controller_id:
            return
        entry = {
            "id": controller_id,
            "name": meta.get("name") or controller_id,
            "host": meta.get("host") or source_ip,
            "port": int(meta.get("port") or DEFAULT_FEEDBACK_PORT),
            "last_seen": time.time(),
        }
        with self._lock:
            self._controllers[controller_id] = entry
        dbg(f"Discovered controller: {entry}")


FEEDBACK_DISPATCHER = FeedbackUDPDispatcher.from_env()


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
  <div class=\"small\">Lifecycle + live profile editor</div>
</header>
<main>
  <section class=\"card\">
    <h2>Lifecycle</h2>
    <div id=\"notificationBox\" class=\"notification starting\"><div id=\"notificationTitle\" class=\"title\">Watcher Starting</div><div id=\"notificationDesc\" class=\"desc\">The watcher is booting and waiting to establish its first state.</div></div>
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
    <div style=\"margin-top:12px;\">
      <div class=\"label\">Controllers</div>
      <div id=\"controllerSummary\" class=\"value\" style=\"margin-bottom:8px;\">No controllers discovered.</div>
      <div class=\"value\"><pre id=\"controllersList\">[]</pre></div>
    </div>
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
  watcherStatus: document.getElementById("watcherStatus"), currentGame: document.getElementById("currentGame"), activeTitle: document.getElementById("activeTitle"), profilePath: document.getElementById("profilePath"), latestJson: document.getElementById("latestJson"), controllerSummary: document.getElementById("controllerSummary"), controllersList: document.getElementById("controllersList"), profileSelect: document.getElementById("profileSelect"), refreshProfiles: document.getElementById("refreshProfiles"), saveProfile: document.getElementById("saveProfile"), profileEditor: document.getElementById("profileEditor"), editorStatus: document.getElementById("editorStatus"), notificationBox: document.getElementById("notificationBox"), notificationTitle: document.getElementById("notificationTitle"), notificationDesc: document.getElementById("notificationDesc"), stateList: document.getElementById("stateList"),
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
  const controllers = debug.discovered_controllers || [];
  els.controllerSummary.textContent = controllers.length === 0 ? "No controllers discovered." : `${controllers.length} controller${controllers.length === 1 ? "" : "s"} discovered.`;
  els.controllersList.textContent = JSON.stringify(controllers, null, 2);
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
            self._send_json({"profile": name, "content": profile_path.read_text(encoding="utf-8")})
            return
        self._send_json({"error": "not_found"}, status=404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/api/profile/"):
            self._send_json({"error": "not_found"}, status=404)
            return
        name = unquote(path[len("/api/profile/"):]).strip()
        if not name.endswith(".yaml") or "/" in name or "\\" in name or name.startswith("."):
            self._send_json({"error": "invalid_profile_name"}, status=400)
            return
        profile_path = self._profiles_dir() / name
        if not profile_path.exists():
            self._send_json({"error": "profile_not_found", "profile": name}, status=404)
            return
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(content_length).decode("utf-8")
        try:
            parsed = yaml.safe_load(raw)
            normalize_profile(parsed)
        except yaml.YAMLError as exc:
            self._send_json({"error": f"invalid_yaml: {exc}"}, status=400)
            return
        except ProfileValidationError as exc:
            self._send_json({"error": f"invalid_profile: {exc}"}, status=400)
            return
        profile_path.write_text(raw, encoding="utf-8")
        _PROFILE_CACHE.pop(str(profile_path), None)
        self._send_json({"ok": True, "profile": name})

    def log_message(self, fmt: str, *args: Any) -> None:
        dbg(f"HTTP {self.address_string()} - {fmt % args}")


def start_http_server(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), WatcherHTTPHandler)
    thread = threading.Thread(target=server.serve_forever, name="watcher-http", daemon=True)
    thread.start()
    print(f"HTTP UI listening on http://{host}:{port}", file=sys.stderr, flush=True)
    return server


class RA:
    def __init__(self, host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def _cmd(self, cmd: str) -> str:
        payload = (cmd + "\n").encode("utf-8")
        dbg(f"RA UDP -> {self.host}:{self.port} :: {cmd}")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(self.timeout)
            sock.sendto(payload, (self.host, self.port))
            chunks: list[bytes] = []
            while True:
                try:
                    chunk, _addr = sock.recvfrom(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
        raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
        dbg(f"RA UDP <- {raw!r}")
        return raw

    def status(self) -> str:
        return self._cmd("GET_STATUS")

    def title(self) -> str:
        return self._cmd("GET_TITLE")

    def read_u8(self, addr: int) -> Optional[int]:
        try:
            raw = self._cmd(f"READ_CORE_MEMORY {addr} 1")
        except Exception:
            raise
        if not raw:
            return None
        match = re.search(r"([0-9A-Fa-f]{2})\b", raw)
        return int(match.group(1), 16) if match else None

    def read_u16(self, addr: int, endian: str = "little") -> Optional[int]:
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


def _ensure_mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProfileValidationError(f"{label} must be a mapping")
    return dict(value)


def _ensure_list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    raise ProfileValidationError(f"{label} must be a list")


def normalize_field_spec(field_name: str, spec: Any) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise ProfileValidationError(f"field '{field_name}' must be a mapping")
    if "addr" not in spec:
        raise ProfileValidationError(f"field '{field_name}' is missing addr")
    if "type" not in spec:
        raise ProfileValidationError(f"field '{field_name}' is missing type")
    field_type = str(spec["type"]).strip().lower()
    if field_type not in {"u8", "u16"}:
        raise ProfileValidationError(f"field '{field_name}' has unsupported type '{field_type}'")
    addr = spec["addr"]
    if isinstance(addr, str):
        try:
            int(addr, 0)
        except ValueError as exc:
            raise ProfileValidationError(f"field '{field_name}' has invalid addr '{addr}'") from exc
    elif not isinstance(addr, int):
        raise ProfileValidationError(f"field '{field_name}' addr must be int or string")
    normalized = {"addr": addr, "type": field_type}
    if field_type == "u16":
        endian = str(spec.get("endian", "little")).strip().lower()
        if endian not in {"little", "big"}:
            raise ProfileValidationError(f"field '{field_name}' has invalid endian '{endian}'")
        normalized["endian"] = endian
    return normalized


def _normalize_compare(value: Any) -> str:
    compare = str(value or "changed").strip().lower().replace(" ", "_")
    aliases = {
        "<": "decreased",
        ">": "increased",
        "=": "unchanged",
        "==": "equal",
        "!=": "not_equal",
        "less_than": "below",
        "greater_than": "above",
    }
    compare = aliases.get(compare, compare)
    supported = {
        "decreased",
        "increased",
        "changed",
        "unchanged",
        "equal",
        "not_equal",
        "above",
        "above_or_equal",
        "below",
        "below_or_equal",
        "crossed_above",
        "crossed_below",
        "delta_gt",
        "delta_gte",
        "delta_lt",
        "delta_lte",
        "became_zero",
        "became_nonzero",
    }
    if compare not in supported:
        raise ProfileValidationError(f"unsupported trigger compare '{compare}'")
    return compare


def normalize_condition(condition: Any, fields: dict[str, Any], *, label: str = "when") -> dict[str, Any]:
    if not isinstance(condition, dict):
        raise ProfileValidationError(f"{label} must be a mapping")
    if "all" in condition:
        clauses = _ensure_list(condition.get("all"), f"{label}.all")
        if not clauses:
            raise ProfileValidationError(f"{label}.all must not be empty")
        return {"all": [normalize_condition(item, fields, label=f"{label}.all") for item in clauses]}
    if "any" in condition:
        clauses = _ensure_list(condition.get("any"), f"{label}.any")
        if not clauses:
            raise ProfileValidationError(f"{label}.any must not be empty")
        return {"any": [normalize_condition(item, fields, label=f"{label}.any") for item in clauses]}

    field = str(condition.get("field") or "").strip()
    if not field:
        raise ProfileValidationError(f"{label} is missing field")
    if field not in fields:
        raise ProfileValidationError(f"{label} references unknown field '{field}'")

    compare = _normalize_compare(condition.get("compare") or condition.get("op") or "changed")
    normalized: dict[str, Any] = {"field": field, "compare": compare}

    if "value_field" in condition and "field_value" in condition:
        raise ProfileValidationError(f"{label} must not define both value_field and field_value")
    rhs_field = condition.get("value_field", condition.get("field_value"))
    if rhs_field is not None:
        rhs_field = str(rhs_field).strip()
        if rhs_field not in fields:
            raise ProfileValidationError(f"{label} references unknown value_field '{rhs_field}'")
        normalized["value_field"] = rhs_field
    elif "value" in condition:
        normalized["value"] = condition["value"]

    if "delta" in condition:
        normalized["delta"] = condition["delta"]
    return normalized


def normalize_action(action: Any, event_name: str) -> dict[str, Any]:
    if not isinstance(action, dict):
        raise ProfileValidationError(f"event '{event_name}' action must be a mapping")
    if "pin" not in action:
        raise ProfileValidationError(f"event '{event_name}' action is missing pin")

    pin = action["pin"]
    if not isinstance(pin, int):
        raise ProfileValidationError(f"event '{event_name}' action pin must be an integer")

    raw_behavior = str(action.get("behavior", action.get("mode", "set"))).strip().lower().replace(" ", "_")
    aliases = {
        "on": "set",
        "off": "set",
        "hold": "set",
        "vibration": "set",
        "pulse_vibration": "pulse",
    }
    behavior = aliases.get(raw_behavior, raw_behavior)
    if behavior not in {"set", "pulse"}:
        raise ProfileValidationError(f"event '{event_name}' has unsupported behavior '{raw_behavior}'")

    active = str(action.get("active", action.get("polarity", "high"))).strip().lower()
    if active not in {"high", "low"}:
        raise ProfileValidationError(f"event '{event_name}' action active must be 'high' or 'low'")

    normalized: dict[str, Any] = {
        "pin": pin,
        "behavior": behavior,
        "active": active,
    }

    if behavior == "set":
        value = action.get("value", action.get("level", action.get("state", None)))
        if value is None:
            if raw_behavior == "off":
                value = "off"
            else:
                value = "on"
        value_text = str(value).strip().lower()
        if value_text not in {"on", "off", "high", "low", "1", "0", "true", "false"}:
            raise ProfileValidationError(f"event '{event_name}' action value must be on/off")
        normalized["value"] = "on" if value_text in {"on", "high", "1", "true"} else "off"
        if "duration_ms" in action and action["duration_ms"] is not None:
            duration_ms = int(action["duration_ms"])
            if duration_ms < 0:
                raise ProfileValidationError(f"event '{event_name}' action duration_ms must be >= 0")
            normalized["duration_ms"] = duration_ms
    else:
        on_ms = int(action.get("on_ms", 150))
        off_ms = int(action.get("off_ms", 150))
        count = int(action.get("count", 1))
        if on_ms <= 0 or off_ms < 0 or count <= 0:
            raise ProfileValidationError(f"event '{event_name}' pulse action must use positive timings/count")
        normalized["on_ms"] = on_ms
        normalized["off_ms"] = off_ms
        normalized["count"] = count

    return normalized


def normalize_events(raw_events: Any) -> dict[str, dict[str, Any]]:
    events = _ensure_mapping(raw_events, "events")
    normalized: dict[str, dict[str, Any]] = {}
    for event_name, event_spec in events.items():
        name = str(event_name).strip()
        if not name:
            raise ProfileValidationError("event names must not be empty")
        if isinstance(event_spec, list):
            event_spec = {"actions": event_spec}
        elif not isinstance(event_spec, dict):
            raise ProfileValidationError(f"event '{name}' must be a mapping or action list")
        actions = event_spec.get("actions")
        if actions is None and "pin" in event_spec:
            actions = [event_spec]
        actions_list = _ensure_list(actions, f"event '{name}'.actions")
        if not actions_list:
            raise ProfileValidationError(f"event '{name}' must define at least one action")
        normalized[name] = {
            "name": name,
            "actions": [normalize_action(action, name) for action in actions_list],
        }
    return normalized


def normalize_triggers(raw_triggers: Any, fields: dict[str, Any], events: dict[str, Any]) -> list[dict[str, Any]]:
    items = _ensure_list(raw_triggers, "triggers")
    normalized: list[dict[str, Any]] = []
    for index, trigger in enumerate(items):
        label = f"triggers[{index}]"
        if not isinstance(trigger, dict):
            raise ProfileValidationError(f"{label} must be a mapping")

        when = trigger.get("when")
        if when is None and "field" in trigger:
            when = {
                "field": trigger.get("field"),
                "compare": trigger.get("compare", trigger.get("op", "changed")),
                **({"value": trigger["value"]} if "value" in trigger else {}),
                **({"value_field": trigger["value_field"]} if "value_field" in trigger else {}),
                **({"delta": trigger["delta"]} if "delta" in trigger else {}),
            }
        condition = normalize_condition(when, fields, label=f"{label}.when")

        event_names: list[str] = []
        if "event" in trigger:
            event_names = [str(trigger["event"]).strip()]
        elif "events" in trigger:
            event_names = [str(item).strip() for item in _ensure_list(trigger.get("events"), f"{label}.events")]
        elif "send" in trigger:
            event_names = [str(trigger["send"]).strip()]
        if not event_names:
            raise ProfileValidationError(f"{label} must define event/events/send")
        event_names = [name for name in event_names if name]
        if not event_names:
            raise ProfileValidationError(f"{label} must define at least one non-empty event name")

        normalized.append(
            {
                "name": str(trigger.get("name") or f"trigger_{index + 1}"),
                "when": condition,
                "events": event_names,
            }
        )
    return normalized


def normalize_profile(raw_profile: Any) -> dict[str, Any]:
    profile = _ensure_mapping(raw_profile, "profile")

    telemetry = _ensure_mapping(profile.get("telemetry"), "telemetry")
    raw_fields = telemetry.get("fields")
    if raw_fields is None:
        raw_fields = profile.get("fields")
    fields_map = _ensure_mapping(raw_fields, "telemetry.fields")
    normalized_fields = {name: normalize_field_spec(name, spec) for name, spec in fields_map.items()}

    raw_events = profile.get("events")
    normalized_events = normalize_events(raw_events)

    raw_triggers = profile.get("triggers")
    normalized_triggers = normalize_triggers(raw_triggers, normalized_fields, normalized_events)

    poll_seconds = float(profile.get("poll_seconds", DEFAULT_POLL))
    if poll_seconds <= 0:
        raise ProfileValidationError("poll_seconds must be > 0")

    normalized = {
        "game": str(profile.get("game") or "").strip() or None,
        "system": str(profile.get("system") or "").strip() or None,
        "poll_seconds": poll_seconds,
        "telemetry": {"fields": normalized_fields},
        "events": normalized_events,
        "triggers": normalized_triggers,
        "generated": bool(profile.get("generated", False)),
    }
    return normalized


def create_profile_scaffold(profile_path: Path, game_slug: str, system: Optional[str]) -> bool:
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "game": game_slug,
        "poll_seconds": DEFAULT_POLL,
        "telemetry": {"fields": {}},
        "events": {},
        "triggers": [],
    }
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


def load_profile(profile_path: Path) -> dict[str, Any]:
    profile_key = str(profile_path)
    cached = _PROFILE_CACHE.get(profile_key)
    if cached is not None:
        return cached
    parsed = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    normalized = normalize_profile(parsed)
    _PROFILE_CACHE[profile_key] = normalized
    return normalized


def profile_has_fields(profile: dict[str, Any]) -> bool:
    fields = profile.get("telemetry", {}).get("fields")
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
            try:
                profile = load_profile(profile_path)
            except (yaml.YAMLError, ProfileValidationError) as exc:
                set_http_state(active_title=title, profile_path=str(profile_path), watcher_status=f"Current Game: {title} (Invalid Profile: {exc})")
                time.sleep(retry_seconds)
                continue
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


def snapshot(client: RA, fields: dict[str, Any]) -> dict[str, Optional[int]]:
    out: dict[str, Optional[int]] = {}
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


def _current_clause_value(clause: dict[str, Any], current: dict[str, Optional[int]]) -> Any:
    if "value_field" in clause:
        return current.get(clause["value_field"])
    return clause.get("value")


def evaluate_condition(condition: dict[str, Any], current: dict[str, Optional[int]], previous: Optional[dict[str, Optional[int]]]) -> bool:
    if "all" in condition:
        return all(evaluate_condition(item, current, previous) for item in condition["all"])
    if "any" in condition:
        return any(evaluate_condition(item, current, previous) for item in condition["any"])

    field = condition["field"]
    compare = condition["compare"]
    cur = current.get(field)
    prev = None if previous is None else previous.get(field)
    rhs = _current_clause_value(condition, current)
    delta = None if prev is None or cur is None else cur - prev
    threshold = condition.get("delta")

    if compare in {"decreased", "increased", "changed", "unchanged", "crossed_above", "crossed_below", "became_zero", "became_nonzero"} and prev is None:
        return False
    if cur is None:
        return False

    if compare == "decreased":
        return prev is not None and cur < prev
    if compare == "increased":
        return prev is not None and cur > prev
    if compare == "changed":
        return prev is not None and cur != prev
    if compare == "unchanged":
        return prev is not None and cur == prev
    if compare == "equal":
        return rhs is not None and cur == rhs
    if compare == "not_equal":
        return rhs is not None and cur != rhs
    if compare == "above":
        return rhs is not None and cur > rhs
    if compare == "above_or_equal":
        return rhs is not None and cur >= rhs
    if compare == "below":
        return rhs is not None and cur < rhs
    if compare == "below_or_equal":
        return rhs is not None and cur <= rhs
    if compare == "crossed_above":
        return prev is not None and rhs is not None and prev <= rhs < cur
    if compare == "crossed_below":
        return prev is not None and rhs is not None and prev >= rhs > cur
    if compare == "delta_gt":
        return delta is not None and threshold is not None and delta > threshold
    if compare == "delta_gte":
        return delta is not None and threshold is not None and delta >= threshold
    if compare == "delta_lt":
        return delta is not None and threshold is not None and delta < threshold
    if compare == "delta_lte":
        return delta is not None and threshold is not None and delta <= threshold
    if compare == "became_zero":
        return prev is not None and prev != 0 and cur == 0
    if compare == "became_nonzero":
        return prev is not None and prev == 0 and cur != 0
    return False


class EventDispatcher:
    def __init__(self, feedback_dispatcher: FeedbackUDPDispatcher) -> None:
        self.feedback_dispatcher = feedback_dispatcher

    def dispatch(self, event_name: str, event_def: Optional[dict[str, Any]] = None) -> None:
        event_def = event_def or {"name": event_name, "commands": [{"command": event_name}], "actions": []}
        commands = event_def.get("commands", [])
        actions = event_def.get("actions", [])

        if not commands and not actions:
            commands = [{"command": event_name}]

        for command in commands:
            if not isinstance(command, dict):
                continue
            target_id = None
            raw_targets = command.get("targets")
            if isinstance(raw_targets, list) and len(raw_targets) == 1:
                target_id = str(raw_targets[0]).strip() or None
            self.feedback_dispatcher.send(str(command.get("command", "")), target_id=target_id)

        for action in actions:
            payload = self._serialize_action(event_name, action)
            try:
                Bridge.call("trigger_event", payload)
            except Exception as exc:
                dbg(f"Bridge trigger_event failed for {event_name}: {exc}")

    @staticmethod
    def _serialize_action(event_name: str, action: dict[str, Any]) -> str:
        parts = [
            f"event={event_name}",
            f"pin={action['pin']}",
            f"behavior={action['behavior']}",
            f"active={action.get('active', 'high')}",
        ]
        if action["behavior"] == "set":
            parts.append(f"value={action.get('value', 'on')}")
            if action.get("duration_ms") is not None:
                parts.append(f"duration_ms={action['duration_ms']}")
        elif action["behavior"] == "pulse":
            parts.append(f"on_ms={action.get('on_ms', 150)}")
            parts.append(f"off_ms={action.get('off_ms', 150)}")
            parts.append(f"count={action.get('count', 1)}")
        return ";".join(parts)


class TriggerEngine:
    def __init__(self) -> None:
        self.previous_snapshot: Optional[dict[str, Optional[int]]] = None
        self.dispatcher = EventDispatcher(FEEDBACK_DISPATCHER)

    def reset(self) -> None:
        self.previous_snapshot = None

    def process(self, profile: dict[str, Any], current: dict[str, Optional[int]]) -> None:
        previous = self.previous_snapshot
        for trigger in profile.get("triggers", []):
            if evaluate_condition(trigger["when"], current, previous):
                for event_name in trigger.get("events", []):
                    event_def = profile.get("events", {}).get(event_name)
                    self.dispatcher.dispatch(event_name, event_def)
        self.previous_snapshot = dict(current)


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
        self.profile: Optional[dict[str, Any]] = None
        self.active_title: Optional[str] = None
        self.active_profile_path: Optional[Path] = None
        self.poll = DEFAULT_POLL
        self.fields: dict[str, Any] = {}
        self.last_data: Optional[dict[str, Optional[int]]] = None
        self.last_state: Optional[str] = None
        self.next_due = 0.0
        self.trigger_engine = TriggerEngine()

    def start(self) -> None:
        set_http_state(payload=make_payload(None, "starting", None), profiles_dir=str(self.profiles_dir), profile_path=None, active_title=None, watcher_status="Starting")
        publish_bridge_payload(get_http_state()["payload"])
        FEEDBACK_DISPATCHER.start()
        start_http_server(self.http_host, self.http_port)
        self.profile, self.active_title = reconnect_and_reload_profile(self.client, self.profiles_dir, self.connect_retry, self.title_retry, self.scaffolded_once)
        self.active_profile_path = self.profiles_dir / f"{slug(self.active_title or '')}.yaml"
        self._reload_profile_state()
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
                self.active_profile_path = self.profiles_dir / f"{slug(self.active_title or '')}.yaml"
                self._reload_profile_state()
                return
            if state == "no_content":
                if self.last_state != "waiting_content":
                    emit_payload(None, "waiting_content", None)
                    self.last_state = "waiting_content"
                set_http_state(active_title=None, profile_path=None, watcher_status="Connected Waiting for Content")
                self.profile, self.active_title = reconnect_and_reload_profile(self.client, self.profiles_dir, self.connect_retry, self.title_retry, self.scaffolded_once)
                self.active_profile_path = self.profiles_dir / f"{slug(self.active_title or '')}.yaml"
                self._reload_profile_state()
                return
            assert current_title is not None
            if slug(current_title) != slug(self.active_title or ""):
                emit_payload(self.active_title, "switching_game", None)
                set_http_state(watcher_status=f"Current Game: {current_title}")
                self.profile, self.active_title = wait_for_profile(self.client, self.profiles_dir, retry_seconds=self.title_retry, scaffolded_once=self.scaffolded_once)
                self.active_profile_path = self.profiles_dir / f"{slug(self.active_title or '')}.yaml"
                self._reload_profile_state()
                return
            current = snapshot(self.client, self.fields)
            assert self.profile is not None
            self.trigger_engine.process(self.profile, current)
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
            self.active_profile_path = self.profiles_dir / f"{slug(self.active_title or '')}.yaml"
            self._reload_profile_state()

    def _reload_profile_state(self) -> None:
        assert self.profile is not None
        self.poll = float(self.profile.get("poll_seconds", DEFAULT_POLL))
        self.fields = dict(self.profile.get("telemetry", {}).get("fields", {}))
        self.last_data = None
        self.last_state = None
        self.trigger_engine.reset()
        self.next_due = time.monotonic() + min(self.poll, 0.10)


RUNTIME: Optional[Runtime] = None


def main() -> None:
    global RUNTIME
    RUNTIME = Runtime()
    RUNTIME.start()
    App.run(user_loop=user_loop)


if __name__ == "__main__":
    main()
