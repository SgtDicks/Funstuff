import os, sys, math, json, threading, atexit, shutil, subprocess, time, socket
from flask import Flask, request, jsonify, render_template_string

# Make ../scservo_sdk importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from scservo_sdk.port_handler import PortHandler
from scservo_sdk.sms_sts import sms_sts

# -----------------------------
# SERVO / DRIVE CONFIG
# -----------------------------
DEVICENAME = "/dev/ttyACM0"
BAUDRATE   = 1_000_000
SERVO_IDS  = [1, 2, 3]  # wheel servos

# 4th servo: Z-lift axis for the front-facing camera platform
Z_LIFT_SERVO_ID = 4
# Reverse direction if Z moves the wrong way (+1 / -1)
Z_LIFT_DIR = 1

# Z-lift position range (STS/SMS typically use 0-4095). Tune these for your mechanics.
Z_LIFT_MIN_POS  = 1200
Z_LIFT_MAX_POS  = 3000
Z_LIFT_HOME_POS = 2100
Z_LIFT_STEP_POS = 60
Z_LIFT_SPEED    = 1200   # move speed (servo units)
Z_LIFT_ACC      = 0      # acceleration (if supported)

# If True, the server will command the Z-lift to its saved position on startup.
# Leave False until you confirm your min/max are safe.
Z_LIFT_APPLY_ON_START = False
# Flip per-wheel direction (+1 / -1) if any wheel is reversed
DIR = {1: 1, 2: 1, 3: 1}

# Wheel angles for 3-omni kiwi drive (robot coords: +Y forward, +X right)
ANGLES_DEG = {1: 0.0, 2: 120.0, 3: 240.0}

# Rotation scaling inside kinematics
L = 1.0

# Speed clamp (software) - raise MAX_SPEED if you want
MIN_SPEED = 0
MAX_SPEED = 3000
SPEED_STEP = 100
TURN_RATIO = 0.7
ACC = 0

# -----------------------------
# CAMERA / STREAM CONFIG
# -----------------------------
AUTO_START_USTREAMER = True
VERSION = "V0.2"
USTREAMER_HOST = "0.0.0.0"

CAMERA_SOURCES = [
    {"name": "Back Camera",          "device": "/dev/video0", "port": 8080, "format": "MJPEG", "resolution": "1280x720", "fps": 30},
    {"name": "Front Forward Camera", "device": "/dev/video2", "port": 8081, "format": "MJPEG", "resolution": "1280x720", "fps": 30},
    {"name": "Front Ground Camera",  "device": "/dev/video4", "port": 8082, "format": "MJPEG", "resolution": "1280x720", "fps": 30},
]

# -----------------------------
# STATE + PERSISTENCE
# -----------------------------
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "robot_config.json")

ROBOT_NAME_DEFAULT = (os.environ.get("ROBOT_NAME") or socket.gethostname() or "Athena").strip()

DEFAULT_STATE = {
    "speed": 600,
    "forward_heading_deg": -20.0,     # default trim
    "robot_name": ROBOT_NAME_DEFAULT,
    "z_lift": {
        "id": Z_LIFT_SERVO_ID,
        "pos": Z_LIFT_HOME_POS,
        "min": Z_LIFT_MIN_POS,
        "max": Z_LIFT_MAX_POS,
        "step": Z_LIFT_STEP_POS,
        "speed": Z_LIFT_SPEED,
        "home": Z_LIFT_HOME_POS,
    },
    "show_all_cameras": True,
    "active_camera": 0,
    "cameras": [
        {"name": "Back Camera",          "url": "http://{host}:8080/stream"},
        {"name": "Front Forward Camera", "url": "http://{host}:8081/stream"},
        {"name": "Front Ground Camera",  "url": "http://{host}:8082/stream"},
    ],
}

def load_state():
    s = dict(DEFAULT_STATE)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            disk = json.load(f)
        if isinstance(disk, dict):
            for k in ["speed", "forward_heading_deg", "show_all_cameras", "active_camera", "cameras", "robot_name", "z_lift"]:
                if k in disk:
                    s[k] = disk[k]

        # Deep-merge z_lift so newer defaults survive older config files
        if isinstance(s.get("z_lift"), dict):
            for kk, vv in DEFAULT_STATE["z_lift"].items():
                s["z_lift"].setdefault(kk, vv)

        # Normalize robot_name
        s["robot_name"] = str(s.get("robot_name", ROBOT_NAME_DEFAULT)).strip() or ROBOT_NAME_DEFAULT

    except FileNotFoundError:
        pass
    except Exception as e:
        print("WARNING: failed to load config:", e)
    return s


def save_state(state):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "speed": state["speed"],
                    "forward_heading_deg": state["forward_heading_deg"],
                    "show_all_cameras": state["show_all_cameras"],
                    "active_camera": state["active_camera"],
                    "cameras": state["cameras"],
                    "robot_name": state.get("robot_name", ROBOT_NAME_DEFAULT),
                    "z_lift": state.get("z_lift", DEFAULT_STATE["z_lift"]),
                },
                f,
                indent=2
            )
    except Exception as e:
        print("WARNING: failed to save config:", e)

