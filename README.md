# UniFi Protect Animal Alarm → Trimlight Red Alert

Turn your Trimlight Edge lights solid red when UniFi Protect detects an animal. After a configurable timeout, the lights automatically restore to their previous state.

```
UniFi Protect ──POST webhook──▸ alarm.py (HTTP :8080) ──TCP :8189──▸ Trimlight Edge Controller
```

## Quick Start

```bash
cp .env.example .env
# Edit .env — set TRIMLIGHT_HOST to your controller's IP
docker compose up -d
```

## Configuration

All configuration is via environment variables (set in `.env`):

| Variable | Default | Description |
|----------|---------|-------------|
| `TRIMLIGHT_HOST` | *(required)* | Trimlight controller IP |
| `TRIMLIGHT_PORT` | `8189` | Trimlight TCP port |
| `WEBHOOK_PORT` | `8080` | HTTP listener port |
| `ALARM_TIMEOUT` | `30` | Seconds before auto-restore |
| `TRIGGER_KEY` | `animal` | Webhook trigger type to match |
| `LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

## How It Works

### Alarm State Machine

```
IDLE ──trigger──▸ ALARMED ──timeout──▸ RESTORING ──done──▸ IDLE
                     ▲                                       │
                     └──── trigger (debounce: reset timer) ──┘
```

1. **Trigger** — Webhook arrives with matching trigger key. The service connects to the Trimlight controller, saves the current mode, switches to Manual, and sets all lights solid red.
2. **Debounce** — If another trigger arrives while alarmed, the timeout timer resets without re-sending the red command.
3. **Restore** — After the timeout, the service reconnects and restores the previous state. If the controller was in Timer mode, it switches back so the schedule resumes automatically. If it was in Manual mode, the specific pattern is restored.

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/`  | Health check — returns `{"status":"ok","alarm_state":"idle"}` |
| `POST` | `/`  | Webhook receiver — accepts UniFi Protect alarm payloads |

### Trimlight Protocol

The service communicates with the Trimlight Edge controller over a binary TCP protocol on port 8189. Each message uses the envelope:

```
[0x5A start] [command byte] [length 2 bytes BE] [payload] [0xA5 end]
```

Commands used:
- **0x0C** Check Device — Handshake with verification bytes + datetime
- **0x02** Sync Detail — Query current mode/pattern
- **0x0D** Set Mode — Switch between Timer (0) and Manual (1)
- **0x14** Set Solid Color — 3-byte RGB payload (primary method)
- **0x13** Preview Custom Pattern — 31-byte payload (fallback for older firmware)
- **0x03** Check Pattern — Restore a saved pattern by ID

## UniFi Protect Setup

In the Protect UI → Alarm Manager:

1. Create an alarm with **Animal detection** as the condition
2. Add a **Webhook** notification action
3. Set the URL to `http://<docker-host-ip>:8080/`
4. Enable Advanced settings → select **POST** method

## Testing

**Health check:**
```bash
curl http://localhost:8080/
```

**Simulate an animal detection:**
```bash
curl -X POST http://localhost:8080/ \
  -H "Content-Type: application/json" \
  -d '{"alarm":{"triggers":[{"key":"animal","device":"camera1"}]},"timestamp":1}'
```

**Watch logs:**
```bash
docker compose logs -f
```

**Debug protocol traffic:**
Set `LOG_LEVEL=DEBUG` in `.env` to see raw bytes sent/received.

## Running Without Docker

Requires Python 3.6+ (stdlib only, no pip dependencies).

```bash
TRIMLIGHT_HOST=192.168.1.100 python3 alarm.py
```

## Design Decisions

- **Reconnect per alarm cycle** — Matches how the official Trimlight app interacts with the embedded controller. No persistent TCP connection to manage.
- **Restore via Timer mode** — Most users run schedules, so switching back to Timer lets the controller resume automatically.
- **stdlib only** — Zero external dependencies. Runs anywhere with Python 3.6+.
- **Dual color commands** — Tries `0x14` (Set Solid Color) first; falls back to `0x13` (Preview Custom Pattern) for Edge controllers that may not support it.
