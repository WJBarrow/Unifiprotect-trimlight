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
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

log = logging.getLogger("alarm")

# Solid red as decimal integer: 0xFF0000 = 16711680
COLOR_RED = 16711680

# Switch states
SWITCH_OFF = 0
SWITCH_MANUAL = 1
SWITCH_TIMER = 2

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class Config:
    """Read and validate configuration from environment variables."""

    def __init__(self):
        self.client_id = os.environ.get("TRIMLIGHT_CLIENT_ID", "")
        self.client_secret = os.environ.get("TRIMLIGHT_CLIENT_SECRET", "")
        self.device_id = os.environ.get("TRIMLIGHT_DEVICE_ID", "")
        self.api_url = os.environ.get(
            "TRIMLIGHT_API_URL", "https://trimlight.ledhue.com/trimlight"
        )
        self.webhook_port = int(os.environ.get("WEBHOOK_PORT", "8080"))
        self.alarm_timeout = int(os.environ.get("ALARM_TIMEOUT", "30"))
        self.trigger_key = os.environ.get("TRIGGER_KEY", "animal")
        self.log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    def validate(self):
        missing = []
        if not self.client_id:
            missing.append("TRIMLIGHT_CLIENT_ID")
        if not self.client_secret:
            missing.append("TRIMLIGHT_CLIENT_SECRET")
        if not self.device_id:
            missing.append("TRIMLIGHT_DEVICE_ID")
        if missing:
            sys.exit("ERROR: Required environment variables not set: " + ", ".join(missing))
        if self.alarm_timeout < 1:
            sys.exit("ERROR: ALARM_TIMEOUT must be >= 1")