STATE = load_state()


# -----------------------------
# HARDWARE SETUP
# -----------------------------
port = PortHandler(DEVICENAME)
if not port.openPort():
    raise SystemExit(f"Failed to open port: {DEVICENAME}")
if not port.setBaudRate(BAUDRATE):
    raise SystemExit(f"Failed to set baudrate: {BAUDRATE}")
bus = sms_sts(port)
lock = threading.Lock()

def _call_if_exists(obj, name, *args):
    fn = getattr(obj, name, None)
    if not callable(fn):
        return None
    try:
        return fn(*args)
    except TypeError:
        # argument mismatch; try another signature
        return None

def set_wheel_speed(servo_id: int, speed: int):
    speed = int(max(-MAX_SPEED, min(MAX_SPEED, speed)))
    speed *= int(DIR.get(servo_id, 1))

    for m in ["WriteSpe","writeSpe","WriteSpeed","writeSpeed","WriteSpec","writeSpec","WriteSpd","writeSpd"]:
        r = _call_if_exists(bus, m, servo_id, speed, ACC)
        if r is not None:
            return
    for m in ["WriteSpe","writeSpe","WriteSpeed","writeSpeed","WriteSpec","writeSpec","WriteSpd","writeSpd"]:
        r = _call_if_exists(bus, m, servo_id, speed)
        if r is not None:
            return

    methods = [m for m in dir(bus) if ("spe" in m.lower() or "spd" in m.lower() or "write" in m.lower() or "wheel" in m.lower())]
    raise RuntimeError("No speed-write method found on sms_sts(). Candidates: " + ", ".join(methods))

def set_servo_position(servo_id: int, pos: int, speed: int = None, acc: int = None):
    """Set servo target position (for Z-lift / other positional axes).

    Feetech SMS/STS libraries differ slightly in method names/signatures, so we try a few.
    """
    pos = int(pos)
    speed = int(speed) if speed is not None else int(Z_LIFT_SPEED)
    acc = int(acc) if acc is not None else int(Z_LIFT_ACC)

    # Apply per-axis direction flip (only for the Z-lift servo by default)
    if servo_id == Z_LIFT_SERVO_ID:
        # Mirror around mid-range if you set DIR=-1 (simple inversion)
        if int(Z_LIFT_DIR) == -1:
            mid = (int(Z_LIFT_MIN_POS) + int(Z_LIFT_MAX_POS)) // 2
            pos = mid - (pos - mid)

    # Common method names
    method_sets = [
        # (method_names, args)
        (["WritePosEx","writePosEx","WritePosSpec","writePosSpec"], (servo_id, pos, speed, acc)),
        (["WritePosEx","writePosEx","WritePos","writePos","WritePosition","writePosition"], (servo_id, pos, speed)),
        (["WritePos","writePos","WritePosition","writePosition"], (servo_id, pos)),
    ]

    for names, args in method_sets:
        for m in names:
            r = _call_if_exists(bus, m, *args)
            if r is not None:
                return

    methods = [m for m in dir(bus) if ("pos" in m.lower() and "write" in m.lower())]
    raise RuntimeError("No position-write method found on sms_sts(). Candidates: " + ", ".join(methods))

def zlift_clamp(pos: int) -> int:
    z = STATE.get("z_lift") or DEFAULT_STATE["z_lift"]
    return int(max(int(z.get("min", Z_LIFT_MIN_POS)), min(int(z.get("max", Z_LIFT_MAX_POS)), int(pos))))

def zlift_move_to(pos: int, speed: int = None):
    """Move Z-lift to an absolute position and update STATE."""
    pos = zlift_clamp(pos)
    STATE.setdefault("z_lift", dict(DEFAULT_STATE["z_lift"]))
    STATE["z_lift"]["pos"] = pos
    set_servo_position(Z_LIFT_SERVO_ID, pos, speed=speed or int(STATE["z_lift"].get("speed", Z_LIFT_SPEED)))
    return pos

def zlift_step(delta: int):
    """Move Z-lift by a delta (positive = up)."""
    z = STATE.get("z_lift") or DEFAULT_STATE["z_lift"]
    cur = int(z.get("pos", Z_LIFT_HOME_POS))
    return zlift_move_to(cur + int(delta))

def stop_all():
    for sid in SERVO_IDS:
        try:
            set_wheel_speed(sid, 0)
        except Exception:
            pass

@atexit.register
def _cleanup():
    try:
        stop_all()
    finally:
        try:
            port.closePort()
        except Exception:
            pass

def kinematics(vx: float, vy: float, omega: float, heading_deg: float):
    # 3-omni kiwi/triangle:
    # wi = -sin(theta_i)*vx + cos(theta_i)*vy + L*omega
    wheel = {}
    for sid in SERVO_IDS:
        th = math.radians(ANGLES_DEG[sid] - heading_deg)
        wi = (-math.sin(th) * vx) + (math.cos(th) * vy) + (L * omega)
        wheel[sid] = int(round(wi))
    return wheel

