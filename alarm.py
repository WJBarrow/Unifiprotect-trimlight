#!/usr/bin/env python3
"""
UniFi Protect → Trimlight Alarm Service

Receives webhook POSTs from UniFi Protect's Alarm Manager and applies a
named light effect via the Trimlight Edge cloud API.  After a configurable
timeout the lights auto-restore to their previous state.

The effect to apply is specified in the webhook URL:
    POST /webhook?effect=white       → solid white  (e.g. animal / person)
    POST /webhook?effect=red-strobe  → red strobe   (e.g. vehicle)
    POST /webhook                    → uses default effect (white)

Requires only Python stdlib — no pip dependencies.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

log = logging.getLogger("alarm")

# ---------------------------------------------------------------------------
# Named light effects
# Each entry: label, pixels (list of Trimlight pixel segments), mode, speed, brightness
# Custom effect modes: 0=Static 1=Chase Fwd 2=Chase Bwd 5=Stars 6=Breath 15=Strobe 16=Fade
# pixels: [{"index": N, "count": N, "color": 0xRRGGBB, "disable": bool}, ...]
#   index = segment slot (0–29), count = consecutive LEDs in that segment,
#   the pattern tiles repeatedly across all LEDs on the controller.
# ---------------------------------------------------------------------------
def _px(*rgb_pairs):
    """Build a pixel list from (color, count) pairs."""
    return [{"index": i, "count": c, "color": col, "disable": False}
            for i, (col, c) in enumerate(rgb_pairs)]

EFFECTS = {
    "white":          {"label": "Solid White",     "mode": 0,  "speed": 100, "brightness": 255,
                       "pixels": _px((0xFFFFFF, 1))},
    "red":            {"label": "Solid Red",       "mode": 0,  "speed": 100, "brightness": 255,
                       "pixels": _px((0xFF0000, 1))},
    "red-strobe":     {"label": "Red Strobe",      "mode": 15, "speed": 150, "brightness": 255,
                       "pixels": _px((0xFF0000, 1))},
    "blue":           {"label": "Solid Blue",      "mode": 0,  "speed": 100, "brightness": 255,
                       "pixels": _px((0x0000FF, 1))},
    "amber":          {"label": "Solid Amber",     "mode": 0,  "speed": 100, "brightness": 255,
                       "pixels": _px((0xFF8C00, 1))},
    # Alternating red/blue LED pattern strobes on/off together
    "red-blue-strobe": {"label": "Red Blue Strobe", "mode": 15, "speed": 200, "brightness": 255,
                        "pixels": _px((0xFF0000, 1), (0x0000FF, 1))},
    # Alternating red/blue LEDs chase forward
    "red-blue-chase":  {"label": "Red Blue Chase",  "mode": 1,  "speed": 150, "brightness": 255,
                        "pixels": _px((0xFF0000, 1), (0x0000FF, 1))},
    # Invoke a pattern saved on the Trimlight device by name
    "intruder": {
        "label":      "Intruder Red Blue Strobe",
        "saved_name": "IntruderRedBlueStrobe",   # exact name as saved on the device
        # UI display only — actual appearance is defined on the device
        "mode": 15, "speed": 200, "brightness": 255,
        "pixels": _px((0xFF0000, 1), (0x0000FF, 1)),
    },
    # All LEDs switch from solid red → solid blue → solid red … (cycle effect)
    "police": {
        "label":    "Police Flash",
        "frames": [
            {"label": "Police Flash – red",  "mode": 0, "speed": 100, "brightness": 255, "pixels": _px((0xFF0000, 1))},
            {"label": "Police Flash – blue", "mode": 0, "speed": 100, "brightness": 255, "pixels": _px((0x0000FF, 1))},
        ],
        "interval": 0.1,    # seconds each frame is shown before switching (API latency ~1s dominates)
        # UI display fields (first-frame colour shown as primary swatch)
        "mode": 0, "speed": 100, "brightness": 255,
        "pixels": _px((0xFF0000, 1), (0x0000FF, 1)),
    },
}
DEFAULT_EFFECT = "white"

# Switch states
SWITCH_OFF    = 0
SWITCH_MANUAL = 1
SWITCH_TIMER  = 2


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class Config:
    def __init__(self):
        self.client_id     = os.environ.get("TRIMLIGHT_CLIENT_ID", "")
        self.client_secret = os.environ.get("TRIMLIGHT_CLIENT_SECRET", "")
        self.device_id     = os.environ.get("TRIMLIGHT_DEVICE_ID", "")
        self.api_url       = os.environ.get(
            "TRIMLIGHT_API_URL", "https://trimlight.ledhue.com/trimlight"
        )
        self.webhook_port  = int(os.environ.get("WEBHOOK_PORT", "8484"))
        self.alarm_timeout = int(os.environ.get("ALARM_TIMEOUT", "30"))
        self.log_level     = os.environ.get("LOG_LEVEL", "INFO").upper()

    def validate(self):
        missing = [k for k, v in [
            ("TRIMLIGHT_CLIENT_ID",     self.client_id),
            ("TRIMLIGHT_CLIENT_SECRET", self.client_secret),
            ("TRIMLIGHT_DEVICE_ID",     self.device_id),
        ] if not v]
        if missing:
            sys.exit("ERROR: Required environment variables not set: " + ", ".join(missing))
        if self.alarm_timeout < 1:
            sys.exit("ERROR: ALARM_TIMEOUT must be >= 1")


# ---------------------------------------------------------------------------
# Trimlight Cloud API client
# ---------------------------------------------------------------------------
class APIError(Exception):
    """Error communicating with the Trimlight cloud API."""


class TrimlightClient:
    """
    Trimlight Edge cloud REST API.

    Docs:  https://trimlight.com/hubfs/Manuals/Trimlight_Edge_API_Documentation%208192022.pdf
    Base:  POST https://trimlight.ledhue.com/trimlight/<path>
    Auth:  HMAC-SHA256("Trimlight|<clientId>|<timestamp>", clientSecret) → base64
    """

    REQUEST_TIMEOUT = 15

    def __init__(self, config: Config):
        self.config = config

    def _auth_headers(self) -> dict:
        timestamp = str(int(time.time() * 1000))
        message   = f"Trimlight|{self.config.client_id}|{timestamp}"
        mac = hmac.new(
            self.config.client_secret.encode(),
            message.encode(),
            hashlib.sha256,
        ).digest()
        return {
            "authorization": base64.b64encode(mac).decode(),
            "S-ClientId":    self.config.client_id,
            "S-Timestamp":   timestamp,
            "Content-Type":  "application/json",
        }

    def _post(self, path: str, body: dict) -> dict:
        url  = self.config.api_url + path
        data = json.dumps(body).encode()
        log.debug("API POST %s  body=%s", path, json.dumps(body))
        req = Request(url, data=data, headers=self._auth_headers(), method="POST")
        try:
            with urlopen(req, timeout=self.REQUEST_TIMEOUT) as resp:
                result = json.loads(resp.read())
                log.debug("API response: %s", json.dumps(result))
                if result.get("code") != 0:
                    raise APIError(
                        f"API error code={result.get('code')} desc={result.get('desc')}"
                    )
                return result
        except HTTPError as exc:
            raise APIError(f"HTTP {exc.code}: {exc.read().decode(errors='replace')}") from exc
        except URLError as exc:
            raise APIError(f"Request failed: {exc.reason}") from exc

    def _now_date(self) -> dict:
        now     = datetime.now()
        weekday = now.isoweekday()   # Mon=1 … Sun=7
        return {
            "year":    now.year - 2000,
            "month":   now.month,
            "day":     now.day,
            "weekday": 1 if weekday == 7 else weekday + 1,  # Sun=1 … Sat=7
            "hours":   now.hour,
            "minutes": now.minute,
            "seconds": now.second,
        }

    def notify_update_shadow(self):
        """Ask the device to push its latest state to the cloud (API #25)."""
        self._post("/v1/oauth/resources/device/notify-update-shadow", {
            "deviceId":    self.config.device_id,
            "currentDate": self._now_date(),
        })

    def get_device_detail(self) -> dict:
        """Fetch current mode, connectivity, and running effect (API #4)."""
        result = self._post("/v1/oauth/resources/device/get", {
            "deviceId":    self.config.device_id,
            "currentDate": self._now_date(),
        })
        payload = result.get("payload", {})
        log.info("Device detail: switchState=%s connectivity=%s",
                 payload.get("switchState"), payload.get("connectivity"))
        return payload

    def set_switch_state(self, state: int):
        """0=Off  1=Manual  2=Timer (API #5)."""
        labels = {0: "Off", 1: "Manual", 2: "Timer"}
        log.info("set_switch_state → %s (%d)", labels.get(state, "?"), state)
        self._post("/v1/oauth/resources/device/update", {
            "deviceId": self.config.device_id,
            "payload":  {"switchState": state},
        })

    def preview_effect(self, effect: dict):
        """Preview a named effect on the device (API #11).

        Note: Edge firmware uses category=2 for custom effects (docs say 1).
        """
        log.info("preview_effect: %s", effect.get("label", effect))
        self._post("/v1/oauth/resources/device/effect/preview", {
            "deviceId": self.config.device_id,
            "payload": {
                "category":   2,
                "mode":       effect["mode"],
                "speed":      effect["speed"],
                "brightness": effect["brightness"],
                "pixels": effect["pixels"],
            },
        })

    def view_effect(self, effect_id: int):
        """Activate a saved effect by ID (API #13)."""
        log.info("view_effect id=%d", effect_id)
        self._post("/v1/oauth/resources/device/effect/view", {
            "deviceId": self.config.device_id,
            "payload":  {"id": effect_id},
        })

    def find_saved_effect_id(self, name: str, effects: list) -> int:
        """Return the ID of a saved effect matched by name (case-insensitive).
        Raises APIError if not found."""
        name_lower = name.lower()
        for eff in effects:
            if isinstance(eff, dict) and eff.get("name", "").lower() == name_lower:
                return eff["id"]
        raise APIError(
            f"Saved effect '{name}' not found on device. "
            f"Available: {[e.get('name') for e in effects if isinstance(e, dict)]}"
        )


# ---------------------------------------------------------------------------
# Alarm state machine
# ---------------------------------------------------------------------------
class AlarmStateMachine:
    """
    IDLE ──trigger(effect)──▸ ALARMED ──timeout──▸ RESTORING ──done──▸ IDLE
                                ▲                                        │
                          trigger (same effect): reset timer             │
                          trigger (new effect):  apply new + reset       │
                          ◀──────────────────────────────────────────────┘
    """

    IDLE      = "idle"
    ALARMED   = "alarmed"
    RESTORING = "restoring"
    MAX_LOG   = 30

    def __init__(self, config: Config):
        self.config              = config
        self._state              = self.IDLE
        self._lock               = threading.RLock()
        self._timer              = None
        self._current_effect     = None   # effect name active during alarm
        self._saved_switch_state = None
        self._saved_effect_id    = None
        self.last_triggered      = None
        self.last_effect         = None
        self.last_restored       = None
        self.activity_log        = []

    @property
    def state(self) -> str:
        return self._state

    def _log(self, message: str, level: str = "info"):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            self.activity_log.insert(0, (ts, level, message))
            if len(self.activity_log) > self.MAX_LOG:
                self.activity_log = self.activity_log[:self.MAX_LOG]
        getattr(log, level, log.info)(message)

    # -- public interface ---------------------------------------------------

    def trigger(self, effect_name: str = DEFAULT_EFFECT):
        """Fire the alarm with the given named effect. Thread-safe."""
        effect_name = effect_name if effect_name in EFFECTS else DEFAULT_EFFECT
        effect      = EFFECTS[effect_name]

        with self._lock:
            if self._state == self.ALARMED:
                if effect_name == self._current_effect:
                    self._log(f"Already alarmed ({effect['label']}) — resetting timer")
                    self._reset_timer()
                    return
                # Different effect — override immediately
                self._log(
                    f"Override: {EFFECTS[self._current_effect]['label']} "
                    f"→ {effect['label']}"
                )
                self._current_effect = effect_name
                self.last_effect     = effect_name
                if self._timer:
                    self._timer.cancel()
                    self._timer = None
                threading.Thread(
                    target=self._apply_effect_and_reset_timer,
                    args=(effect_name,), daemon=True,
                ).start()
                return

            if self._state == self.RESTORING:
                self._log(f"Restore in progress — queuing re-alarm ({effect['label']})")
                threading.Thread(
                    target=self._wait_and_retrigger,
                    args=(effect_name,), daemon=True,
                ).start()
                return

            # IDLE → ALARMED
            self._state          = self.ALARMED
            self._current_effect = effect_name
            self.last_triggered  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.last_effect     = effect_name

        self._log(f"Alarm triggered — {effect['label']}")
        try:
            self._activate_alarm(effect_name)
        except Exception as exc:
            self._log(f"Failed to activate alarm: {exc}", level="error")
            log.exception("Alarm activation error")
            with self._lock:
                self._state = self.IDLE
            return

        self._log(f"{effect['label']} active — restoring in {self.config.alarm_timeout}s")
        with self._lock:
            self._reset_timer()

    # -- internals ----------------------------------------------------------

    def _activate_alarm(self, effect_name: str):
        """Save device state, switch to Manual, apply effect."""
        client = TrimlightClient(self.config)
        try:
            client.notify_update_shadow()
            time.sleep(1)
        except Exception:
            log.debug("notify_update_shadow failed (non-fatal)")

        detail = client.get_device_detail()
        self._saved_switch_state = detail.get("switchState")
        current = detail.get("currentEffect") or {}
        eid = current.get("id") if isinstance(current, dict) else None
        self._saved_effect_id = eid if eid is not None and eid >= 0 else None
        log.info("Saved state: switchState=%s effectId=%s",
                 self._saved_switch_state, self._saved_effect_id)

        effect = EFFECTS[effect_name]
        if "saved_name" in effect:
            saved_id = client.find_saved_effect_id(effect["saved_name"],
                                                   detail.get("effects", []))
            # view_effect only activates reliably when the device is in Timer mode.
            # Ensure Timer mode first, then activate the saved effect, then lock
            # in Manual so the schedule doesn't advance past it.
            if self._saved_switch_state != SWITCH_TIMER:
                client.set_switch_state(SWITCH_TIMER)
                time.sleep(1.0)
            client.view_effect(saved_id)
            client.set_switch_state(SWITCH_MANUAL)
        elif "frames" in effect:
            client.set_switch_state(SWITCH_MANUAL)
            threading.Thread(
                target=self._run_cycle_effect,
                args=(effect_name,), daemon=True,
            ).start()
        else:
            client.set_switch_state(SWITCH_MANUAL)
            client.preview_effect(effect)

    def _run_cycle_effect(self, effect_name: str):
        """Loop through cycle-effect frames while this effect is active."""
        effect   = EFFECTS[effect_name]
        frames   = effect["frames"]
        interval = effect.get("interval", 1.0)
        client   = TrimlightClient(self.config)
        i        = 0
        while True:
            with self._lock:
                if self._state != self.ALARMED or self._current_effect != effect_name:
                    break
            try:
                client.preview_effect(frames[i % len(frames)])
            except APIError as exc:
                log.warning("Cycle frame preview failed: %s", exc)
            i += 1
            time.sleep(interval)

    def _apply_effect_and_reset_timer(self, effect_name: str):
        """Apply a new effect while already in Manual mode, then reset timer."""
        try:
            effect = EFFECTS[effect_name]
            client = TrimlightClient(self.config)
            if "saved_name" in effect:
                detail = client.get_device_detail()
                saved_id = client.find_saved_effect_id(effect["saved_name"],
                                                       detail.get("effects", []))
                current_state = detail.get("switchState")
                if current_state != SWITCH_TIMER:
                    client.set_switch_state(SWITCH_TIMER)
                    time.sleep(1.0)
                client.view_effect(saved_id)
                client.set_switch_state(SWITCH_MANUAL)
            elif "frames" in effect:
                threading.Thread(
                    target=self._run_cycle_effect,
                    args=(effect_name,), daemon=True,
                ).start()
            else:
                client.preview_effect(effect)
        except Exception as exc:
            self._log(f"Override apply failed: {exc}", level="error")
        with self._lock:
            if self._state == self.ALARMED:
                self._reset_timer()

    def _restore(self):
        """Restore the pre-alarm device state."""
        with self._lock:
            if self._state != self.ALARMED:
                return
            self._state = self.RESTORING

        # Wait for any in-flight cycle-effect API call to complete before
        # sending restore commands — prevents a race where the last
        # preview_effect arrives after SWITCH_TIMER and reverts the device
        # back to Manual mode.
        time.sleep(1.5)

        self._log("Restoring previous state…")
        try:
            client = TrimlightClient(self.config)
            saved  = self._saved_switch_state

            if saved == SWITCH_MANUAL and self._saved_effect_id is not None:
                self._log(f"Restoring Manual effect ID {self._saved_effect_id}")
                client.view_effect(self._saved_effect_id)

            else:
                # Restore to Timer for both SWITCH_TIMER and SWITCH_OFF.
                # When saved==SWITCH_OFF the device was likely off because no
                # schedule was active at that moment — not because the user
                # explicitly turned it off.  Always returning to Timer lets the
                # schedule resume naturally and prevents the cascade where every
                # successive alarm finds switchState=0 and permanently locks the
                # lights off.
                if saved == SWITCH_OFF:
                    self._log("Saved state was Off — restoring to Timer so schedule can resume",
                              level="warning")
                else:
                    self._log("Restoring Timer mode (schedule will resume)")
                client.set_switch_state(SWITCH_OFF)
                time.sleep(0.5)
                client.set_switch_state(SWITCH_TIMER)

        except Exception as exc:
            self._log(f"Restore failed: {exc} — lights may remain on", level="error")
            log.exception("Restore error")

        with self._lock:
            self._state        = self.IDLE
            self._current_effect = None
            self.last_restored = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._log("Lights restored — system idle")

    def _reset_timer(self):
        """(Re)start the restore countdown. Call under self._lock."""
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self.config.alarm_timeout, self._restore)
        self._timer.daemon = True
        self._timer.start()

    def _wait_and_retrigger(self, effect_name: str):
        for _ in range(50):
            time.sleep(0.1)
            if self._state == self.IDLE:
                self.trigger(effect_name)
                return
        log.warning("Timed out waiting for restore — dropping re-trigger")

    def get_state_dict(self) -> dict:
        with self._lock:
            return {
                "alarm_state":    self._state,
                "current_effect": self._current_effect,
                "last_triggered": self.last_triggered,
                "last_effect":    self.last_effect,
                "last_restored":  self.last_restored,
            }


