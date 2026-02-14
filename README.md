# UniFi Protect Animal Alarm → Trimlight Red Alert

Turn your Trimlight Edge lights solid red when UniFi Protect detects an animal. After a configurable timeout, the lights automatically restore to their previous state.

```
UniFi Protect ──POST webhook──▸ alarm.py (HTTP :8080) ──HTTPS──▸ Trimlight Cloud API ──▸ Trimlight Edge Controller
```

## Quick Start

```bash
cp .env.example .env
# Edit .env — set your Trimlight API credentials and device ID
docker compose up -d
```

## Configuration

All configuration is via environment variables (set in `.env`):

| Variable | Default | Description |
|----------|---------|-------------|
| `TRIMLIGHT_CLIENT_ID` | *(required)* | API client ID (contact Trimlight to obtain) |
| `TRIMLIGHT_CLIENT_SECRET` | *(required)* | API client secret |
| `TRIMLIGHT_DEVICE_ID` | *(required)* | Target device ID |
| `TRIMLIGHT_API_URL` | `https://trimlight.ledhue.com/trimlight` | Cloud API base URL |
| `WEBHOOK_PORT` | `8080` | HTTP listener port |
| `ALARM_TIMEOUT` | `30` | Seconds before auto-restore |
| `TRIGGER_KEY` | `animal` | Webhook trigger type to match |
| `LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

### Getting API Credentials

1. Contact Trimlight business to obtain a `clientId` and `clientSecret`
2. Use the device list API to find your `deviceId`:
   ```bash
   # After configuring CLIENT_ID and CLIENT_SECRET, the service logs
   # will show device detail on first trigger. Or query directly:
   curl -X POST https://trimlight.ledhue.com/trimlight/v1/oauth/resources/devices \
     -H "Content-Type: application/json" \
     -H "authorization: <your-access-token>" \
     -H "S-ClientId: <your-client-id>" \
     -H "S-Timestamp: <timestamp-ms>" \
     -d '{"page": 1}'
   ```

## How It Works

### Trimlight Cloud API

The service communicates with your Trimlight Edge controller through the [Trimlight Cloud API](https://trimlight.ledhue.com/trimlight). Authentication uses HMAC-SHA256 signatures:

1. Concatenate: `Trimlight|<clientId>|<timestamp>`
2. Compute HMAC-SHA256 with `clientSecret` as the key
3. Base64-encode the result as the `authorization` header

API endpoints used:
- **Device Detail** (`/v1/oauth/resources/device/get`) — Query current mode and running effect
- **Set Switch State** (`/v1/oauth/resources/device/update`) — Switch between Off (0), Manual (1), Timer (2)
- **Preview Custom Effect** (`/v1/oauth/resources/device/effect/preview`) — Display solid red (static custom effect)
- **View Effect** (`/v1/oauth/resources/device/effect/view`) — Restore a saved effect by ID
- **Notify Update Shadow** (`/v1/oauth/resources/device/notify-update-shadow`) — Request fresh device data

### Alarm State Machine

```
IDLE ──trigger──▸ ALARMED ──timeout──▸ RESTORING ──done──▸ IDLE
                     ▲                                       │
                     └──── trigger (debounce: reset timer) ──┘
```

1. **Trigger** — Webhook arrives with matching trigger key. The service queries the device's current state (switchState + running effect), switches to Manual mode, and previews a solid red static effect.
2. **Debounce** — If another trigger arrives while alarmed, the timeout timer resets without re-sending the red command.
3. **Restore** — After the timeout, the service restores the previous state:
   - **Timer mode** → switches back to Timer (schedule resumes automatically)
   - **Manual mode** → re-activates the previously running saved effect
   - **Off** → turns the lights back off

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/`  | Health check — returns `{"status":"ok","alarm_state":"idle"}` |
| `POST` | `/`  | Webhook receiver — accepts UniFi Protect alarm payloads |

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