def apply_drive(vx: float, vy: float, omega: float):
    # vx,vy,omega are already in "speed units"
    heading = float(STATE["forward_heading_deg"])
    wheel = kinematics(vx, vy, omega, heading)
    for sid, spd in wheel.items():
        set_wheel_speed(sid, spd)

# -----------------------------
# USTREAMER AUTOSTART
# -----------------------------
STREAM_PROCS = []
STREAM_LOCK = threading.Lock()

def _start_one_stream(src):
    u = shutil.which("ustreamer")
    if not u:
        print("WARNING: ustreamer not found. Install: sudo apt install -y ustreamer")
        return None

    cmd = [
        u,
        "--device", src["device"],
        "--host", USTREAMER_HOST,
        "--port", str(src["port"]),
        "--allow-origin", "*",
    ]
    if src.get("format"):
        cmd += ["--format", src["format"]]
    if src.get("resolution"):
        cmd += ["--resolution", src["resolution"]]
    if src.get("fps"):
        cmd += ["--desired-fps", str(src["fps"])]

    print("Starting ustreamer:", " ".join(cmd))
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def start_streams():
    if not AUTO_START_USTREAMER:
        return
    with STREAM_LOCK:
        alive = [p for p in STREAM_PROCS if p and p.poll() is None]
        if alive:
            return
        STREAM_PROCS.clear()
        for src in CAMERA_SOURCES:
            p = _start_one_stream(src)
            if p:
                STREAM_PROCS.append(p)

def stop_streams():
    with STREAM_LOCK:
        for p in STREAM_PROCS:
            try:
                if p and p.poll() is None:
                    p.terminate()
            except Exception:
                pass
        time.sleep(0.3)
        for p in STREAM_PROCS:
            try:
                if p and p.poll() is None:
                    p.kill()
            except Exception:
                pass
        STREAM_PROCS.clear()

@atexit.register
def _stop_streams_on_exit():
    stop_streams()

# -----------------------------
# WEB APP
# -----------------------------
app = Flask(__name__)

HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Servo Bus Drive</title>
  <style>
    :root{
      --bg0:#050607; --bg1:#070a0d;
      --panel:#0b0f14;
      --line:#ff7a18; --line2:#ff9a3c;
      --green:#7CFF7A;
      --text:#e8e8e8; --muted:#a8a8a8;
      --shadow: 0 10px 30px rgba(0,0,0,.55);
      --rad: 18px; --pad: 14px; --gap: 12px;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      --ui: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
    }
    *{ box-sizing:border-box; }
    html, body{ height:100%; }
    body{
      margin:0; padding: var(--pad);
      font-family: var(--ui); color: var(--text);
      background:
        radial-gradient(1200px 700px at 20% 15%, rgba(255,122,24,.10), transparent 55%),
        radial-gradient(900px 600px at 80% 35%, rgba(124,255,122,.08), transparent 55%),
        linear-gradient(180deg, var(--bg0), var(--bg1));
      overflow-x:hidden;
    }
    body::before{
      content:""; position:fixed; inset:0; pointer-events:none;
      background:
        repeating-linear-gradient(180deg, rgba(255,255,255,.030) 0px, rgba(255,255,255,.010) 1px, rgba(0,0,0,0) 3px),
        repeating-linear-gradient(90deg, rgba(255,122,24,.030) 0px, rgba(0,0,0,0) 120px);
      mix-blend-mode: overlay; opacity:.22;
    }

    .topbar{ display:flex; align-items:flex-end; justify-content:space-between; gap: 10px; margin: 4px 0 12px 0; }
    .brand{ display:flex; flex-direction:column; gap:6px; line-height:1; }
    .brand .name{
      font-family: var(--mono); letter-spacing:.18em; font-weight:900;
      color: var(--green); font-size: 26px; text-transform:uppercase;
      text-shadow: 0 0 12px rgba(124,255,122,.25);
    }
    .brand .sub{ font-family: var(--mono); color: var(--muted); font-size: 12px; letter-spacing:.14em; text-transform: uppercase; }

    .statuspill{ display:flex; gap:10px; flex-wrap:wrap; justify-content:flex-end; font-family: var(--mono); font-size: 12px;
      letter-spacing:.10em; text-transform: uppercase; }
    .pill{
      border: 1px solid rgba(255,122,24,.7); border-radius: 999px;
      padding: 8px 12px; background: rgba(10,13,16,.55);
      box-shadow: 0 0 18px rgba(255,122,24,.08);
      color: var(--line2); white-space:nowrap;
    }
    .pill.green{ border-color: rgba(124,255,122,.55); color: var(--green); box-shadow: 0 0 18px rgba(124,255,122,.10); }

    .wrap{ display:grid; grid-template-columns: 440px 1fr; gap: var(--gap); align-items:start; }
    @media (max-width: 980px){ .wrap{ grid-template-columns: 1fr; } }

    .card{
      position:relative;
      background: linear-gradient(180deg, rgba(11,15,20,.94), rgba(8,10,12,.92));
      border-radius: var(--rad);
      padding: var(--pad);
      border: 1px solid rgba(255,122,24,.65);
      box-shadow: var(--shadow);
      overflow:hidden;
    }
    .card::before{ content:""; position:absolute; inset:10px; border-radius: calc(var(--rad) - 8px);
      border: 1px solid rgba(255,154,60,.18); pointer-events:none; }
    .card::after{ content:""; position:absolute; inset:0;
      background:
        radial-gradient(600px 300px at 10% 0%, rgba(255,122,24,.12), transparent 60%),
        radial-gradient(650px 350px at 90% 30%, rgba(124,255,122,.08), transparent 60%);
      pointer-events:none; opacity:.35; }
    .card > *{ position:relative; z-index:1; }

    hr{ border:none; border-top: 1px solid rgba(255,122,24,.25); margin: 12px 0; }
    .row{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
    .small{ color: var(--muted); font-size: 0.95em; font-family: var(--mono); letter-spacing:.02em; }
    .mono{ font-family: var(--mono); white-space: pre-wrap; font-size: 0.95em; color:#d8d8d8; }

    button, input, select{
      font-size: 17px; padding: 10px 12px; border-radius: 14px;
      border: 1px solid rgba(255,122,24,.6);
      background: rgba(10,13,16,.60);
      color: var(--text); outline:none;
    }
    button{ cursor:pointer; font-family: var(--mono); letter-spacing:.08em; text-transform: uppercase; }
    button:hover{ box-shadow: 0 0 18px rgba(255,122,24,.20); border-color: rgba(255,154,60,.9); }
    button:active{ transform: translateY(1px); box-shadow: 0 0 26px rgba(255,122,24,.28); }
    input::placeholder{ color: rgba(230,230,230,.35); }

    .kbd{
      font-family: var(--mono);
      padding: 4px 10px;
      border: 1px solid rgba(255,122,24,.7);
      border-radius: 999px;
      background: rgba(10,13,16,.55);
      color: var(--line2);
      display:inline-block;
      margin: 2px 6px 2px 0;
      box-shadow: 0 0 14px rgba(255,122,24,.12);
    }

    .btngrid{ display:grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 10px; }
    @media (max-width: 520px){ .btngrid{ grid-template-columns: repeat(2, 1fr); } }
    .btngrid button.stop{ grid-column: span 4; border-color: rgba(124,255,122,.55); color: var(--green); box-shadow: 0 0 18px rgba(124,255,122,.10); }
    @media (max-width: 520px){ .btngrid button.stop{ grid-column: span 2; } }

    /* Dials */
    .dialrow{ display:grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .dial{ border: 1px solid rgba(255,122,24,.45); border-radius: 16px; padding: 10px; background: rgba(5,6,7,.40); }
    canvas{ width: 100%; height: 155px; display:block; }
    .dialmeta{ display:flex; justify-content:space-between; gap:10px; margin-top: 6px;
      font-family: var(--mono); text-transform:uppercase; letter-spacing:.10em; font-size: 12px; color: var(--muted); }
    .dialmeta b{ color: var(--line2); }

    /* Cameras */
    .grid{ display:grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 10px; }
    @media (max-width: 520px){ .grid{ grid-template-columns: 1fr; } }
    .camtitle{ font-family: var(--mono); font-weight: 800; letter-spacing:.10em; text-transform: uppercase; margin-bottom: 8px; color: var(--line2); }
    img.cam{ width:100%; height:auto; border-radius: 16px; border: 1px solid rgba(255,122,24,.65);
      object-fit: contain; background:#000; box-shadow: 0 0 28px rgba(255,122,24,.10); }

    #status{ border: 1px solid rgba(255,122,24,.35); border-radius: 14px; padding: 10px; background: rgba(5,6,7,.55); max-height: 280px; overflow:auto; }
  </style>
</head>
<body>
  <div class="topbar">
    <div class="brand">
      <div class="name" id="robotName">GREEK</div>
      <div class="sub">servo bus control / web hud</div>
    </div>
    <div class="statuspill">
      <div class="pill green" id="ver">V0.2</div>
      <div class="pill" id="srvtime">SERVER TIME: --</div>
    </div>
  </div>

  <div class="wrap">
    <div class="card">
      <div class="dialrow">
        <div class="dial">
          <canvas id="dialSpeed" width="420" height="200"></canvas>
          <div class="dialmeta"><span><b>SPEED</b></span><span id="speedHint">min/max</span></div>
        </div>
        <div class="dial">
          <canvas id="dialHeading" width="420" height="200"></canvas>
          <div class="dialmeta"><span><b>HEADING</b></span><span class="small">deg</span></div>
        </div>
      </div>

      <div style="margin-top:10px">
        <span class="kbd">W</span>Forward
        <span class="kbd">S</span>Back
        <span class="kbd">A</span>Left+Fwd
        <span class="kbd">D</span>Right+Fwd
        <span class="kbd">Q</span>Rotate L
        <span class="kbd">E</span>Rotate R
        <span class="kbd">X</span>Stop
        <span class="kbd">+</span>/<span class="kbd">=</span>Faster
        <span class="kbd">-</span>Slower
      </div>

      <div class="btngrid">
        <button onclick="send('W')">W</button><button onclick="send('S')">S</button>
        <button onclick="send('A')">A</button><button onclick="send('D')">D</button>
        <button onclick="send('Q')">Q</button><button onclick="send('E')">E</button>
        <button onclick="send('+')">+</button><button onclick="send('-')">-</button>
        <button onclick="send('Z+')">Z▲</button><button onclick="send('Z-')">Z▼</button>
        <button onclick="send('Z0')">Z HOME</button><button onclick="send('ZMAX')">Z MAX</button>
        <button class="stop" onclick="send('X')">STOP</button>
      </div>

      <hr>

      <div class="row">
        <div><b style="font-family:var(--mono); letter-spacing:.12em; color:var(--line2);">FORWARD HEADING (deg)</b></div>
        <div class="small">(default -20; editable)</div>
      </div>
      <div class="row" style="margin-top:8px">
        <input id="heading" type="number" step="1" style="width:140px">
        <input id="headingSlider" type="range" min="-180" max="180" step="1" style="flex:1;">
        <button onclick="saveHeading()">Save</button>
      </div>

      <hr>

      <div class="row">
        <label class="row" style="gap:8px;">
          <input id="showAll" type="checkbox" onchange="toggleShowAll()">
          <b style="font-family:var(--mono); letter-spacing:.10em;">SHOW ALL CAMERAS</b>
        </label>
        <button onclick="refresh()">Refresh</button>
      </div>

      <div id="singleCamControls" class="row" style="margin-top:10px">
        <select id="camSelect" onchange="selectCam()" style="flex:1;"></select>
      </div>

      <div style="margin-top:10px">
        <div class="small">Add camera stream URL (ustreamer):</div>
        <div class="row" style="margin-top:8px">
          <input id="camName" placeholder="Name (optional)" style="width:220px">
          <input id="camUrl" placeholder="http://{host}:8083/stream" style="flex:1;">
          <button onclick="addCam()">Add</button>
        </div>
        <div class="row" style="margin-top:10px">
          <button onclick="removeActiveCam()">Remove active camera</button>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="row">
        <div><b style="font-family:var(--mono); letter-spacing:.12em; color:var(--line2); text-transform:uppercase;">Video</b></div>
        <div class="small">({host} auto-fills from browser)</div>
      </div>

      <div id="camArea" style="margin-top:12px;"></div>

      <div style="margin-top:12px"><b style="font-family:var(--mono); letter-spacing:.12em; color:var(--line2); text-transform:uppercase;">Status</b></div>
      <div class="mono" id="status">loadingâ€¦</div>
    </div>
  </div>

<script>
let headingDirty=false;
let lastCamSig=null;

function drawDial(canvas, value, min, max, label, sublabel){
  const ctx = canvas.getContext("2d");
  const w=canvas.width, h=canvas.height;
  ctx.clearRect(0,0,w,h);
  const cx=w*0.5, cy=h*0.55;
  const rOuter=Math.min(w,h)*0.38, rInner=rOuter*0.78;

  const grad=ctx.createRadialGradient(cx,cy,rInner*0.2,cx,cy,rOuter*1.2);
  grad.addColorStop(0,"rgba(255,154,60,0.18)");
  grad.addColorStop(1,"rgba(0,0,0,0)");
  ctx.fillStyle=grad; ctx.beginPath(); ctx.arc(cx,cy,rOuter*1.2,0,Math.PI*2); ctx.fill();

  ctx.strokeStyle="rgba(255,122,24,0.55)"; ctx.lineWidth=Math.max(2,rOuter*0.07);
  ctx.beginPath(); ctx.arc(cx,cy,rOuter,0,Math.PI*2); ctx.stroke();

  ctx.strokeStyle="rgba(255,154,60,0.20)"; ctx.lineWidth=Math.max(1,rOuter*0.04);
  ctx.beginPath(); ctx.arc(cx,cy,rInner,0,Math.PI*2); ctx.stroke();

  ctx.strokeStyle="rgba(255,122,24,0.55)"; ctx.lineWidth=2;
  for(let i=0;i<60;i++){
    const a=(i/60)*Math.PI*2;
    const len=(i%5===0)? rOuter*0.12 : rOuter*0.06;
    const x1=cx+Math.cos(a)*(rInner), y1=cy+Math.sin(a)*(rInner);
    const x2=cx+Math.cos(a)*(rInner+len), y2=cy+Math.sin(a)*(rInner+len);
    ctx.globalAlpha=(i%5===0)?0.75:0.35;
    ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.stroke();
  }
  ctx.globalAlpha=1;

  const sweepStart=-Math.PI*0.75, sweepEnd=Math.PI*0.75;
  const t=(value-min)/(max-min||1);
  const a2=sweepStart+(sweepEnd-sweepStart)*Math.max(0,Math.min(1,t));

  ctx.strokeStyle="rgba(255,154,60,0.95)";
  ctx.lineWidth=Math.max(3,rOuter*0.08); ctx.lineCap="round";
  ctx.beginPath(); ctx.arc(cx,cy,rInner*0.92,sweepStart,a2,false); ctx.stroke();

  ctx.strokeStyle="rgba(255,154,60,0.95)"; ctx.lineWidth=3;
  ctx.beginPath(); ctx.moveTo(cx,cy); ctx.lineTo(cx+Math.cos(a2)*(rInner*0.85), cy+Math.sin(a2)*(rInner*0.85)); ctx.stroke();

  ctx.fillStyle="rgba(255,154,60,0.95)"; ctx.beginPath(); ctx.arc(cx,cy,5,0,Math.PI*2); ctx.fill();

  ctx.font="800 18px ui-monospace, monospace"; ctx.fillStyle="rgba(255,154,60,0.95)";
  ctx.textAlign="center"; ctx.fillText(label, cx, h*0.18);
  ctx.font="700 40px ui-monospace, monospace"; ctx.fillStyle="rgba(230,230,230,0.92)";
  ctx.fillText(String(Math.round(value)), cx, h*0.62);
  ctx.font="700 14px ui-monospace, monospace"; ctx.fillStyle="rgba(168,168,168,0.92)";
  ctx.fillText(sublabel, cx, h*0.78);
}

function hostify(url){ return (url||"").replaceAll("{host}", location.hostname); }
async function api(path, body){
  const r=await fetch(path,{method:body?"POST":"GET",headers:body?{"Content-Type":"application/json"}:undefined,body:body?JSON.stringify(body):undefined});
  if(!r.ok) throw new Error(await r.text());
  return await r.json();
}

function renderCams(state){
  const camArea=document.getElementById("camArea");
  camArea.innerHTML="";
  const cams=state.cameras||[];
  const showAll=!!state.show_all_cameras;
  if(cams.length===0){ camArea.innerHTML="<div class='small'>No cameras configured.</div>"; return; }

  if(!showAll){
    const idx=Math.max(0,Math.min(cams.length-1,state.active_camera||0));
    const c=cams[idx];
    camArea.innerHTML = `
      <div class="camtitle">${c.name || ("Camera "+(idx+1))}</div>
      <img class="cam" src="${hostify(c.url)}" alt="camera">
      <div class="small" style="margin-top:8px;">${hostify(c.url)}</div>`;
  } else {
    const grid=document.createElement("div"); grid.className="grid";
    cams.forEach((c,i)=>{
      const cell=document.createElement("div");
      cell.innerHTML=`<div class="camtitle">${c.name || ("Camera "+(i+1))}</div><img class="cam" src="${hostify(c.url)}" alt="camera">`;
      grid.appendChild(cell);
    });
    camArea.appendChild(grid);
  }
}

async function refresh(){
  const j=await api("/state");
  document.getElementById("ver").textContent=j.version||"V0.2";
  const rn=(j.robot_name||"Robot").toString().trim();
  document.getElementById("robotName").textContent=(rn?rn:"Robot").toUpperCase();
  document.getElementById("srvtime").textContent="SERVER TIME: "+(j.server_time||"--");
  document.getElementById("speedHint").textContent=`MIN ${j.limits.min_speed} / MAX ${j.limits.max_speed}`;

  const ae=document.activeElement && document.activeElement.id;
  if(!headingDirty && ae!=="heading" && ae!=="headingSlider"){
    document.getElementById("heading").value=j.forward_heading_deg;
    document.getElementById("headingSlider").value=j.forward_heading_deg;
  }

  drawDial(document.getElementById("dialSpeed"), j.speed||0, j.limits.min_speed, j.limits.max_speed, "SPEED", "units");
  drawDial(document.getElementById("dialHeading"), j.forward_heading_deg||0, -180, 180, "HEADING", "deg");

  document.getElementById("showAll").checked=!!j.show_all_cameras;

  const sel=document.getElementById("camSelect"); sel.innerHTML="";
  (j.cameras||[]).forEach((c,i)=>{
    const opt=document.createElement("option");
    opt.value=String(i);
    opt.textContent=`${i}: ${(c.name||("Camera "+(i+1)))} â€” ${hostify(c.url)}`;
    if(i===j.active_camera) opt.selected=true;
    sel.appendChild(opt);
  });
  document.getElementById("singleCamControls").style.display = j.show_all_cameras ? "none" : "flex";

  const camSig=JSON.stringify({cameras:j.cameras, active:j.active_camera, show:j.show_all_cameras});
  if(camSig!==lastCamSig){ renderCams(j); lastCamSig=camSig; }

  document.getElementById("status").textContent=JSON.stringify(j,null,2);
}

async function send(cmd){ await api("/cmd",{cmd}); await refresh(); }
async function saveHeading(){
  const v=parseFloat(document.getElementById("heading").value||"0");
  await api("/config",{forward_heading_deg:v});
  headingDirty=false; await refresh();
}

document.getElementById("headingSlider").addEventListener("input",(e)=>{
  const v=parseFloat(e.target.value); document.getElementById("heading").value=v; headingDirty=true;
});
document.getElementById("heading").addEventListener("input",()=>{ headingDirty=true; });

async function selectCam(){ const idx=parseInt(document.getElementById("camSelect").value,10); await api("/config",{active_camera:idx}); await refresh(); }
async function toggleShowAll(){ const v=document.getElementById("showAll").checked; await api("/config",{show_all_cameras:v}); await refresh(); }
async function addCam(){
  const name=document.getElementById("camName").value.trim();
  const url=document.getElementById("camUrl").value.trim();
  if(!url) return;
  await api("/config",{add_camera:{name,url}});
  document.getElementById("camName").value=""; document.getElementById("camUrl").value="";
  await refresh();
}
async function removeActiveCam(){ await api("/config",{remove_active_camera:true}); await refresh(); }

function keyToCmd(k){
  if(k==='w'||k==='W') return 'W';
  if(k==='s'||k==='S') return 'S';
  if(k==='a'||k==='A') return 'A';
  if(k==='d'||k==='D') return 'D';
  if(k==='q'||k==='Q') return 'Q';
  if(k==='e'||k==='E') return 'E';
  if(k==='x'||k==='X') return 'X';
  if(k==='+'||k==='=') return '+';
  if(k==='-') return '-';
  if(k==='r'||k==='R') return 'Z+';
  if(k==='f'||k==='F') return 'Z-';
  if(k==='g'||k==='G') return 'Z0';
  return null;
}
const held=new Set();
window.addEventListener("keydown", async (e)=>{
  const cmd=keyToCmd(e.key); if(!cmd) return;
  if(["W","A","S","D","Q","E"].includes(cmd)){ if(held.has(cmd)) return; held.add(cmd); }
  e.preventDefault(); await send(cmd);
});
window.addEventListener("keyup", async (e)=>{
  const cmd=keyToCmd(e.key); if(!cmd) return;
  if(["W","A","S","D","Q","E"].includes(cmd)){ held.delete(cmd); e.preventDefault(); await send("X"); }
});

refresh();
setInterval(refresh, 1200);
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(HTML)

@app.get("/state")
def state():
    with lock:
        return jsonify({
        "version": VERSION,
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            **STATE,
            "ids": SERVO_IDS,
            "angles_deg": ANGLES_DEG,
            "dir": DIR,
            "limits": {"min_speed": MIN_SPEED, "max_speed": MAX_SPEED},
        })

@app.post("/config")
def config():
    data = request.get_json(force=True, silent=True) or {}
    with lock:
        if "forward_heading_deg" in data:
            STATE["forward_heading_deg"] = float(data["forward_heading_deg"])

        if "active_camera" in data:
            idx = int(data["active_camera"])
            if 0 <= idx < len(STATE["cameras"]):
                STATE["active_camera"] = idx

        if "show_all_cameras" in data:
            STATE["show_all_cameras"] = bool(data["show_all_cameras"])

        if "add_camera" in data:
            cam = data["add_camera"] or {}
            name = str(cam.get("name") or "").strip() or f"Cam {len(STATE['cameras']) + 1}"
            url = str(cam.get("url") or "").strip()
            if url:
                STATE["cameras"].append({"name": name, "url": url})
                STATE["active_camera"] = len(STATE["cameras"]) - 1

        if data.get("remove_active_camera"):
            if STATE["cameras"]:
                idx = max(0, min(len(STATE["cameras"]) - 1, int(STATE["active_camera"])))
                STATE["cameras"].pop(idx)
                STATE["active_camera"] = max(0, min(len(STATE["cameras"]) - 1, idx))

        save_state(STATE)

    return jsonify(ok=True)

@app.post("/cmd")
def cmd():
    data = request.get_json(force=True, silent=True) or {}
    c = (data.get("cmd") or "").strip()

    with lock:
        if c in ["+", "="]:
            STATE["speed"] = min(MAX_SPEED, STATE["speed"] + SPEED_STEP)
            save_state(STATE)
            return jsonify(ok=True)
        if c == "-":
            STATE["speed"] = max(MIN_SPEED, STATE["speed"] - SPEED_STEP)
            save_state(STATE)
            return jsonify(ok=True)


        # Z-lift commands (camera platform)
        # Supported forms: "Z+", "Z-", "Z0"/"ZHOME", "ZMAX", "ZMIN"
        if c in ["Z+", "ZU", "ZUP"]:
            zlift_step(+int(STATE.get("z_lift", DEFAULT_STATE["z_lift"]).get("step", Z_LIFT_STEP_POS)))
            save_state(STATE)
            return jsonify(ok=True, z_lift=STATE.get("z_lift"))
        if c in ["Z-", "ZD", "ZDN"]:
            zlift_step(-int(STATE.get("z_lift", DEFAULT_STATE["z_lift"]).get("step", Z_LIFT_STEP_POS)))
            save_state(STATE)
            return jsonify(ok=True, z_lift=STATE.get("z_lift"))
        if c in ["Z0", "ZHOME"]:
            zlift_move_to(int(STATE.get("z_lift", DEFAULT_STATE["z_lift"]).get("home", Z_LIFT_HOME_POS)) if isinstance(STATE.get("z_lift", {}), dict) and "home" in STATE.get("z_lift", {}) else int(Z_LIFT_HOME_POS))
            save_state(STATE)
            return jsonify(ok=True, z_lift=STATE.get("z_lift"))
        if c == "ZMAX":
            zlift_move_to(int(STATE.get("z_lift", DEFAULT_STATE["z_lift"]).get("max", Z_LIFT_MAX_POS)))
            save_state(STATE)
            return jsonify(ok=True, z_lift=STATE.get("z_lift"))
        if c == "ZMIN":
            zlift_move_to(int(STATE.get("z_lift", DEFAULT_STATE["z_lift"]).get("min", Z_LIFT_MIN_POS)))
            save_state(STATE)
            return jsonify(ok=True, z_lift=STATE.get("z_lift"))

        base = int(STATE["speed"])
        turn = int(round(base * TURN_RATIO))

        vx = vy = omega = 0

        if c == "W":
            vy = +base
        elif c == "S":
            vy = -base
        elif c == "A":
            vy = +base
            omega = +turn
        elif c == "D":
            vy = +base
            omega = -turn
        elif c == "Q":
            omega = +turn
        elif c == "E":
            omega = -turn
        elif c == "X":
            vx = vy = omega = 0
        else:
            return jsonify(ok=False, error="unknown cmd"), 400

        apply_drive(vx, vy, omega)

    return jsonify(ok=True, cmd=c)

@app.post("/zlift")
def zlift():
    """Control the Z-lift (camera platform).

    JSON body supports:
      - {"pos": <int>}      : absolute target position
      - {"delta": <int>}    : relative move (positive = up)
      - {"preset": "home"|"min"|"max"}
      - {"speed": <int>}    : optional override
    """
    data = request.get_json(force=True, silent=True) or {}
    with lock:
        STATE.setdefault("z_lift", dict(DEFAULT_STATE["z_lift"]))
        z = STATE["z_lift"]

        speed = data.get("speed", None)
        target = None

        if "pos" in data:
            target = int(data["pos"])
        elif "delta" in data:
            target = int(z.get("pos", Z_LIFT_HOME_POS)) + int(data["delta"])
        elif "preset" in data:
            p = str(data["preset"]).lower().strip()
            if p == "home":
                target = int(Z_LIFT_HOME_POS)
            elif p == "min":
                target = int(z.get("min", Z_LIFT_MIN_POS))
            elif p == "max":
                target = int(z.get("max", Z_LIFT_MAX_POS))

        if target is None:
            return jsonify(ok=False, error="provide pos/delta/preset"), 400

        target = zlift_move_to(target, speed=int(speed) if speed is not None else None)
        save_state(STATE)
        return jsonify(ok=True, z_lift=STATE["z_lift"], pos=target)

@app.post("/drive")
def drive():
    """
    Analog drive:
      vx, vy, omega in [-1..1] from gamepad.
      We scale them by current STATE["speed"].
    """
    data = request.get_json(force=True, silent=True) or {}
    try:
        vx = float(data.get("vx", 0.0))
        vy = float(data.get("vy", 0.0))
        omega = float(data.get("omega", 0.0))
    except Exception:
        return jsonify(ok=False, error="bad floats"), 400

    # clamp
    vx = max(-1.0, min(1.0, vx))
    vy = max(-1.0, min(1.0, vy))
    omega = max(-1.0, min(1.0, omega))

    with lock:
        base = float(STATE["speed"])
        # translation scales with speed, rotation uses TURN_RATIO
        vx_u = vx * base
        vy_u = vy * base
        om_u = omega * base * TURN_RATIO

        # if near zero -> stop
        if abs(vx_u) + abs(vy_u) + abs(om_u) < 1.0:
            stop_all()
        else:
            apply_drive(vx_u, vy_u, om_u)

    return jsonify(ok=True)

if __name__ == "__main__":
    start_streams()
    # Optionally move Z-lift to the saved position on startup
    if Z_LIFT_APPLY_ON_START:
        try:
            with lock:
                z = STATE.get('z_lift') or DEFAULT_STATE['z_lift']
                zlift_move_to(int(z.get('pos', Z_LIFT_HOME_POS)))
                save_state(STATE)
        except Exception as e:
            print('WARNING: failed to apply Z-lift on start:', e)

    print("Starting web UI on http://0.0.0.0:5000")
    print("Config file:", CONFIG_PATH)
    app.run(host="0.0.0.0", port=5000, threaded=True)