# ---------------------------------------------------------------------------
# Trimlight Cloud API client
# ---------------------------------------------------------------------------
class TrimlightClient:
    """Communicate with a Trimlight Edge controller via the cloud REST API.

    API docs: https://trimlight.com/hubfs/Manuals/Trimlight_Edge_API_Documentation%208192022.pdf
    Base URL: POST https://trimlight.ledhue.com/trimlight/<path>
    Auth:     HMAC-SHA256(clientSecret, "Trimlight|<clientId>|<timestamp>")
    """

    REQUEST_TIMEOUT = 15  # seconds

    def __init__(self, config: Config):
        self.config = config

    # -- auth ---------------------------------------------------------------

    def _auth_headers(self) -> dict:
        """Build the required authentication headers."""
        timestamp = str(int(time.time() * 1000))
        message = f"Trimlight|{self.config.client_id}|{timestamp}"
        mac = hmac.new(
            self.config.client_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        token = base64.b64encode(mac).decode("utf-8")
        return {
            "authorization": token,
            "S-ClientId": self.config.client_id,
            "S-Timestamp": timestamp,
            "Content-Type": "application/json",
        }

    # -- low-level request --------------------------------------------------

    def _post(self, path: str, body: dict) -> dict:
        """Send a POST request to the Trimlight cloud API."""
        url = self.config.api_url + path
        data = json.dumps(body).encode("utf-8")
        headers = self._auth_headers()

        log.debug("API POST %s  body=%s", path, json.dumps(body))
        req = Request(url, data=data, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=self.REQUEST_TIMEOUT) as resp:
                raw = resp.read()
                result = json.loads(raw)
                log.debug("API response: %s", json.dumps(result))
                if result.get("code") != 0:
                    raise APIError(
                        f"API error: code={result.get('code')} desc={result.get('desc')}"
                    )
                return result
        except HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise APIError(f"HTTP {exc.code}: {body_text}") from exc
        except URLError as exc:
            raise APIError(f"Request failed: {exc.reason}") from exc

    # -- high-level commands ------------------------------------------------

    def get_device_detail(self) -> dict:
        """Get device detail data (API #4)."""
        now = datetime.now()
        weekday = now.isoweekday()  # Mon=1..Sun=7
        # API uses: SUNDAY=1, MONDAY=2, ..., SATURDAY=7
        api_weekday = 1 if weekday == 7 else weekday + 1

        result = self._post("/v1/oauth/resources/device/get", {
            "deviceId": self.config.device_id,
            "currentDate": {
                "year": now.year - 2000,
                "month": now.month,
                "day": now.day,
                "weekday": api_weekday,
                "hours": now.hour,
                "minutes": now.minute,
                "seconds": now.second,
            },
        })
        payload = result.get("payload", {})
        log.info(
            "Device detail: switchState=%s connectivity=%s",
            payload.get("switchState"),
            payload.get("connectivity"),
        )
        return payload

    def set_switch_state(self, state: int):
        """Set device switch state (API #5).

        0 = light off, 1 = manual mode, 2 = timer mode.
        """
        labels = {0: "Off", 1: "Manual", 2: "Timer"}
        log.info("Setting switch state → %s (%d)", labels.get(state, "?"), state)
        self._post("/v1/oauth/resources/device/update", {
            "deviceId": self.config.device_id,
            "payload": {"switchState": state},
        })

    def preview_custom_effect(self, pixels: list, mode: int = 0,
                              speed: int = 100, brightness: int = 255):
        """Preview a custom effect on the device (API #11).

        mode 0 = STATIC, see docs for other modes.
        """
        log.info("Previewing custom effect: mode=%d pixels=%d", mode, len(pixels))
        self._post("/v1/oauth/resources/device/effect/preview", {
            "deviceId": self.config.device_id,
            "payload": {
                "category": 1,
                "mode": mode,
                "speed": speed,
                "brightness": brightness,
                "pixels": pixels,
            },
        })

    def view_effect(self, effect_id: int):
        """Check out / activate a saved effect by ID (API #13)."""
        log.info("Activating saved effect ID %d", effect_id)
        self._post("/v1/oauth/resources/device/effect/view", {
            "deviceId": self.config.device_id,
            "payload": {"id": effect_id},
        })

    def notify_update_shadow(self):
        """Notify device to report latest shadow data (API #25)."""
        now = datetime.now()
        weekday = now.isoweekday()
        api_weekday = 1 if weekday == 7 else weekday + 1

        log.debug("Notifying device to update shadow data")
        self._post("/v1/oauth/resources/device/notify-update-shadow", {
            "deviceId": self.config.device_id,
            "currentDate": {
                "year": now.year - 2000,
                "month": now.month,
                "day": now.day,
                "weekday": api_weekday,
                "hours": now.hour,
                "minutes": now.minute,
                "seconds": now.second,
            },
        })

    def activate_red(self):
        """Set all lights to solid red."""
        self.set_switch_state(SWITCH_MANUAL)
        self.preview_custom_effect(
            pixels=[{"index": 0, "count": 60, "color": COLOR_RED, "disable": False}],
            mode=0,       # STATIC
            speed=100,
            brightness=255,
        )


class APIError(Exception):
    """Error communicating with the Trimlight cloud API."""


# ---------------------------------------------------------------------------
# Alarm state machine
# ---------------------------------------------------------------------------
class AlarmStateMachine:
    """
    IDLE ──trigger──▸ ALARMED ──timeout──▸ RESTORING ──done──▸ IDLE

    Debounce: re-triggering while ALARMED resets the timer without resending
    the red command.
    """

    IDLE = "idle"
    ALARMED = "alarmed"
    RESTORING = "restoring"

    def __init__(self, config: Config):
        self.config = config
        self._state = self.IDLE
        self._lock = threading.Lock()
        self._timer = None
        self._saved_switch_state = None
        self._saved_effect_id = None

    @property
    def state(self) -> str:
        return self._state

    # -- public interface ---------------------------------------------------

    def trigger(self):
        """Called when the webhook fires.  Thread-safe."""
        with self._lock:
            if self._state == self.ALARMED:
                log.info("Already alarmed — resetting timer (debounce)")
                self._reset_timer()
                return
            if self._state == self.RESTORING:
                log.info("Restore in progress — will re-alarm after")
                threading.Thread(target=self._wait_and_retrigger, daemon=True).start()
                return
            # IDLE → ALARMED
            self._state = self.ALARMED
            log.info("State → ALARMED")

        # Outside the lock: talk to the cloud API (blocking I/O)
        try:
            self._activate_alarm()
        except Exception:
            log.exception("Failed to activate alarm")
            with self._lock:
                self._state = self.IDLE
            return

        with self._lock:
            self._reset_timer()

    # -- internals ----------------------------------------------------------

    def _activate_alarm(self):
        """Query current state, switch to manual, set solid red."""
        client = TrimlightClient(self.config)
        # Request fresh data from the device
        try:
            client.notify_update_shadow()
            time.sleep(1)  # give the device a moment to report
        except Exception:
            log.debug("notify_update_shadow failed (non-fatal), continuing")

        detail = client.get_device_detail()
        self._saved_switch_state = detail.get("switchState")
        current_effect = detail.get("currentEffect") or {}
        effect_id = current_effect.get("id") if isinstance(current_effect, dict) else None
        self._saved_effect_id = effect_id if effect_id is not None and effect_id >= 0 else None
        log.info(
            "Saved state: switchState=%s effectId=%s",
            self._saved_switch_state,
            self._saved_effect_id,
        )
        client.activate_red()

    def _restore(self):
        """Restore the previous state."""
        with self._lock:
            if self._state != self.ALARMED:
                return
            self._state = self.RESTORING
            log.info("State → RESTORING")

        try:
            client = TrimlightClient(self.config)
            if self._saved_switch_state == SWITCH_TIMER:
                log.info("Restoring Timer mode (controller resumes schedule)")
                client.set_switch_state(SWITCH_TIMER)
            elif self._saved_switch_state == SWITCH_MANUAL:
                log.info("Previous mode was Manual")
                if self._saved_effect_id is not None:
                    log.info("Restoring saved effect ID %d", self._saved_effect_id)
                    client.view_effect(self._saved_effect_id)
                else:
                    log.info("No saved effect ID — switching to Timer mode")
                    client.set_switch_state(SWITCH_TIMER)
            elif self._saved_switch_state == SWITCH_OFF:
                log.info("Previous mode was Off — turning lights off")
                client.set_switch_state(SWITCH_OFF)
            else:
                log.warning(
                    "Unknown saved switchState=%s — defaulting to Timer",
                    self._saved_switch_state,
                )
                client.set_switch_state(SWITCH_TIMER)
        except Exception:
            log.exception("Failed to restore — lights may remain red")

        with self._lock:
            self._state = self.IDLE
            log.info("State → IDLE")

    def _reset_timer(self):
        """(Re)start the restore timer.  Must be called under self._lock."""
        if self._timer is not None:
            self._timer.cancel()
        log.info("Timer set: %ds", self.config.alarm_timeout)
        self._timer = threading.Timer(self.config.alarm_timeout, self._restore)
        self._timer.daemon = True
        self._timer.start()

    def _wait_and_retrigger(self):
        """Wait for RESTORING to finish, then re-trigger."""
        for _ in range(50):  # up to ~5 s
            time.sleep(0.1)
            if self._state == self.IDLE:
                self.trigger()
                return
        log.warning("Timed out waiting for restore to finish; dropping re-trigger")


# ---------------------------------------------------------------------------
# HTTP webhook handler
# ---------------------------------------------------------------------------
class WebhookHandler(BaseHTTPRequestHandler):
    """Handles POST webhooks from UniFi Protect and GET health checks."""

    # Attached by main() before the server starts
    alarm_sm = None  # type: AlarmStateMachine
    config = None  # type: Config

    def do_GET(self):
        """Health check."""
        body = json.dumps({
            "status": "ok",
            "alarm_state": self.alarm_sm.state,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        """Receive a UniFi Protect Alarm Manager webhook."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._respond(400, {"error": "Empty body"})
            return

        raw = self.rfile.read(content_length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("Invalid JSON: %s", exc)
            self._respond(400, {"error": "Invalid JSON"})
            return

        log.debug("Webhook payload: %s", json.dumps(data, indent=2))

        # Look for the configured trigger key in alarm.triggers[].key
        triggers = []
        alarm = data.get("alarm") or data.get("Alarm") or {}
        if isinstance(alarm, dict):
            triggers = alarm.get("triggers") or alarm.get("Triggers") or []

        matched = any(
            t.get("key") == self.config.trigger_key or t.get("Key") == self.config.trigger_key
            for t in triggers
            if isinstance(t, dict)
        )

        if matched:
            log.info("Trigger matched: key=%s", self.config.trigger_key)
            threading.Thread(target=self.alarm_sm.trigger, daemon=True).start()
            self._respond(200, {"triggered": True})
        else:
            log.debug("No matching trigger key '%s' in payload", self.config.trigger_key)
            self._respond(200, {"triggered": False})

    def _respond(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Route BaseHTTPRequestHandler logs through our logger."""
        log.info(format, *args)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    config = Config()
    config.validate()

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    log.info("Starting UniFi Protect → Trimlight alarm service")
    log.info("  API URL        : %s", config.api_url)
    log.info("  Device ID      : %s", config.device_id)
    log.info("  Webhook port   : %d", config.webhook_port)
    log.info("  Alarm timeout  : %ds", config.alarm_timeout)
    log.info("  Trigger key    : %s", config.trigger_key)

    alarm_sm = AlarmStateMachine(config)

    WebhookHandler.alarm_sm = alarm_sm
    WebhookHandler.config = config

    server = HTTPServer(("0.0.0.0", config.webhook_port), WebhookHandler)

    def _shutdown(signum, frame):
        log.info("Received signal %d — shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("Listening on 0.0.0.0:%d", config.webhook_port)
    server.serve_forever()
    log.info("Server stopped")


if __name__ == "__main__":
    main()
