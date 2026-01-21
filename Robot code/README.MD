# Pi Robot Server (Pi_Server.py)

A lightweight HTTP server intended to run on a Raspberry Pi to control a small robot and provide a simple web HUD + API that a Steam Deck / desktop client can talk to.

This version includes **a 4th servo for a Z-lift axis** (front-facing camera platform).

---

## What’s new in this update

- Added **Z-lift servo** (default servo ID: **4**)
- Persistent Z-lift settings stored in `robot_config.json`
- Z-lift exposed in `/state`
- New endpoint: `POST /zlift`
- `POST /cmd` supports `Z+`, `Z-`, `Z0`/`ZHOME`, `ZMIN`, `ZMAX`

---

## Quick start

### 1) Install dependencies

```bash
python3 -m pip install --upgrade pip
python3 -m pip install flask requests
```

> If your build uses a hardware servo driver (e.g., PCA9685, pigpio, waveshare bus, etc.), install the relevant library for your hardware as well.

### 2) Run

```bash
python3 Pi_Server.py
```

By default the server listens on:

- `http://0.0.0.0:5000`

### 3) Test

Open in a browser on the same LAN:

- `http://<pi-ip>:5000/`

And verify state:

- `http://<pi-ip>:5000/state`

---

## Configuration

The server saves runtime configuration to:

- `robot_config.json`

Typical keys include:

- `robot_name` – shown in clients (Steam Deck HUD)
- `forward_heading_deg` – heading trim
- `limits` – min/max speed limits (if enabled)
- `z_lift` – Z lift settings (see below)

### Z-lift settings

In the code (top section), you can tune:

- `Z_LIFT_SERVO_ID` (default `4`)
- `Z_LIFT_MIN_POS` / `Z_LIFT_MAX_POS` (safe mechanical limits)
- `Z_LIFT_HOME_POS` (default “home” height)
- `Z_LIFT_STEP_POS` (increment size for button presses)
- `Z_LIFT_DEFAULT_SPEED` (optional, if your servo driver supports speed)

**Safety note:** many lift mechanisms can bind or stall. Start with conservative min/max values and test slowly.

---

## API

### `GET /state`

Returns JSON describing the robot.

Includes:

- `robot_name`
- `speed`
- `forward_heading_deg`
- `cameras` (if configured)
- `z_lift` (position + bounds + step)

Example (simplified):

```json
{
  "robot_name": "Dummy Robot",
  "speed": 1200,
  "forward_heading_deg": -20,
  "z_lift": {
    "pos": 2300,
    "min": 1600,
    "max": 2600,
    "step": 60,
    "home": 2300
  }
}
```

### `POST /cmd`

Used for simple driving commands and quick actions.

Common examples:

```bash
curl -X POST http://<pi-ip>:5000/cmd -H "Content-Type: application/json" -d '{"cmd":"W"}'
curl -X POST http://<pi-ip>:5000/cmd -H "Content-Type: application/json" -d '{"cmd":"X"}'
```

Z-lift commands supported:

- `Z+` – move up by `step`
- `Z-` – move down by `step`
- `Z0` / `ZHOME` – go to `home`
- `ZMIN` – go to min
- `ZMAX` – go to max

### `POST /zlift`

Direct Z-lift control.

**Absolute position:**

```bash
curl -X POST http://<pi-ip>:5000/zlift -H "Content-Type: application/json" -d '{"pos":2300}'
```

**Relative move:**

```bash
curl -X POST http://<pi-ip>:5000/zlift -H "Content-Type: application/json" -d '{"delta":60}'
```

**Preset:**

```bash
curl -X POST http://<pi-ip>:5000/zlift -H "Content-Type: application/json" -d '{"preset":"home"}'
curl -X POST http://<pi-ip>:5000/zlift -H "Content-Type: application/json" -d '{"preset":"min"}'
curl -X POST http://<pi-ip>:5000/zlift -H "Content-Type: application/json" -d '{"preset":"max"}'
```

Optional speed (if supported by your servo layer):

```bash
curl -X POST http://<pi-ip>:5000/zlift -H "Content-Type: application/json" -d '{"pos":2400,"speed":1200}'
```

---

## Steam Deck / Pygame HUD integration

The Steam Deck client typically:

- Scans the LAN for servers responding to `/state`
- Uses `robot_name` from `/state` to label the robot
- Drives via `POST /cmd`
- Can control Z-lift via `POST /cmd` (`Z+`, `Z-`, `Z0`) or `POST /zlift`

Suggested bindings in the client UI:

- Z▲ / Z▼ buttons (step moves)
- Z HOME preset button
- Optional slider (absolute position)

---

## Suggested next features

- **Z-lift slider** in the HUD (with min/max and current position)
- **Servo calibration page** (live min/max/home test with safety lock)
- **Camera self-test** (per-camera fps/last-frame age + reconnect button)
- **Config download/upload** in the web UI
- **Auth token** (simple shared secret) if used outside a trusted LAN

---

## Troubleshooting

- **No servers found by scanner:**  
  Ensure the Pi and client are on the same subnet and port `5000` is reachable.

- **Camera streams show offline:**  
  Confirm your MJPEG endpoints are correct and reachable from the client.

- **Z-lift doesn’t move:**  
  Verify wiring/servo ID, and reduce `Z_LIFT_STEP_POS`. Confirm `Z_LIFT_MIN_POS/MAX_POS` are safe and within your servo driver range.

---

## Files

- `Pi_Server.py` – main server
- `robot_config.json` – saved configuration (auto-created)
- `README.md` – this file
