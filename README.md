# UniFi Protect → Trimlight Alarm

Trigger named Trimlight Edge light effects when UniFi Protect detects animals, people, or vehicles. After a configurable timeout the lights automatically restore to their previous state.

```
UniFi Protect ──POST webhook──▸ alarm.py (HTTP :8484) ──HTTPS──▸ Trimlight Cloud API ──▸ Trimlight Edge
```

## Quick Start

```bash
cp .env.example .env
# Edit .env — set your Trimlight API credentials and device ID
docker compose up -d
```

Status page: `http://<host>:8484/`

## Configuration

All configuration is via environment variables (set in `.env`):

| Variable | Default | Description |
|----------|---------|-------------|
| `TRIMLIGHT_CLIENT_ID` | *(required)* | API client ID (contact Trimlight to obtain) |
| `TRIMLIGHT_CLIENT_SECRET` | *(required)* | API client secret |
| `TRIMLIGHT_DEVICE_ID` | *(required)* | Target device ID |
| `TRIMLIGHT_API_URL` | `https://trimlight.ledhue.com/trimlight` | Cloud API base URL |
| `WEBHOOK_PORT` | `8484` | HTTP listener port |
| `ALARM_TIMEOUT` | `30` | Seconds before auto-restore |
| `LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

### Getting API Credentials

1. Contact Trimlight to obtain a `clientId` and `clientSecret`
2. Find your `deviceId` via the device list API:
   ```bash
   # timestamp in milliseconds
   TS=$(date +%s000)
   SIG=$(echo -n "Trimlight|<clientId>|$TS" | openssl dgst -sha256 -hmac "<clientSecret>" -binary | base64)
   curl -X POST https://trimlight.ledhue.com/trimlight/v1/oauth/resources/devices \
     -H "Content-Type: application/json" \
     -H "authorization: $SIG" \
     -H "S-ClientId: <clientId>" \
     -H "S-Timestamp: $TS" \
     -d '{"page": 1}'
   ```

## Available Effects

| Name | Description |
|------|-------------|
| `white` | Solid white — full brightness |
| `red` | Solid red — full brightness |
| `red-strobe` | Red strobe — full brightness |
| `blue` | Solid blue — full brightness |
| `amber` | Solid amber — full brightness |

## UniFi Protect Setup

In Protect → Alarm Manager, create one alarm per detection type and set its webhook to a POST with the corresponding URL:

| Detection type | Webhook URL |
|----------------|-------------|
| Animal / Person | `http://<host>:8484/webhook?effect=white` |
| Vehicle | `http://<host>:8484/webhook?effect=red-strobe` |

For each alarm:
1. Add a **Webhook** notification action
2. Set the URL as shown above
3. Select **POST** method

If no `?effect=` parameter is given the default effect (`white`) is used.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Status web UI |
| `GET` | `/health` | JSON health check |
| `POST` | `/webhook?effect=<name>` | UniFi Protect webhook receiver |
| `POST` | `/test` | Trigger a test alarm (used by the UI) |

## Alarm Behaviour

```
IDLE ──trigger(effect)──▸ ALARMED ──timeout──▸ RESTORING ──done──▸ IDLE
                              ▲
                   same effect: debounce (reset timer)
                   new effect:  override immediately + reset timer
```

1. **Trigger** — Saves current device state (switch mode + effect ID), switches to Manual, previews the named effect
2. **Debounce** — Re-trigger with the same effect resets the countdown without re-sending the command
3. **Override** — Re-trigger with a different effect applies the new effect immediately and resets the countdown
4. **Restore** — Switches back to the saved state:
   - *Timer mode* → Off → Timer (clears preview buffer, resumes schedule)
   - *Manual mode* → re-activates the previously running saved effect
   - *Off* → turns lights back off

## How It Works

Authentication uses HMAC-SHA256:
1. Concatenate `Trimlight|<clientId>|<timestamp-ms>`
2. Compute HMAC-SHA256 keyed with `clientSecret`
3. Base64-encode → `authorization` header

API endpoints used:
- **Notify Update Shadow** — request fresh device state from controller
- **Device Detail** — query current switch mode and running effect
- **Set Switch State** — switch between Off (0), Manual (1), Timer (2)
- **Preview Custom Effect** — apply a named light effect (category 2 for Edge firmware)
- **View Effect** — restore a saved effect by ID

## Testing

**Simulate a detection from the command line:**
```bash
curl -X POST "http://localhost:8484/webhook?effect=white" \
  -H "Content-Type: application/json" \
  -d '{"alarm":{"triggers":[{"key":"animal","device":"camera1"}]}}'

curl -X POST "http://localhost:8484/webhook?effect=red-strobe" \
  -H "Content-Type: application/json" \
  -d '{"alarm":{"triggers":[{"key":"vehicle","device":"camera1"}]}}'
```

**Health check:**
```bash
curl http://localhost:8484/health
```

**Watch logs:**
```bash
docker compose logs -f
```

**Debug API traffic:**
Set `LOG_LEVEL=DEBUG` in `.env` to see full API request/response bodies.

## Running Without Docker

Requires Python 3.6+ (stdlib only, no pip dependencies).

```bash
export TRIMLIGHT_CLIENT_ID=your_id
export TRIMLIGHT_CLIENT_SECRET=your_secret
export TRIMLIGHT_DEVICE_ID=your_device
python3 alarm.py
```
