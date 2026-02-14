#!/usr/bin/env python3
"""
UniFi Protect Animal Alarm → Trimlight Red Alert

Receives webhook POSTs from UniFi Protect's Alarm Manager when an animal is
detected and turns the Trimlight Edge lights solid red. After a configurable
timeout the lights auto-restore to their previous state.

Requires only Python stdlib — no pip dependencies.
"""

import json
import logging
import os
import signal
import socket
import struct
import sys
import threading
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

# ---------------------------------------------------------------------------
# Constants – Trimlight binary protocol
# ---------------------------------------------------------------------------
START_FLAG = 0x5A
END_FLAG = 0xA5

CMD_SYNC_DETAIL = 0x02
CMD_CHECK_PATTERN = 0x03
CMD_CHECK_DEVICE = 0x0C
CMD_SET_MODE = 0x0D
CMD_PREVIEW_PATTERN = 0x13
CMD_SET_SOLID_COLOR = 0x14

MODE_TIMER = 0x00
MODE_MANUAL = 0x01

# Solid-red colour
RED = (0xFF, 0x00, 0x00)

log = logging.getLogger("alarm")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class Config:
    """Read and validate configuration from environment variables."""

    def __init__(self):
        self.trimlight_host = os.environ.get("TRIMLIGHT_HOST", "")
        self.trimlight_port = int(os.environ.get("TRIMLIGHT_PORT", "8189"))
        self.webhook_port = int(os.environ.get("WEBHOOK_PORT", "8080"))
        self.alarm_timeout = int(os.environ.get("ALARM_TIMEOUT", "30"))
        self.trigger_key = os.environ.get("TRIGGER_KEY", "animal")
        self.log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    def validate(self):
        if not self.trimlight_host:
            sys.exit("ERROR: TRIMLIGHT_HOST environment variable is required")
        if self.alarm_timeout < 1:
            sys.exit("ERROR: ALARM_TIMEOUT must be >= 1")

