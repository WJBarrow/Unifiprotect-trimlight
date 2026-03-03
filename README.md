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

The effect to apply is specified as a URL parameter on the webhook: `POST /webhook?effect=<name>`

### Built-in Effects

| Name | Type | Description |
|------|------|-------------|
| `white` | Static | Solid white — full brightness |
| `red` | Static | Solid red — full brightness |
| `red-strobe` | Strobe | Red strobe — full brightness |
| `blue` | Static | Solid blue — full brightness |
| `amber` | Static | Solid amber — full brightness |
| `red-blue-strobe` | Strobe | Alternating red/blue LED pattern strobing together |
| `red-blue-chase` | Chase | Alternating red/blue LEDs chasing forward |
| `police` | Cycle | All LEDs solid red → all LEDs solid blue, alternating every ~1s |
| `intruder` | Saved | Invokes the `IntruderRedBlueStrobe` pattern saved on the device |

### Saved Device Effects

Effects of type **Saved** invoke a pattern you have already created and saved in the Trimlight app. The effect is looked up by name at trigger time — no pixel data is sent from this service. If the name is not found, the error log lists all available effect names on the device.

To add more saved effects, add an entry to the `EFFECTS` dict in `alarm.py`:

```python
"my-effect": {
    "label":      "My Custom Effect",
    "saved_name": "ExactNameInTrimlightApp",  # case-insensitive match
    # UI display only:
    "mode": 0, "speed": 127, "brightness": 255,
    "pixels": _px((0xFF0000, 1)),
},
```

### Cycle Effects

Effects of type **Cycle** loop through a list of frames in a background thread while the alarm is active, sending each frame as a `preview_effect` API call. The minimum cycle time is bounded by the Trimlight cloud API round-trip (~1s per call).

## UniFi Protect Setup

In Protect → Alarm Manager, create one alarm per detection type and configure its webhook as a POST to the corresponding URL:

| Detection type | Webhook URL |
|----------------|-------------|
| Animal / Person | `http://<host>:8484/webhook?effect=white` |
| Vehicle | `http://<host>:8484/webhook?effect=red-strobe` |
| Intruder | `http://<host>:8484/webhook?effect=intruder` |

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
| `GET` | `/webhook` | Connectivity probe (returns 200 — UniFi Protect checks this before sending alarms) |
| `POST` | `/webhook?effect=<name>` | UniFi Protect webhook receiver |
| `POST` | `/test` | Trigger a test alarm (used by the web UI) |

## Alarm Behaviour

```
IDLE ──trigger(effect)──▸ ALARMED ──timeout──▸ RESTORING ──done──▸ IDLE
                              ▲
                   same effect: debounce (reset timer)
                   new effect:  override immediately + reset timer
```

1. **Trigger** — Saves current device state (switch mode + effect ID), switches to Manual, applies the named effect
2. **Debounce** — Re-trigger with the same effect resets the countdown without re-applying the effect
3. **Override** — Re-trigger with a different effect applies the new effect immediately and resets the countdown
4. **Restore** — Switches back to the saved state:
   - *Timer mode* → Off → Timer (clears preview buffer so schedule resumes cleanly)
   - *Manual mode* → re-activates the previously running saved effect
   - *Off* → turns lights back off

The page auto-refreshes every 5 seconds only while an alarm is active.

## How It Works

Authentication uses HMAC-SHA256:
1. Concatenate `Trimlight|<clientId>|<timestamp-ms>`
2. Compute HMAC-SHA256 keyed with `clientSecret`
3. Base64-encode → `authorization` header

API endpoints used:
- **Notify Update Shadow** — request fresh device state from controller
- **Device Detail** — query current switch mode, running effect, and saved effects list
- **Set Switch State** — switch between Off (0), Manual (1), Timer (2)
- **Preview Custom Effect** — apply a custom light effect (Edge firmware requires `category: 2`)
- **View Effect** — activate a saved effect by ID (used for saved-name effects and restore)

## Testing

**Simulate detections from the command line:**
```bash
curl -X POST "http://localhost:8484/webhook?effect=white" \
  -H "Content-Type: application/json" \
  -d '{"alarm":{"triggers":[{"key":"animal","device":"camera1"}]}}'

curl -X POST "http://localhost:8484/webhook?effect=red-strobe" \
  -H "Content-Type: application/json" \
  -d '{"alarm":{"triggers":[{"key":"vehicle","device":"camera1"}]}}'

curl -X POST "http://localhost:8484/webhook?effect=intruder" \
  -H "Content-Type: application/json" \
  -d '{"alarm":{"triggers":[{"key":"person","device":"camera1"}]}}'
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
