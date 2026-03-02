#!/usr/bin/env python3
"""
UniFi Protect Animal Alarm → Trimlight Red Alert

Receives webhook POSTs from UniFi Protect's Alarm Manager when an animal is
detected and turns the Trimlight Edge lights solid red via the Trimlight
cloud API.  After a configurable timeout the lights auto-restore to their
previous state.

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
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

log = logging.getLogger("alarm")

# Solid red as decimal integer: 0xFF0000 = 16711680
COLOR_RED = 16711680

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
        self.trigger_key   = os.environ.get("TRIGGER_KEY", "animal")
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
        mac       = hmac.new(
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
        weekday = now.isoweekday()              # Mon=1 … Sun=7
        return {
            "year":    now.year - 2000,
            "month":   now.month,
            "day":     now.day,
            "weekday": 1 if weekday == 7 else weekday + 1,   # Sun=1 … Sat=7
            "hours":   now.hour,
            "minutes": now.minute,
            "seconds": now.second,
        }

    # -- high-level commands ------------------------------------------------

    def notify_update_shadow(self):
        """Ask the device to push its latest state to the cloud (API #25)."""
        log.debug("notify_update_shadow")
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
        log.info(
            "Device detail: switchState=%s connectivity=%s",
            payload.get("switchState"), payload.get("connectivity"),
        )
        return payload

    def set_switch_state(self, state: int):
        """0=Off  1=Manual  2=Timer (API #5)."""
        labels = {0: "Off", 1: "Manual", 2: "Timer"}
        log.info("set_switch_state → %s (%d)", labels.get(state, "?"), state)
        self._post("/v1/oauth/resources/device/update", {
            "deviceId": self.config.device_id,
            "payload":  {"switchState": state},
        })

    def preview_solid_red(self):
        """Preview a solid-red static custom effect (API #11)."""
        log.info("preview_solid_red")
        self._post("/v1/oauth/resources/device/effect/preview", {
            "deviceId": self.config.device_id,
            "payload": {
                "category":   2,        # Edge uses category 2 (docs say 1, device expects 2)
                "mode":       0,        # STATIC
                "speed":      100,
                "brightness": 255,
                "pixels": [
                    {"index": 0, "count": 1, "color": COLOR_RED, "disable": False},
                ],
            },
        })

    def view_effect(self, effect_id: int):
        """Activate a saved effect by ID (API #13)."""
        log.info("view_effect id=%d", effect_id)
        self._post("/v1/oauth/resources/device/effect/view", {
            "deviceId": self.config.device_id,
            "payload":  {"id": effect_id},
        })


# ---------------------------------------------------------------------------
# Alarm state machine
# ---------------------------------------------------------------------------
class AlarmStateMachine:
    """
    IDLE ──trigger──▸ ALARMED ──timeout──▸ RESTORING ──done──▸ IDLE

    Debounce: re-triggering while ALARMED resets the timeout timer.
    """

    IDLE       = "idle"
    ALARMED    = "alarmed"
    RESTORING  = "restoring"
    MAX_LOG    = 30

    def __init__(self, config: Config):
        self.config              = config
        self._state              = self.IDLE
        self._lock               = threading.Lock()
        self._timer              = None
        self._saved_switch_state = None
        self._saved_effect_id    = None
        self.last_triggered      = None   # ISO string
        self.last_restored       = None   # ISO string
        self.activity_log        = []     # [(ts_str, level, message), …]

    @property
    def state(self) -> str:
        return self._state

    # -- activity log -------------------------------------------------------

    def _log(self, message: str, level: str = "info"):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            self.activity_log.insert(0, (ts, level, message))
            if len(self.activity_log) > self.MAX_LOG:
                self.activity_log = self.activity_log[:self.MAX_LOG]
        getattr(log, level, log.info)(message)

    # -- public interface ---------------------------------------------------

    def trigger(self):
        """Called when the webhook fires.  Thread-safe."""
        with self._lock:
            if self._state == self.ALARMED:
                self._log("Already alarmed — resetting timer (debounce)")
                self._reset_timer()
                return
            if self._state == self.RESTORING:
                self._log("Restore in progress — queuing re-alarm")
                threading.Thread(target=self._wait_and_retrigger, daemon=True).start()
                return
            self._state = self.ALARMED
            self.last_triggered = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        self._log("Animal detected — activating alarm")

        try:
            self._activate_alarm()
        except Exception as exc:
            self._log(f"Failed to activate alarm: {exc}", level="error")
            log.exception("Alarm activation error")
            with self._lock:
                self._state = self.IDLE
            return

        self._log(f"Lights set to solid red — restoring in {self.config.alarm_timeout}s")
        with self._lock:
            self._reset_timer()

    # -- internals ----------------------------------------------------------

    def _activate_alarm(self):
        client = TrimlightClient(self.config)
        try:
            client.notify_update_shadow()
            time.sleep(1)
        except Exception:
            log.debug("notify_update_shadow failed (non-fatal)")

        detail = client.get_device_detail()
        self._saved_switch_state = detail.get("switchState")
        effect = detail.get("currentEffect") or {}
        eid    = effect.get("id") if isinstance(effect, dict) else None
        self._saved_effect_id = eid if eid is not None and eid >= 0 else None
        log.info("Saved state: switchState=%s effectId=%s",
                 self._saved_switch_state, self._saved_effect_id)

        client.set_switch_state(SWITCH_MANUAL)
        client.preview_solid_red()

    def _restore(self):
        with self._lock:
            if self._state != self.ALARMED:
                return
            self._state = self.RESTORING

        self._log("Restoring previous state…")
        try:
            client = TrimlightClient(self.config)
            saved  = self._saved_switch_state

            if saved == SWITCH_TIMER:
                # Turn off first to clear the preview, then resume schedule
                self._log("Restoring Timer mode (schedule will resume)")
                client.set_switch_state(SWITCH_OFF)
                time.sleep(0.5)
                client.set_switch_state(SWITCH_TIMER)

            elif saved == SWITCH_MANUAL and self._saved_effect_id is not None:
                self._log(f"Restoring Manual effect ID {self._saved_effect_id}")
                client.view_effect(self._saved_effect_id)

            elif saved == SWITCH_OFF:
                self._log("Previous state was Off — turning off")
                client.set_switch_state(SWITCH_OFF)

            else:
                self._log("Previous state unknown — defaulting to Timer mode", level="warning")
                client.set_switch_state(SWITCH_OFF)
                time.sleep(0.5)
                client.set_switch_state(SWITCH_TIMER)

        except Exception as exc:
            self._log(f"Restore failed: {exc} — lights may remain red", level="error")
            log.exception("Restore error")

        with self._lock:
            self._state = self.IDLE
            self.last_restored = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._log("Lights restored — system idle")

    def _reset_timer(self):
        """(Re)start the restore countdown.  Call under self._lock."""
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self.config.alarm_timeout, self._restore)
        self._timer.daemon = True
        self._timer.start()

    def _wait_and_retrigger(self):
        for _ in range(50):
            time.sleep(0.1)
            if self._state == self.IDLE:
                self.trigger()
                return
        log.warning("Timed out waiting for restore — dropping re-trigger")

    def get_state_dict(self) -> dict:
        with self._lock:
            return {
                "alarm_state":    self._state,
                "last_triggered": self.last_triggered,
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
      padding: 0.45rem 0; border-bottom: 1px solid #263347;
      font-size: 0.875rem;
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
    button {{
      width: 100%; padding: 0.75rem 1rem;
      background: #dc2626; color: #fff;
      border: none; border-radius: 8px; font-size: 0.95rem;
      font-weight: 600; cursor: pointer; transition: background 0.15s;
      margin-top: 0.5rem;
    }}
    button:hover {{ background: #b91c1c; }}
    button:disabled {{ background: #334155; color: #64748b; cursor: not-allowed; }}
    #result {{
      margin-top: 0.75rem; padding: 0.65rem 0.9rem;
      border-radius: 8px; font-size: 0.875rem; display: none;
    }}
    #result.ok  {{ background: #052e16; color: #4ade80; display: block; }}
    #result.err {{ background: #450a0a; color: #f87171; display: block; }}
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
    .webhook-url {{
      font-family: monospace; font-size: 0.8rem; color: #fb923c;
      background: #1a1a2e; padding: 0.5rem 0.75rem;
      border-radius: 6px; margin-top: 0.75rem; word-break: break-all;
    }}
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
        <span class="stat-label">Device ID</span>
        <span class="stat-value">{device_id_short}&hellip;</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Alarm Timeout</span>
        <span class="stat-value">{alarm_timeout}s</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Trigger Key</span>
        <span class="stat-value">{trigger_key}</span>
      </div>
    </div>

    <!-- Last alarm card -->
    <div class="card">
      <h2>Last Alarm</h2>
      <div class="stat-row">
        <span class="stat-label">Triggered</span>
        <span class="stat-value">
          <time data-utc="{last_triggered}">{last_triggered_disp}</time>
        </span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Restored</span>
        <span class="stat-value">
          <time data-utc="{last_restored}">{last_restored_disp}</time>
        </span>
      </div>
    </div>

    <!-- Test card -->
    <div class="card">
      <h2>Test &mdash; Simulate Animal Detection</h2>
      <p style="font-size:0.82rem;color:#94a3b8;margin-bottom:0.75rem">
        Triggers the alarm immediately, same as a real UniFi Protect webhook.
        Lights will go red and auto-restore after {alarm_timeout}s.
      </p>
      <button id="triggerBtn" onclick="triggerAlarm()">&#128308; Trigger Alarm Now</button>
      <div id="result"></div>
    </div>
  </div>

  <!-- Webhook info -->
  <div class="card" style="margin-bottom:1.25rem">
    <h2>UniFi Protect Webhook URL</h2>
    <p style="font-size:0.82rem;color:#94a3b8;margin-bottom:0.5rem">
      Set this URL in Protect &rarr; Alarm Manager &rarr; Webhook action (POST method):
    </p>
    <div class="webhook-url" id="webhookUrl">http://&lt;this-host&gt;:{port}/webhook</div>
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
      if (v && v !== '—') {{
        try {{ el.textContent = new Date(v).toLocaleString(); }} catch(e) {{}}
      }}
    }});

    // Fill in the actual host
    document.getElementById('webhookUrl').textContent =
      'http://' + location.hostname + ':{port}/webhook';

    async function triggerAlarm() {{
      const btn    = document.getElementById('triggerBtn');
      const result = document.getElementById('result');
      btn.disabled = true;
      btn.textContent = 'Triggering\u2026';
      result.className = '';
      result.style.display = 'none';

      try {{
        const resp = await fetch('/test', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{}})
        }});
        const data = await resp.json();
        if (resp.ok && data.triggered) {{
          result.className = 'ok';
          result.textContent = '\u2713 Alarm triggered — lights going red';
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
        btn.textContent = '&#128308; Trigger Alarm Now';
      }}
    }}

    // Auto-refresh every 10s while alarmed
    const state = '{state_class}';
    if (state !== 'idle') setTimeout(() => location.reload(), 10000);
    else setTimeout(() => location.reload(), 30000);
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

    # -- helpers ------------------------------------------------------------

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
        if not length:
            return {}
        return json.loads(self.rfile.read(length))

    # -- routing ------------------------------------------------------------

    def do_GET(self):
        if self.path in ("/", "/status"):
            self._serve_ui()
        elif self.path == "/health":
            self._json(200, {"status": "ok", **self.alarm_sm.get_state_dict()})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/webhook":
            self._handle_webhook()
        elif self.path == "/test":
            self._handle_test()
        else:
            self._json(404, {"error": "not found"})

    # -- status page --------------------------------------------------------

    def _serve_ui(self):
        sd    = self.alarm_sm.get_state_dict()
        state = sd["alarm_state"]

        logs = self.alarm_sm.activity_log
        if logs:
            rows = []
            for ts, level, msg in logs:
                css = {"error": "log-error", "warning": "log-warning"}.get(level, "log-msg")
                rows.append(
                    f'<li><time class="log-ts" data-utc="{ts}">{ts}</time>'
                    f'<span class="{css}">{msg}</span></li>'
                )
            items = "\n      ".join(rows)
        else:
            items = '<li><span class="empty">No activity yet</span></li>'

        html = _HTML.format(
            port             = self.config.webhook_port,
            state_upper      = state.upper(),
            state_class      = state,
            device_id_short  = self.config.device_id[:16],
            alarm_timeout    = self.config.alarm_timeout,
            trigger_key      = self.config.trigger_key,
            last_triggered   = sd["last_triggered"] or "—",
            last_triggered_disp = sd["last_triggered"] or "Never",
            last_restored    = sd["last_restored"] or "—",
            last_restored_disp  = sd["last_restored"] or "Never",
            activity_items   = items,
        )
        self._html(html)

    # -- webhook handler ----------------------------------------------------

    def _handle_webhook(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        log.debug("Webhook raw (%d bytes): %s", len(raw), raw[:500])

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

        log.debug("Webhook parsed: %s", json.dumps(data))

        alarm    = data.get("alarm") or data.get("Alarm") or {}
        triggers = []
        if isinstance(alarm, dict):
            triggers = alarm.get("triggers") or alarm.get("Triggers") or []

        matched = any(
            t.get("key") == self.config.trigger_key or
            t.get("Key") == self.config.trigger_key
            for t in triggers if isinstance(t, dict)
        )

        if matched:
            log.info("Webhook trigger matched: key=%s", self.config.trigger_key)
            threading.Thread(target=self.alarm_sm.trigger, daemon=True).start()
            self._json(200, {"triggered": True})
        else:
            log.debug("Webhook: trigger key '%s' not matched", self.config.trigger_key)
            self._json(200, {"triggered": False})

    # -- test handler -------------------------------------------------------

    def _handle_test(self):
        log.info("Test trigger from UI")
        threading.Thread(target=self.alarm_sm.trigger, daemon=True).start()
        self._json(200, {"triggered": True})


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
    log.info("  Trigger key   : %s", config.trigger_key)

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
    log.info("Webhook URL → http://0.0.0.0:%d/webhook", config.webhook_port)
    server.serve_forever()
    log.info("Server stopped")


if __name__ == "__main__":
    main()
