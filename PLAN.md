# UniFi Protect Animal Alarm → Trimlight Red Alert

## Context

You want your Trimlight Edge lights to turn solid red when UniFi Protect detects an animal. After a configurable timeout, the lights auto-restore to their previous state. UniFi Protect's Alarm Manager can send a webhook POST on animal detection — this service receives that webhook and controls the Trimlight controller over the local network.

## Architecture

A single-file Python application (stdlib only, no pip) running in Docker:

```
UniFi Protect ──POST webhook──▸ alarm.py (HTTP :8080) ──TCP :8189──▸ Trimlight Edge Controller
```

## Files to Create

| File | Purpose |
|------|---------|
| `alarm.py` | All application logic (~400 lines) |
| `Dockerfile` | python:3.9-slim container |
| `docker-compose.yml` | Service config with env vars |
| `.env.example` | Configuration template |

## Configuration (Environment Variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `TRIMLIGHT_HOST` | *(required)* | Trimlight controller IP |
| `TRIMLIGHT_PORT` | `8189` | Trimlight TCP port |
| `WEBHOOK_PORT` | `8080` | HTTP listener port |
| `ALARM_TIMEOUT` | `30` | Seconds before auto-restore |
| `TRIGGER_KEY` | `animal` | Webhook trigger type to match |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

## How It Works

### Webhook Handling
- **POST /** — Receives UniFi Protect webhook, checks `alarm.triggers[].key == "animal"`, triggers alarm
- **GET /** — Health check returning JSON with alarm state

### Trimlight Protocol (TCP port 8189)
The Trimlight controller uses a binary protocol with this envelope:
```
[0x5A start] [command byte] [length 2 bytes BE] [payload] [0xA5 end]
```

Key commands used:
1. **0x0C Check Device** — Handshake with verification bytes + datetime
2. **0x02 Sync Detail** — Query current patterns/schedules/mode
3. **0x0D Set Mode** — Switch between Timer (0) and Manual (1)
4. **0x14 Set Solid Color** — 3-byte payload: R, G, B (primary method)
5. **0x13 Preview Custom Pattern** — 31-byte payload (fallback if 0x14 unsupported on Edge)
6. **0x03 Check Pattern** — Restore a saved pattern by ID

### Alarm State Machine

```
IDLE ──trigger──▸ ALARMED ──timeout──▸ RESTORING ──done──▸ IDLE
                     ▲                                       │
                     └──── trigger (debounce: reset timer) ──┘
```

1. **Trigger**: Connect → handshake → save current mode → switch to Manual → set solid red → start timer
2. **Debounce**: If triggered again while alarmed, reset the timer (don't re-send red command)
3. **Restore**: Reconnect → switch back to Timer mode (controller resumes its schedule automatically). If previously in Manual mode, restore the specific pattern ID instead.

### Key Design Decisions
- **Reconnect per alarm cycle** rather than persistent connection (matches how the official app works with the embedded controller)
- **Restore via Timer mode** — most users run schedules, so switching back to Timer lets the controller resume automatically
- **stdlib only** — no external dependencies, runs anywhere with Python 3.6+
- **Both 0x14 and 0x13 implemented** — tries Set Solid Color first; Preview Custom Pattern available as fallback

## `alarm.py` Structure

```
Constants (START_FLAG, END_FLAG, command bytes)
Config class (reads/validates env vars)
TrimlightClient class (TCP socket, handshake, send/recv, all commands)
AlarmStateMachine class (idle/alarmed/restoring, timer, debounce)
WebhookHandler class (BaseHTTPRequestHandler, GET health, POST webhook)
main() (setup, signal handlers, serve_forever)
```

## Verification

1. **Health check**: `curl http://localhost:8080/` → `{"status":"ok","alarm_state":"idle"}`
2. **Test webhook**: `curl -X POST http://localhost:8080/ -H "Content-Type: application/json" -d '{"alarm":{"triggers":[{"key":"animal","device":"X"}]},"timestamp":1}'`
3. **Debounce**: Send two triggers 5s apart, check logs for "Timer reset"
4. **Docker**: `docker compose up -d && docker compose logs -f`
5. **Protocol debugging**: Set `LOG_LEVEL=DEBUG` to see raw bytes sent/received

## UniFi Protect Setup

In the Protect UI Alarm Manager:
1. Create an alarm with Animal detection as the condition
2. Add a Webhook notification action
3. Set the URL to `http://<docker-host-ip>:8080/`
4. Enable Advanced settings → select POST method