# ---------------------------------------------------------------------------
# Web UI HTML template
# ---------------------------------------------------------------------------
_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Trimlight Alarm</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: #0f172a; color: #e2e8f0; min-height: 100vh; padding: 2rem;
    }}
    h1 {{ font-size: 1.75rem; color: #f97316; margin-bottom: 0.2rem; }}
    .subtitle {{ color: #64748b; font-size: 0.875rem; margin-bottom: 2rem; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 1.25rem; margin-bottom: 1.25rem;
    }}
    .card {{
      background: #1e293b; border: 1px solid #334155;
      border-radius: 12px; padding: 1.5rem;
    }}
    .card h2 {{
      font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.08em;
      color: #64748b; margin-bottom: 1rem;
    }}
    .stat-row {{
      display: flex; justify-content: space-between; align-items: center;
      padding: 0.45rem 0; border-bottom: 1px solid #263347; font-size: 0.875rem;
    }}
    .stat-row:last-child {{ border-bottom: none; }}
    .stat-label {{ color: #94a3b8; }}
    .stat-value {{ color: #f1f5f9; font-weight: 500; font-family: monospace; font-size: 0.82rem; }}
    .badge {{
      display: inline-block; padding: 0.25rem 0.75rem;
      border-radius: 9999px; font-size: 0.8rem; font-weight: 600;
    }}
    .badge-idle      {{ background: #1e3a5f; color: #38bdf8; }}
    .badge-alarmed   {{ background: #450a0a; color: #f87171; }}
    .badge-restoring {{ background: #431407; color: #fb923c; }}
    .field label {{
      display: block; font-size: 0.8rem; color: #94a3b8; margin-bottom: 0.4rem;
    }}
    select {{
      width: 100%; padding: 0.6rem 0.75rem;
      background: #0f172a; border: 1px solid #334155;
      border-radius: 8px; color: #f1f5f9; font-size: 0.9rem; margin-bottom: 0.75rem;
    }}
    select:focus {{ outline: none; border-color: #f97316; }}
    button {{
      width: 100%; padding: 0.7rem 1rem;
      background: #ea580c; color: #fff;
      border: none; border-radius: 8px; font-size: 0.95rem;
      font-weight: 600; cursor: pointer; transition: background 0.15s;
    }}
    button:hover {{ background: #c2410c; }}
    button:disabled {{ background: #334155; color: #64748b; cursor: not-allowed; }}
    #result {{
      margin-top: 0.75rem; padding: 0.65rem 0.9rem;
      border-radius: 8px; font-size: 0.875rem; display: none;
    }}
    #result.ok  {{ background: #052e16; color: #4ade80; display: block; }}
    #result.err {{ background: #450a0a; color: #f87171; display: block; }}
    .url-table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
    .url-table td {{ padding: 0.4rem 0.5rem; border-bottom: 1px solid #263347; vertical-align: top; }}
    .url-table tr:last-child td {{ border-bottom: none; }}
    .url-table td:first-child {{ color: #94a3b8; white-space: nowrap; padding-right: 1rem; }}
    .url-code {{
      font-family: monospace; color: #fb923c;
      background: #0f172a; padding: 0.2rem 0.5rem; border-radius: 4px;
      word-break: break-all;
    }}
    .fx-table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
    .fx-table th {{
      text-align: left; padding: 0.35rem 0.5rem;
      color: #64748b; font-weight: 500; border-bottom: 1px solid #334155;
    }}
    .fx-table td {{ padding: 0.4rem 0.5rem; border-bottom: 1px solid #263347; }}
    .fx-table tr:last-child td {{ border-bottom: none; }}
    .swatch {{
      display: inline-block; width: 14px; height: 14px;
      border-radius: 50%; vertical-align: middle; margin-right: 6px;
      border: 1px solid rgba(255,255,255,0.15);
    }}
    .fx-name {{ font-family: monospace; color: #fb923c; }}
    .log-list {{ list-style: none; }}
    .log-list li {{
      display: flex; gap: 1rem; padding: 0.45rem 0;
      border-bottom: 1px solid #263347; font-size: 0.82rem;
    }}
    .log-list li:last-child {{ border-bottom: none; }}
    time.log-ts {{ color: #475569; white-space: nowrap; flex-shrink: 0; }}
    .log-msg     {{ color: #cbd5e1; }}
    .log-error   {{ color: #f87171; }}
    .log-warning {{ color: #fb923c; }}
    .empty       {{ color: #475569; font-size: 0.85rem; font-style: italic; }}
  </style>
</head>
<body>
  <h1>&#128308; Trimlight Alarm</h1>
  <p class="subtitle">UniFi Protect &rarr; Trimlight Edge &mdash; port {port}</p>

  <div class="grid">
    <!-- Status card -->
    <div class="card">
      <h2>System Status</h2>
      <div class="stat-row">
        <span class="stat-label">Alarm State</span>
        <span class="badge badge-{state_class}">{state_upper}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Active Effect</span>
        <span class="stat-value">{current_effect_disp}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Device ID</span>
        <span class="stat-value">{device_id_short}&hellip;</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Alarm Timeout</span>
        <span class="stat-value">{alarm_timeout}s</span>
      </div>
    </div>

    <!-- Last alarm card -->
    <div class="card">
      <h2>Last Alarm</h2>
      <div class="stat-row">
        <span class="stat-label">Triggered</span>
        <span class="stat-value"><time data-utc="{last_triggered}">{last_triggered_disp}</time></span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Effect Used</span>
        <span class="stat-value">{last_effect_disp}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Restored</span>
        <span class="stat-value"><time data-utc="{last_restored}">{last_restored_disp}</time></span>
      </div>
    </div>

    <!-- Test card -->
    <div class="card">
      <h2>Test &mdash; Simulate Detection</h2>
      <div class="field">
        <label for="effectSel">Effect</label>
        <select id="effectSel">
          {effect_options}
        </select>
      </div>
      <button id="triggerBtn" onclick="triggerAlarm()">&#9654; Trigger Now</button>
      <div id="result"></div>
    </div>
  </div>

  <!-- Webhook URLs -->
  <div class="card" style="margin-bottom:1.25rem">
    <h2>UniFi Protect Webhook URLs</h2>
    <p style="font-size:0.82rem;color:#94a3b8;margin-bottom:0.75rem">
      In Protect &rarr; Alarm Manager, create one alarm per detection type and set
      the webhook URL to the corresponding URL below (POST method).
    </p>
    <table class="url-table">
      {webhook_rows}
    </table>
  </div>

  <!-- Effects reference -->
  <div class="card" style="margin-bottom:1.25rem">
    <h2>Available Effects</h2>
    <table class="fx-table">
      <thead>
        <tr>
          <th>Name</th><th>Label</th><th>Mode</th><th>Speed</th><th>Brightness</th>
        </tr>
      </thead>
      <tbody>
        {effects_rows}
      </tbody>
    </table>
  </div>

  <!-- Activity log -->
  <div class="card">
    <h2>Activity Log</h2>
    <ul class="log-list">
      {activity_items}
    </ul>
  </div>

  <script>
    // Localise UTC timestamps
    document.querySelectorAll('time[data-utc]').forEach(el => {{
      const v = el.dataset.utc;
      if (v && v !== '\u2014') {{
        try {{ el.textContent = new Date(v).toLocaleString(); }} catch(e) {{}}
      }}
    }});

    // Replace <host> placeholder in webhook URLs with actual hostname
    document.querySelectorAll('.url-code[data-url]').forEach(el => {{
      el.textContent = el.dataset.url.replace('<host>', location.hostname);
    }});

    async function triggerAlarm() {{
      const btn    = document.getElementById('triggerBtn');
      const result = document.getElementById('result');
      const effect = document.getElementById('effectSel').value;
      btn.disabled = true;
      btn.textContent = 'Triggering\u2026';
      result.className = '';
      result.style.display = 'none';

      try {{
        const resp = await fetch('/test', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ effect }})
        }});
        const data = await resp.json();
        if (resp.ok && data.triggered) {{
          result.className = 'ok';
          result.textContent = '\u2713 Triggered: ' + (data.effect_label || effect);
          setTimeout(() => location.reload(), 3000);
        }} else {{
          result.className = 'err';
          result.textContent = '\u2717 ' + (data.error || 'Failed');
        }}
      }} catch (err) {{
        result.className = 'err';
        result.textContent = '\u2717 Request error: ' + err.message;
      }} finally {{
        btn.disabled = false;
        btn.textContent = '\u25b6 Trigger Now';
      }}
    }}

    const state = '{state_class}';
    if (state !== 'idle') setTimeout(() => location.reload(), 5000);
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------
class WebhookHandler(BaseHTTPRequestHandler):
    alarm_sm: AlarmStateMachine = None
    config:   Config            = None

    def log_message(self, fmt, *args):
        log.debug("HTTP %s — " + fmt, self.address_string(), *args)

    def _json(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, html: str):
        data = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def _effect_from_query(self) -> str:
        """Extract ?effect=<name> from the request path."""
        params = parse_qs(urlparse(self.path).query)
        name   = params.get("effect", [DEFAULT_EFFECT])[0]
        return name if name in EFFECTS else DEFAULT_EFFECT

    # -- routing ------------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/status"):
            self._serve_ui()
        elif path in ("/health", "/webhook"):
            self._json(200, {"status": "ok", **self.alarm_sm.get_state_dict()})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/webhook":
            self._handle_webhook()
        elif path == "/test":
            self._handle_test()
        else:
            self._json(404, {"error": "not found"})

    # -- status page --------------------------------------------------------

    def _serve_ui(self):
        sd    = self.alarm_sm.get_state_dict()
        state = sd["alarm_state"]
        port  = self.config.webhook_port

        # Effect selector options
        options = "\n          ".join(
            f'<option value="{k}">{v["label"]}</option>'
            for k, v in EFFECTS.items()
        )

        # Webhook URL table rows (placeholder host replaced in JS)
        wh_rows = "\n      ".join(
            f'<tr><td>{v["label"]}</td>'
            f'<td><span class="url-code" data-url="http://<host>:{port}/webhook?effect={k}">'
            f'http://&lt;host&gt;:{port}/webhook?effect={k}</span></td></tr>'
            for k, v in EFFECTS.items()
        )

        # Effects reference table rows
        def mode_label(m):
            return {0: "Static", 1: "Chase Fwd", 2: "Chase Bwd",
                    6: "Breath", 15: "Strobe", 16: "Fade"}.get(m, str(m))

        def swatches(effect):
            if "frames" in effect:
                colors = [f["pixels"][0]["color"] for f in effect["frames"]]
            else:
                colors = [p["color"] for p in effect["pixels"] if not p.get("disable", False)]
            return "".join(
                f'<span class="swatch" style="background:#{c:06X}"></span>'
                for c in colors
            )

        def effect_mode_cell(v):
            if "saved_name" in v:
                return f'Saved: {v["saved_name"]}'
            if "frames" in v:
                return f'Cycle &times; {len(v["frames"])} frames'
            return mode_label(v["mode"])

        def effect_speed_cell(v):
            if "saved_name" in v:
                return "&mdash;"
            if "frames" in v:
                return f'{v["interval"]}s / frame'
            return str(v["speed"])

        fx_rows = "\n        ".join(
            f'<tr>'
            f'<td>{swatches(v)}<span class="fx-name">{k}</span></td>'
            f'<td>{v["label"]}</td>'
            f'<td>{effect_mode_cell(v)}</td>'
            f'<td>{effect_speed_cell(v)}</td>'
            f'<td>{v["brightness"]}</td>'
            f'</tr>'
            for k, v in EFFECTS.items()
        )

        # Activity log rows
        logs = self.alarm_sm.activity_log
        if logs:
            log_items = "\n      ".join(
                f'<li><time class="log-ts" data-utc="{ts}">{ts}</time>'
                f'<span class="{"log-error" if lv == "error" else "log-warning" if lv == "warning" else "log-msg"}">{msg}</span></li>'
                for ts, lv, msg in logs
            )
        else:
            log_items = '<li><span class="empty">No activity yet</span></li>'

        current_eff = sd["current_effect"]
        last_eff    = sd["last_effect"]

        html = _HTML.format(
            port                = port,
            state_upper         = state.upper(),
            state_class         = state,
            current_effect_disp = EFFECTS[current_eff]["label"] if current_eff else "—",
            device_id_short     = self.config.device_id[:16],
            alarm_timeout       = self.config.alarm_timeout,
            last_triggered      = sd["last_triggered"] or "\u2014",
            last_triggered_disp = sd["last_triggered"] or "Never",
            last_effect_disp    = EFFECTS[last_eff]["label"] if last_eff else "—",
            last_restored       = sd["last_restored"] or "\u2014",
            last_restored_disp  = sd["last_restored"] or "Never",
            effect_options      = options,
            webhook_rows        = wh_rows,
            effects_rows        = fx_rows,
            activity_items      = log_items,
        )
        self._html(html)

    # -- webhook handler ----------------------------------------------------

    def _handle_webhook(self):
        effect_name = self._effect_from_query()
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        log.debug("Webhook raw (%d bytes) effect=%s: %s", len(raw), effect_name, raw[:400])

        try:
            data = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("Bad webhook payload: %s", exc)
            self._json(400, {"error": "invalid JSON"})
            return

        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                pass

        if not isinstance(data, dict):
            self._json(400, {"error": "expected JSON object"})
            return

        # Validate this is a real UniFi Protect alarm payload (has triggers)
        alarm    = data.get("alarm") or data.get("Alarm") or {}
        triggers = []
        if isinstance(alarm, dict):
            triggers = alarm.get("triggers") or alarm.get("Triggers") or []

        if not triggers:
            log.debug("Webhook: no triggers in payload — ignoring")
            self._json(200, {"triggered": False, "reason": "no triggers in payload"})
            return

        trigger_keys = [t.get("key") or t.get("Key", "") for t in triggers if isinstance(t, dict)]
        log.info("Webhook trigger keys=%s effect=%s", trigger_keys, effect_name)

        threading.Thread(
            target=self.alarm_sm.trigger,
            args=(effect_name,), daemon=True,
        ).start()
        self._json(200, {
            "triggered":    True,
            "effect":       effect_name,
            "effect_label": EFFECTS[effect_name]["label"],
            "trigger_keys": trigger_keys,
        })

    # -- test handler -------------------------------------------------------

    def _handle_test(self):
        try:
            data = self._read_json()
        except Exception:
            data = {}
        effect_name = data.get("effect", DEFAULT_EFFECT)
        if effect_name not in EFFECTS:
            effect_name = DEFAULT_EFFECT
        log.info("Test trigger from UI: effect=%s", effect_name)
        threading.Thread(
            target=self.alarm_sm.trigger,
            args=(effect_name,), daemon=True,
        ).start()
        self._json(200, {
            "triggered":    True,
            "effect":       effect_name,
            "effect_label": EFFECTS[effect_name]["label"],
        })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    config = Config()
    config.validate()

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    log.info("Starting UniFi Protect → Trimlight alarm service")
    log.info("  API URL       : %s", config.api_url)
    log.info("  Device ID     : %s", config.device_id)
    log.info("  Port          : %d", config.webhook_port)
    log.info("  Alarm timeout : %ds", config.alarm_timeout)
    log.info("  Effects       : %s", ", ".join(EFFECTS))

    alarm_sm = AlarmStateMachine(config)
    WebhookHandler.alarm_sm = alarm_sm
    WebhookHandler.config   = config

    server = HTTPServer(("0.0.0.0", config.webhook_port), WebhookHandler)

    def _shutdown(sig, _frame):
        log.info("Received signal %d — shutting down", sig)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("Listening on 0.0.0.0:%d", config.webhook_port)
    log.info("Status page → http://0.0.0.0:%d/", config.webhook_port)
    server.serve_forever()
    log.info("Server stopped")


if __name__ == "__main__":
    main()