# ---------------------------------------------------------------------------
# Trimlight TCP client
# ---------------------------------------------------------------------------
class TrimlightClient:
    """Communicate with a Trimlight Edge controller over its binary TCP
    protocol on port 8189."""

    RECV_TIMEOUT = 5  # seconds

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._sock = None

    # -- connection lifecycle -----------------------------------------------

    def connect(self):
        """Open a TCP connection to the controller."""
        log.info("Connecting to %s:%d", self.host, self.port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(self.RECV_TIMEOUT)
        self._sock.connect((self.host, self.port))
        log.info("Connected")

    def close(self):
        """Close the TCP connection."""
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            log.debug("Connection closed")

    # -- low-level framing --------------------------------------------------

    @staticmethod
    def _build_frame(command: int, payload: bytes) -> bytes:
        """Build a framed message: [0x5A] [cmd] [len BE16] [payload] [0xA5]"""
        length = len(payload)
        frame = bytes([START_FLAG, command]) + struct.pack(">H", length) + payload + bytes([END_FLAG])
        return frame

    def _send(self, command: int, payload: bytes):
        """Send a framed command to the controller."""
        frame = self._build_frame(command, payload)
        log.debug("TX cmd=0x%02X len=%d  %s", command, len(payload), frame.hex())
        self._sock.sendall(frame)

    def _recv(self) -> tuple:
        """Receive one framed response.  Returns (command, payload)."""
        # Read start flag + command + 2-byte length
        header = self._recv_exact(4)
        if header[0] != START_FLAG:
            raise ProtocolError(f"Bad start flag: 0x{header[0]:02X}")
        cmd = header[1]
        length = struct.unpack(">H", header[2:4])[0]
        payload = self._recv_exact(length) if length else b""
        end = self._recv_exact(1)
        if end[0] != END_FLAG:
            raise ProtocolError(f"Bad end flag: 0x{end[0]:02X}")
        log.debug("RX cmd=0x%02X len=%d  %s", cmd, length, payload.hex())
        return cmd, payload

    def _recv_exact(self, n: int) -> bytes:
        """Read exactly *n* bytes from the socket."""
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Socket closed while reading")
            buf.extend(chunk)
        return bytes(buf)

    # -- high-level commands ------------------------------------------------

    def handshake(self):
        """Send CMD_CHECK_DEVICE (0x0C) with verification bytes + datetime."""
        now = datetime.now()
        payload = bytes([
            0x56, 0x56,  # verification bytes
            now.year - 2000,
            now.month,
            now.day,
            now.hour,
            now.minute,
            now.second,
        ])
        self._send(CMD_CHECK_DEVICE, payload)
        cmd, resp = self._recv()
        log.info("Handshake response: cmd=0x%02X payload=%s", cmd, resp.hex())

    def sync_detail(self) -> dict:
        """Query the current state (mode, pattern ID, etc.)."""
        self._send(CMD_SYNC_DETAIL, b"")
        cmd, payload = self._recv()
        if len(payload) < 3:
            log.warning("sync_detail: short payload (%d bytes)", len(payload))
            return {"raw": payload, "mode": None, "pattern_id": None}
        mode = payload[0]
        pattern_id = payload[1]
        log.info(
            "sync_detail: mode=%d (%s)  pattern_id=%d",
            mode,
            "Timer" if mode == MODE_TIMER else "Manual",
            pattern_id,
        )
        return {"raw": payload, "mode": mode, "pattern_id": pattern_id}

    def set_mode(self, mode: int):
        """Switch between Timer (0) and Manual (1) mode."""
        label = "Timer" if mode == MODE_TIMER else "Manual"
        log.info("Setting mode → %s (%d)", label, mode)
        self._send(CMD_SET_MODE, bytes([mode]))
        cmd, resp = self._recv()
        log.debug("set_mode response: %s", resp.hex())

    def set_solid_color(self, r: int, g: int, b: int):
        """Set all pixels to a single colour (0x14)."""
        log.info("Setting solid colour → #%02X%02X%02X", r, g, b)
        self._send(CMD_SET_SOLID_COLOR, bytes([r, g, b]))
        try:
            cmd, resp = self._recv()
            log.debug("set_solid_color response: %s", resp.hex())
        except socket.timeout:
            log.debug("No response to set_solid_color (may be normal)")

    def preview_pattern_solid(self, r: int, g: int, b: int):
        """Fallback: send a solid colour via CMD_PREVIEW_PATTERN (0x13).

        The 31-byte payload encodes a single-colour static pattern compatible
        with Trimlight Edge controllers that may not support 0x14.
        """
        log.info("Preview-pattern fallback → solid #%02X%02X%02X", r, g, b)
        # Pattern layout: 7 colour slots (3 bytes each = 21) + 10 config bytes
        colours = bytes([r, g, b]) + bytes(18)  # one colour + 6 unused slots
        config = bytes([
            0x01,  # number of colours used
            0x00,  # animation mode (0 = static)
            0x00, 0x00,  # speed (unused for static)
            0x00, 0x00,  # brightness (controller default)
            0x00, 0x00, 0x00, 0x00,  # reserved
        ])
        payload = colours + config
        assert len(payload) == 31, f"Expected 31-byte payload, got {len(payload)}"
        self._send(CMD_PREVIEW_PATTERN, payload)
        try:
            cmd, resp = self._recv()
            log.debug("preview_pattern response: %s", resp.hex())
        except socket.timeout:
            log.debug("No response to preview_pattern (may be normal)")

    def check_pattern(self, pattern_id: int):
        """Restore a saved pattern by its ID (0x03)."""
        log.info("Restoring pattern ID %d", pattern_id)
        self._send(CMD_CHECK_PATTERN, bytes([pattern_id]))
        try:
            cmd, resp = self._recv()
            log.debug("check_pattern response: %s", resp.hex())
        except socket.timeout:
            log.debug("No response to check_pattern (may be normal)")

    def activate_red(self):
        """Set the lights solid red. Tries 0x14 first, falls back to 0x13."""
        try:
            self.set_solid_color(*RED)
        except Exception as exc:
            log.warning("set_solid_color failed (%s), trying preview fallback", exc)
            self.preview_pattern_solid(*RED)


class ProtocolError(Exception):
    """Unexpected data in the Trimlight protocol stream."""


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
        self._saved_mode = None
        self._saved_pattern_id = None

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
                # Let the restore finish, then we'll re-trigger from idle.
                # Queue a delayed re-trigger.
                threading.Thread(target=self._wait_and_retrigger, daemon=True).start()
                return
            # IDLE → ALARMED
            self._state = self.ALARMED
            log.info("State → ALARMED")

        # Outside the lock: talk to the controller (blocking I/O)
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
        """Connect, handshake, save state, switch to manual, set red."""
        client = TrimlightClient(self.config.trimlight_host, self.config.trimlight_port)
        try:
            client.connect()
            client.handshake()
            detail = client.sync_detail()
            self._saved_mode = detail.get("mode")
            self._saved_pattern_id = detail.get("pattern_id")
            client.set_mode(MODE_MANUAL)
            client.activate_red()
        finally:
            client.close()

    def _restore(self):
        """Reconnect and restore the previous state."""
        with self._lock:
            if self._state != self.ALARMED:
                return
            self._state = self.RESTORING
            log.info("State → RESTORING")

        try:
            client = TrimlightClient(self.config.trimlight_host, self.config.trimlight_port)
            try:
                client.connect()
                client.handshake()
                if self._saved_mode == MODE_MANUAL and self._saved_pattern_id is not None:
                    log.info("Previous mode was Manual — restoring pattern %d", self._saved_pattern_id)
                    client.set_mode(MODE_MANUAL)
                    client.check_pattern(self._saved_pattern_id)
                else:
                    log.info("Restoring Timer mode (controller resumes schedule)")
                    client.set_mode(MODE_TIMER)
            finally:
                client.close()
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
    log.info(
        "  Trimlight host : %s:%d",
        config.trimlight_host,
        config.trimlight_port,
    )
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
