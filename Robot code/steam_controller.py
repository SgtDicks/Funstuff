import socket, ipaddress, time, threading, math
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pygame
from PIL import Image
import io

VERSION = "V0.3-ui5"
DEFAULT_PORT = 5000
DEFAULT_CAM_INDEX = 1
DISCOVERY_TIMEOUT_S = 25

CMD_REPEAT_HZ = 10
CMD_DEADZONE = 0.18
ROTATE_ONLY_THRESH = 0.35

BACK_KEYS = {pygame.K_BACKSPACE, pygame.K_b}

# Touch / gesture tuning
DOUBLE_TAP_S = 0.33
DOUBLE_TAP_DIST_PX = 40
SWIPE_MIN_PX = 90
SWIPE_MAX_OFFAXIS_PX = 70

# Scan spinner speed (seconds per tick)
SPINNER_STEP_S = 0.30  # slower / calmer

# -------------------------
# Network discovery
# -------------------------
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()

def probe(ip, port, timeout=0.35):
    url = f"http://{ip}:{port}/state"
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code != 200:
            return None
        j = r.json()
        if not isinstance(j, dict):
            return None
        if not ("speed" in j or "forward_heading_deg" in j or "cameras" in j):
            return None

        name = str(j.get("robot_name") or "").strip() or "Unnamed Robot"
        ver  = str(j.get("version") or "").strip() or "?"
        st   = str(j.get("server_time") or "").strip() or "--"
        cams = j.get("cameras") if isinstance(j.get("cameras"), list) else []
        return {
            "ip": ip,
            "port": port,
            "base": f"http://{ip}:{port}",
            "robot_name": name,
            "version": ver,
            "server_time": st,
            "cams_count": len(cams),
        }
    except Exception:
        return None

def discover_robots(port=DEFAULT_PORT, stop_event=None):
    local_ip = get_local_ip()
    net = ipaddress.ip_network(local_ip + "/24", strict=False)

    ips = [str(h) for h in net.hosts()]
    ips.sort(key=lambda x: abs(int(ipaddress.ip_address(x)) - int(ipaddress.ip_address(local_ip))))

    stop_event = stop_event or threading.Event()
    found = []

    with ThreadPoolExecutor(max_workers=96) as ex:
        futs = [ex.submit(probe, ip, port) for ip in ips]
        for f in as_completed(futs):
            if stop_event.is_set():
                break
            res = f.result()
            if res:
                found.append(res)

    found.sort(key=lambda d: (d["robot_name"].lower(), d["ip"]))
    return found

# -------------------------
# MJPEG Reader (no OpenCV)
# -------------------------
class MJPEGStream:
    def __init__(self, url):
        self.url = url
        self.frame = None  # PIL Image RGB
        self._stop = False
        self._t = None
        self.last_frame_ts = 0.0
        self.frames = 0
        self.fps = 0.0
        self._fps_last_ts = time.time()
        self._fps_last_frames = 0

    def start(self):
        self._stop = False
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def stop(self):
        self._stop = True

    def _tick_fps(self):
        now = time.time()
        dt = now - self._fps_last_ts
        if dt >= 1.0:
            df = self.frames - self._fps_last_frames
            self.fps = df / dt if dt > 0 else 0.0
            self._fps_last_ts = now
            self._fps_last_frames = self.frames

    def _run(self):
        while not self._stop:
            try:
                with requests.get(self.url, stream=True, timeout=3) as r:
                    r.raise_for_status()
                    buf = b""
                    for chunk in r.iter_content(chunk_size=4096):
                        if self._stop:
                            break
                        if not chunk:
                            continue
                        buf += chunk
                        a = buf.find(b"\xff\xd8")
                        b = buf.find(b"\xff\xd9")
                        if a != -1 and b != -1 and b > a:
                            jpg = buf[a:b+2]
                            buf = buf[b+2:]
                            try:
                                img = Image.open(io.BytesIO(jpg)).convert("RGB")
                                self.frame = img
                                self.last_frame_ts = time.time()
                                self.frames += 1
                                self._tick_fps()
                            except Exception:
                                pass
            except Exception:
                time.sleep(0.5)

# -------------------------
# Theme / Drawing
# -------------------------
ORANGE  = (255, 122, 24)
ORANGE2 = (255, 154, 60)
GREEN   = (124, 255, 122)
TEXT    = (232, 232, 232)
MUTED   = (168, 168, 168)
BG0     = (5, 6, 7)
PANEL   = (11, 15, 20)
RED     = (255, 80, 80)

_BG_CACHE = {}

def _make_background(size):
    w, h = size
    bg = pygame.Surface((w, h)).convert()

    top = (4, 6, 8)
    bot = (2, 3, 4)
    for y in range(h):
        t = y / max(1, (h - 1))
        r = int(top[0] + (bot[0] - top[0]) * t)
        g = int(top[1] + (bot[1] - top[1]) * t)
        b = int(top[2] + (bot[2] - top[2]) * t)
        pygame.draw.line(bg, (r, g, b), (0, y), (w, y))

    grid = pygame.Surface((w, h), pygame.SRCALPHA)
    step = 40
    for x in range(0, w, step):
        pygame.draw.line(grid, (255, 154, 60, 10), (x, 0), (x, h), 1)
    for y in range(0, h, step):
        pygame.draw.line(grid, (255, 154, 60, 8), (0, y), (w, y), 1)

    vig = pygame.Surface((w, h), pygame.SRCALPHA)
    layers = 28
    for i in range(layers):
        alpha = int(6 + (i / layers) * 16)
        rect = pygame.Rect(i, i, w - i*2, h - i*2)
        if rect.width <= 0 or rect.height <= 0:
            break
        pygame.draw.rect(vig, (0, 0, 0, alpha), rect, 2, border_radius=0)

    bg.blit(grid, (0, 0))
    bg.blit(vig, (0, 0))
    return bg

def draw_background(screen):
    size = screen.get_size()
    bg = _BG_CACHE.get(size)
    if bg is None:
        bg = _make_background(size)
        _BG_CACHE[size] = bg
    screen.blit(bg, (0, 0))

def draw_glow_rect(surf, rect, color, radius=16, strength=3):
    for i in range(strength, 0, -1):
        a = int(22 * (i / strength))
        r = rect.inflate(i*10, i*10)
        glow = pygame.Surface((r.width, r.height), pygame.SRCALPHA)
        pygame.draw.rect(glow, (*color, a), glow.get_rect(), 3, border_radius=radius+8)
        surf.blit(glow, r.topleft)

def draw_panel(surf, rect, alpha=220):
    draw_glow_rect(surf, rect, ORANGE2, radius=16, strength=2)

    panel = pygame.Surface((rect.width, rect.height), pygame.SRCALPHA)
    panel.fill((*PANEL, alpha))

    hi = pygame.Surface((rect.width, rect.height), pygame.SRCALPHA)
    pygame.draw.rect(hi, (255, 255, 255, 16), pygame.Rect(0, 0, rect.width, int(rect.height*0.20)), 0, border_radius=16)
    panel.blit(hi, (0, 0))

    pygame.draw.rect(panel, (*ORANGE, 140), panel.get_rect(), 2, border_radius=16)
    pygame.draw.rect(panel, (*ORANGE2, 45), panel.get_rect().inflate(-10, -10), 1, border_radius=14)

    sh = pygame.Surface((rect.width, rect.height), pygame.SRCALPHA)
    pygame.draw.rect(sh, (0, 0, 0, 26), pygame.Rect(0, int(rect.height*0.80), rect.width, int(rect.height*0.20)), 0, border_radius=16)
    panel.blit(sh, (0, 0))

    surf.blit(panel, rect.topleft)

def draw_pill(surf, x, y, text, font, fg, border=ORANGE2, fill_alpha=140):
    t = font.render(text, True, fg)
    pad = 10
    r = pygame.Rect(x, y, t.get_width() + pad*2, t.get_height() + 8)

    shadow = pygame.Surface((r.width, r.height), pygame.SRCALPHA)
    pygame.draw.rect(shadow, (0, 0, 0, 90), shadow.get_rect(), 0, border_radius=18)
    surf.blit(shadow, (r.left + 2, r.top + 2))

    p = pygame.Surface((r.width, r.height), pygame.SRCALPHA)
    pygame.draw.rect(p, (*border, 90), p.get_rect(), 2, border_radius=18)
    pygame.draw.rect(p, (0, 0, 0, fill_alpha), p.get_rect().inflate(-2, -2), 0, border_radius=16)
    pygame.draw.rect(p, (255, 255, 255, 18), pygame.Rect(3, 3, r.width-6, max(8, int(r.height*0.33))), 0, border_radius=16)

    p.blit(t, (pad, 4))
    surf.blit(p, r.topleft)
    return r

def draw_glow_circle(surf, center, radius, color, strength=2):
    for i in range(strength, 0, -1):
        a = int(18 * (i / strength))
        pygame.draw.circle(surf, (*color, a), center, radius + i*8, 3)

def render_shadow_text(font, text, fg, shadow=(0,0,0), shadow_alpha=150, offset=(2,2)):
    """Return a surface with a slight drop shadow for readability."""
    base = font.render(text, True, fg)
    sh = font.render(text, True, shadow)
    out = pygame.Surface((base.get_width() + offset[0], base.get_height() + offset[1]), pygame.SRCALPHA)
    sh.set_alpha(shadow_alpha)
    out.blit(sh, offset)
    out.blit(base, (0,0))
    return out

def draw_dial(surf, center, radius, value, vmin, vmax, label, sublabel, font_small, font_big):
    cx, cy = center
    thick = max(4, int(radius * 0.07))
    thick2 = max(2, int(radius * 0.03))
    tick_w = max(1, int(radius * 0.015))

    draw_glow_circle(surf, center, radius, ORANGE2, strength=2)

    pygame.draw.circle(surf, (*ORANGE, 140), center, radius, thick)
    pygame.draw.circle(surf, (*ORANGE2, 70), center, int(radius*0.80), thick2)

    for i in range(60):
        ang = (i/60.0) * (2*math.pi)
        inner = radius*0.80
        ln = radius*0.12 if i % 5 == 0 else radius*0.06
        x1 = cx + int(inner * math.cos(ang))
        y1 = cy + int(inner * math.sin(ang))
        x2 = cx + int((inner + ln) * math.cos(ang))
        y2 = cy + int((inner + ln) * math.sin(ang))
        col = (*ORANGE, 190) if i % 5 == 0 else (*ORANGE, 95)
        pygame.draw.line(surf, col, (x1, y1), (x2, y2), tick_w)

    sweep_start = -2.35619449
    sweep_end   =  2.35619449
    t = 0.0 if vmax == vmin else max(0.0, min(1.0, (value - vmin) / (vmax - vmin)))
    ang = sweep_start + (sweep_end - sweep_start) * t

    arc_rect = pygame.Rect(0, 0, int(radius*1.66), int(radius*1.66))
    arc_rect.center = center
    pygame.draw.arc(surf, (*ORANGE2, 210), arc_rect, sweep_start, ang, max(3, int(radius*0.06)))

    center_disc = pygame.Surface((radius*2, radius*2), pygame.SRCALPHA)
    pygame.draw.circle(center_disc, (0, 0, 0, 110), (radius, radius), int(radius*0.62))
    pygame.draw.circle(center_disc, (255, 255, 255, 12), (radius, radius - int(radius*0.22)), int(radius*0.42))
    surf.blit(center_disc, (cx - radius, cy - radius))

    px = cx + int((radius * 0.70) * math.cos(ang))
    py = cy + int((radius * 0.70) * math.sin(ang))
    pygame.draw.line(surf, ORANGE2, center, (px, py), max(2, int(radius*0.03)))
    pygame.draw.circle(surf, ORANGE2, center, max(5, int(radius*0.05)))

    # ---- FIX: better text placement inside the dial
    lab = font_small.render(label, True, ORANGE2)
    surf.blit(lab, (cx - lab.get_width()//2, cy - int(radius*0.83) - lab.get_height()//2))

    val_surf = render_shadow_text(font_big, str(int(round(value))), TEXT)
    # nudge value slightly down so it feels centered even with the label above
    surf.blit(val_surf, (cx - val_surf.get_width()//2, cy - val_surf.get_height()//2 + int(radius*0.05)))

    sub = font_small.render(sublabel, True, MUTED)
    surf.blit(sub, (cx - sub.get_width()//2, cy + int(radius*0.62) - sub.get_height()//2))

def draw_kv_list(screen, rect, rows, font_label, font_value):
    x = rect.left
    y = rect.top
    line_h = max(font_value.get_height(), font_label.get_height()) + 6
    for (k, v, c) in rows:
        lab = font_label.render(k, True, MUTED)
        val = font_value.render(v, True, c if c else TEXT)
        screen.blit(lab, (x, y))
        screen.blit(val, (x + 160, y))
        y += line_h
        if y > rect.bottom - line_h:
            break

def draw_wrapped_text(screen, text, font, color, rect, line_spacing=2):
    """
    Word-wrap within rect width, and clip to rect so it never overlaps other panels.
    Returns the y after drawing.
    """
    old_clip = screen.get_clip()
    screen.set_clip(rect)

    words = text.split(" ")
    lines = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        if font.size(test)[0] <= rect.width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)

    y = rect.top
    for line in lines:
        s = font.render(line, True, color)
        if y + s.get_height() > rect.bottom:
            break
        screen.blit(s, (rect.left, y))
        y += s.get_height() + line_spacing

    screen.set_clip(old_clip)
    return y

def pil_to_surface(img: Image.Image):
    data = img.tobytes()
    return pygame.image.fromstring(data, img.size, img.mode)

def fit_or_fill(iw, ih, tw, th, mode="fit"):
    if iw <= 0 or ih <= 0:
        return 1.0
    return max(tw/iw, th/ih) if mode == "fill" else min(tw/iw, th/ih)

# -------------------------
# Robot API
# -------------------------
def robot_get_state(base, timeout=1.0):
    return requests.get(base + "/state", timeout=timeout).json()

def robot_cmd(base, c):
    try:
        requests.post(base + "/cmd", json={"cmd": c}, timeout=1)
    except Exception:
        pass

def robot_set_heading(base, deg):
    try:
        requests.post(base + "/config", json={"forward_heading_deg": float(deg)}, timeout=1)
    except Exception:
        pass

# -------------------------
# Input helpers
# -------------------------
def dz(x, deadzone=CMD_DEADZONE):
    return 0.0 if abs(x) < deadzone else x

def clamp(x, a, b):
    return max(a, min(b, x))

def dist2(a, b):
    dx = a[0]-b[0]
    dy = a[1]-b[1]
    return dx*dx + dy*dy

TOUCH_AVAILABLE = hasattr(pygame, "FINGERDOWN")

def event_pos_px(e, screen):
    if e.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP, pygame.MOUSEMOTION):
        return e.pos
    if TOUCH_AVAILABLE and e.type in (pygame.FINGERDOWN, pygame.FINGERUP, pygame.FINGERMOTION):
        w, h = screen.get_size()
        return (int(e.x * w), int(e.y * h))
    return None

# -------------------------
# Server Browser UI
# -------------------------
def server_browser(screen, title_font, mono, small, joystick):
    scan_stop = threading.Event()
    scan_data = {"done": False, "found": [], "err": None, "started": time.time()}

    def scan_worker():
        try:
            scan_data["found"] = discover_robots(DEFAULT_PORT, stop_event=scan_stop)
        except Exception as e:
            scan_data["err"] = str(e)
        finally:
            scan_data["done"] = True

    threading.Thread(target=scan_worker, daemon=True).start()

    spinner = ["|", "/", "-", "\\"]
    sel = 0
    clock = pygame.time.Clock()

    while True:
        clock.tick(60)
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                scan_stop.set()
                return None

            if e.type == pygame.KEYDOWN:
                if e.key == pygame.K_ESCAPE:
                    scan_stop.set()
                    return None
                if e.key in (pygame.K_DOWN, pygame.K_s):
                    if scan_data["found"]:
                        sel = (sel + 1) % len(scan_data["found"])
                if e.key in (pygame.K_UP, pygame.K_w):
                    if scan_data["found"]:
                        sel = (sel - 1) % len(scan_data["found"])
                if e.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                    if scan_data["found"]:
                        scan_stop.set()
                        return scan_data["found"][sel]
                if e.key == pygame.K_r:
                    scan_stop.set()
                    return "__RESCAN__"

            if e.type == pygame.JOYBUTTONDOWN:
                b = e.button
                if b == 0 and scan_data["found"]:
                    scan_stop.set()
                    return scan_data["found"][sel]
                if b == 1:
                    scan_stop.set()
                    return None
                if b == 3:
                    scan_stop.set()
                    return "__RESCAN__"

            if e.type == pygame.JOYHATMOTION:
                hx, hy = e.value
                if hy == -1 and scan_data["found"]:
                    sel = (sel + 1) % len(scan_data["found"])
                if hy == 1 and scan_data["found"]:
                    sel = (sel - 1) % len(scan_data["found"])

        if (time.time() - scan_data["started"]) > DISCOVERY_TIMEOUT_S and not scan_data["done"]:
            scan_stop.set()
            scan_data["done"] = True

        w, h = screen.get_size()
        pad = 14
        draw_background(screen)

        header = title_font.render("SERVER BROWSER", True, GREEN)
        screen.blit(header, (pad, pad))
        screen.blit(small.render("Select a robot on your LAN", True, MUTED), (pad, pad + 44))

        found_count = len(scan_data["found"])
        status = "SCANNING…" if not scan_data["done"] else ("DONE" if found_count else "NO ROBOTS FOUND")

        spin_idx = int((time.time() - scan_data["started"]) / SPINNER_STEP_S) % len(spinner)
        status_line = f"{status}  {spinner[spin_idx]}   Found: {found_count}   (Enter/A=Connect  R/Y=Rescan  Esc/B=Quit)"
        draw_pill(screen, pad, pad + 72, status_line, mono, ORANGE2)

        list_rect = pygame.Rect(pad, pad + 118, w - pad*2, h - (pad + 118) - pad)
        draw_panel(screen, list_rect)

        inner = list_rect.inflate(-18, -18)
        row_h = 64
        max_rows = max(1, inner.height // row_h)

        if found_count == 0:
            msg = mono.render("No robots discovered yet…", True, MUTED if not scan_data["done"] else ORANGE2)
            screen.blit(msg, (inner.left + 10, inner.top + 10))
            if scan_data["err"]:
                err = mono.render(f"Scan error: {scan_data['err']}", True, RED)
                screen.blit(err, (inner.left + 10, inner.top + 36))
        else:
            sel = max(0, min(found_count - 1, sel))
            start = 0
            if found_count > max_rows:
                start = max(0, min(sel - max_rows // 2, found_count - max_rows))
            end = min(found_count, start + max_rows)

            y = inner.top
            for i in range(start, end):
                item = scan_data["found"][i]
                r = pygame.Rect(inner.left, y, inner.width, row_h).inflate(-6, -6)

                if i == sel:
                    draw_glow_rect(screen, r, ORANGE2, radius=14, strength=1)
                    pygame.draw.rect(screen, (*ORANGE, 170), r, 3, border_radius=14)
                else:
                    pygame.draw.rect(screen, (*ORANGE2, 80), r, 1, border_radius=14)

                nm = item["robot_name"]
                ip = item["ip"]
                ver = item.get("version", "?")
                cams = item.get("cams_count", 0)

                t1 = title_font.render(nm, True, TEXT if i == sel else MUTED)
                screen.blit(t1, (r.left + 14, r.top + 8))

                t2 = mono.render(f"{ip}:{DEFAULT_PORT}   |   {ver}   |   cams:{cams}", True, ORANGE2 if i == sel else MUTED)
                screen.blit(t2, (r.left + 16, r.top + 38))

                y += row_h

        pygame.display.flip()

# -------------------------
# Utility: stop streams
# -------------------------
def stop_streams(cam_streams):
    for s in cam_streams:
        try:
            s.stop()
        except Exception:
            pass

# -------------------------
# Main app
# -------------------------
def main():
    pygame.init()
    pygame.joystick.init()

    def make_font(size, bold=False):
        f = pygame.font.Font(None, size)
        f.set_bold(bool(bold))
        return f

    mono  = make_font(18, bold=True)
    mono2 = make_font(16, bold=True)
    small = make_font(16, bold=False)
    big   = make_font(38, bold=True)
    title = make_font(34, bold=True)

    try:
        flags = pygame.FULLSCREEN | pygame.SCALED
        screen = pygame.display.set_mode((1280, 800), flags)
    except Exception:
        screen = pygame.display.set_mode((1280, 800))

    joystick = None
    if pygame.joystick.get_count() > 0:
        joystick = pygame.joystick.Joystick(0)
        joystick.init()

    while True:
        selected = None
        while selected is None:
            sel = server_browser(screen, title, mono, small, joystick)
            if sel == "__RESCAN__":
                continue
            selected = sel

        if not selected:
            pygame.quit()
            return

        back_to_browser = run_hud_session(screen, joystick, selected, mono, mono2, small, big, title)
        if not back_to_browser:
            pygame.quit()
            return

def run_hud_session(screen, joystick, selected, mono, mono2, small, big, title):
    ip = selected["ip"]
    base = selected["base"]
    robot_name = selected.get("robot_name", "Robot")
    pygame.display.set_caption(f"{robot_name} — HUD Controller")

    last_state = {}
    cams = []
    active_cam = DEFAULT_CAM_INDEX
    cam_streams = []
    cam_surfaces = []
    cam_last_ok = []

    heading_edit = -20.0
    speed_min = 0.0
    speed_max = 3000.0

    video_scale_mode = "fit"
    trim_edit_mode = False
    trim_buffer = ""

    held_cmd = "X"
    last_cmd_sent = 0.0

    last_latency_ms = None
    last_state_ok_ts = 0.0

    hit = {
        "back": None,
        "stop": None,
        "speed_up": None,
        "speed_dn": None,
        "cam_prev": None,
        "cam_next": None,
        "video": None,
        "thumbs": [],
    }

    last_tap = {"t": 0.0, "pos": (0, 0)}
    swipe = {"active": False, "start": (0, 0), "start_t": 0.0, "in_video": False}

    def start_all_cameras(cameras):
        nonlocal cam_streams, cam_surfaces, cam_last_ok, active_cam
        stop_streams(cam_streams)
        cam_streams = []
        cam_surfaces = []
        cam_last_ok = []
        if not cameras:
            return
        active_cam = int(clamp(active_cam, 0, len(cameras)-1))
        for c in cameras:
            url = str(c.get("url","")).replace("{host}", ip)
            st = MJPEGStream(url)
            st.start()
            cam_streams.append(st)
            cam_surfaces.append(None)
            cam_last_ok.append(False)

    def safe_cam_set(idx):
        nonlocal active_cam
        if cams:
            active_cam = int(clamp(idx, 0, len(cams)-1))

    def cam_next():
        if cams:
            safe_cam_set((active_cam + 1) % len(cams))

    def cam_prev():
        if cams:
            safe_cam_set((active_cam - 1) % len(cams))

    try:
        t0 = time.time()
        last_state = robot_get_state(base)
        last_latency_ms = int((time.time() - t0) * 1000)
        last_state_ok_ts = time.time()

        robot_name = str(last_state.get("robot_name") or robot_name).strip() or robot_name
        cams = last_state.get("cameras", [])
        heading_edit = float(last_state.get("forward_heading_deg", -20.0))
        speed_min = float(last_state.get("limits", {}).get("min_speed", 0))
        speed_max = float(last_state.get("limits", {}).get("max_speed", 3000))
        pygame.display.set_caption(f"{robot_name} — HUD Controller")
    except Exception:
        pass

    start_all_cameras(cams)

    # We will wrap this text inside the left panel instead of blitting a single long line.
    help_str = "Touch: BACK/STOP/Speed/Cam buttons, tap thumbs, tap video FIT/FILL, swipe video to change cam. Keys: WASD/QE | X | +/- | H/J | T | V | ESC"

    clock = pygame.time.Clock()
    last_poll = 0.0

    def set_held(cmd):
        nonlocal held_cmd
        held_cmd = cmd

    def maybe_send_cmd(force=False):
        nonlocal last_cmd_sent
        now = time.time()
        if force or (now - last_cmd_sent) >= (1.0 / CMD_REPEAT_HZ):
            robot_cmd(base, held_cmd)
            last_cmd_sent = now

    def handle_tap(pos):
        nonlocal video_scale_mode
        if hit["back"] and hit["back"].collidepoint(pos):
            stop_streams(cam_streams)
            return "BACK"
        if hit["stop"] and hit["stop"].collidepoint(pos):
            set_held("X"); maybe_send_cmd(force=True); return None
        if hit["speed_dn"] and hit["speed_dn"].collidepoint(pos):
            robot_cmd(base, "-"); return None
        if hit["speed_up"] and hit["speed_up"].collidepoint(pos):
            robot_cmd(base, "+"); return None
        if hit["cam_prev"] and hit["cam_prev"].collidepoint(pos):
            cam_prev(); return None
        if hit["cam_next"] and hit["cam_next"].collidepoint(pos):
            cam_next(); return None
        for idx, r in hit["thumbs"]:
            if r.collidepoint(pos):
                safe_cam_set(idx)
                return None
        if hit["video"] and hit["video"].collidepoint(pos):
            video_scale_mode = "fill" if video_scale_mode == "fit" else "fit"
            return None
        return None

    while True:
        clock.tick(60)
        now = time.time()

        for i, st in enumerate(cam_streams):
            if st and st.frame is not None:
                try:
                    cam_surfaces[i] = pil_to_surface(st.frame)
                    cam_last_ok[i] = True
                except Exception:
                    cam_last_ok[i] = False

        if now - last_poll > 0.5:
            last_poll = now
            try:
                t0 = time.time()
                s = robot_get_state(base, timeout=1.0)
                last_latency_ms = int((time.time() - t0) * 1000)
                last_state_ok_ts = time.time()

                last_state = s
                robot_name = str(s.get("robot_name") or robot_name).strip() or robot_name

                newcams = s.get("cameras", cams)
                if isinstance(newcams, list) and len(newcams) != len(cams):
                    cams = newcams
                    start_all_cameras(cams)
                else:
                    cams = newcams

                heading_edit = float(s.get("forward_heading_deg", heading_edit))
                speed_min = float(s.get("limits", {}).get("min_speed", speed_min))
                speed_max = float(s.get("limits", {}).get("max_speed", speed_max))
            except Exception:
                pass

        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                stop_streams(cam_streams)
                return False

            if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
                pos = e.pos
                swipe["active"] = True
                swipe["start"] = pos
                swipe["start_t"] = time.time()
                swipe["in_video"] = bool(hit["video"] and hit["video"].collidepoint(pos))

            if e.type == pygame.MOUSEBUTTONUP and e.button == 1:
                pos = e.pos
                if swipe["active"]:
                    dx = pos[0] - swipe["start"][0]
                    dy = pos[1] - swipe["start"][1]
                    swipe["active"] = False
                    if swipe["in_video"] and abs(dx) >= SWIPE_MIN_PX and abs(dy) <= SWIPE_MAX_OFFAXIS_PX:
                        (cam_next() if dx < 0 else cam_prev())
                        continue

                tnow = time.time()
                if (tnow - last_tap["t"]) <= DOUBLE_TAP_S and dist2(pos, last_tap["pos"]) <= (DOUBLE_TAP_DIST_PX**2):
                    last_tap["t"] = 0.0
                else:
                    last_tap["t"] = tnow
                    last_tap["pos"] = pos

                res = handle_tap(pos)
                if res == "BACK":
                    return True

            if TOUCH_AVAILABLE and e.type == pygame.FINGERDOWN:
                pos = event_pos_px(e, screen)
                if pos:
                    swipe["active"] = True
                    swipe["start"] = pos
                    swipe["start_t"] = time.time()
                    swipe["in_video"] = bool(hit["video"] and hit["video"].collidepoint(pos))

            if TOUCH_AVAILABLE and e.type == pygame.FINGERUP:
                pos = event_pos_px(e, screen)
                if pos:
                    if swipe["active"]:
                        dx = pos[0] - swipe["start"][0]
                        dy = pos[1] - swipe["start"][1]
                        swipe["active"] = False
                        if swipe["in_video"] and abs(dx) >= SWIPE_MIN_PX and abs(dy) <= SWIPE_MAX_OFFAXIS_PX:
                            (cam_next() if dx < 0 else cam_prev())
                            continue

                    tnow = time.time()
                    if (tnow - last_tap["t"]) <= DOUBLE_TAP_S and dist2(pos, last_tap["pos"]) <= (DOUBLE_TAP_DIST_PX**2):
                        last_tap["t"] = 0.0
                    else:
                        last_tap["t"] = tnow
                        last_tap["pos"] = pos

                    res = handle_tap(pos)
                    if res == "BACK":
                        return True

            if e.type == pygame.KEYDOWN:
                k = e.key
                if (not trim_edit_mode) and (k in BACK_KEYS):
                    stop_streams(cam_streams)
                    return True

                if k == pygame.K_ESCAPE:
                    stop_streams(cam_streams)
                    return False
                elif k == pygame.K_v:
                    video_scale_mode = "fill" if video_scale_mode == "fit" else "fit"
                elif k == pygame.K_w:
                    set_held("W"); maybe_send_cmd(force=True)
                elif k == pygame.K_s:
                    set_held("S"); maybe_send_cmd(force=True)
                elif k == pygame.K_a:
                    set_held("A"); maybe_send_cmd(force=True)
                elif k == pygame.K_d:
                    set_held("D"); maybe_send_cmd(force=True)
                elif k == pygame.K_q:
                    set_held("Q"); maybe_send_cmd(force=True)
                elif k == pygame.K_e:
                    set_held("E"); maybe_send_cmd(force=True)
                elif k == pygame.K_x:
                    set_held("X"); maybe_send_cmd(force=True)
                elif k in (pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS):
                    robot_cmd(base, "+")
                elif k in (pygame.K_MINUS, pygame.K_KP_MINUS):
                    robot_cmd(base, "-")
                elif k == pygame.K_h:
                    heading_edit -= 1
                    robot_set_heading(base, heading_edit)
                elif k == pygame.K_j:
                    heading_edit += 1
                    robot_set_heading(base, heading_edit)
                elif pygame.K_1 <= k <= pygame.K_9:
                    idx = k - pygame.K_1
                    if cams and idx < len(cams):
                        safe_cam_set(idx)

            if e.type == pygame.KEYUP:
                if e.key in (pygame.K_w, pygame.K_s, pygame.K_a, pygame.K_d, pygame.K_q, pygame.K_e):
                    set_held("X")
                    maybe_send_cmd(force=True)

            if e.type == pygame.JOYBUTTONDOWN:
                b = e.button
                if b == 1:
                    stop_streams(cam_streams)
                    return True
                if b == 0:
                    set_held("X"); maybe_send_cmd(force=True)
                elif b == 7:
                    cam_next()
                elif b == 4:
                    robot_cmd(base, "-")
                elif b == 5:
                    robot_cmd(base, "+")
                elif b == 2:
                    video_scale_mode = "fill" if video_scale_mode == "fit" else "fit"

            if e.type == pygame.JOYHATMOTION:
                hx, hy = e.value
                if hy == 1:
                    heading_edit += 1
                    robot_set_heading(base, heading_edit)
                elif hy == -1:
                    heading_edit -= 1
                    robot_set_heading(base, heading_edit)

        # gamepad driving
        if joystick is not None:
            lx = dz(joystick.get_axis(0))
            ly = dz(joystick.get_axis(1))
            forward = -ly
            turn = lx

            rx = 0.0
            for ax in (2, 3, 4):
                try:
                    val = joystick.get_axis(ax)
                    if abs(val) > abs(rx):
                        rx = val
                except Exception:
                    pass
            rx = dz(rx)

            cmd = "X"
            if abs(rx) >= ROTATE_ONLY_THRESH and abs(forward) < 0.20:
                cmd = "E" if rx > 0 else "Q"
            else:
                if forward > 0.25:
                    if turn < -0.25:
                        cmd = "A"
                    elif turn > 0.25:
                        cmd = "D"
                    else:
                        cmd = "W"
                elif forward < -0.25:
                    cmd = "S"
                else:
                    cmd = "X"

            if cmd != held_cmd:
                set_held(cmd)
                maybe_send_cmd(force=True)
            else:
                if held_cmd != "X":
                    maybe_send_cmd(force=False)

        # -------------------------
        # Draw UI
        # -------------------------
        w, h = screen.get_size()
        pad = 14
        draw_background(screen)

        header_left = title.render(robot_name.upper(), True, GREEN)
        screen.blit(header_left, (pad, pad))
        screen.blit(small.render("servo bus control / deck hud", True, MUTED), (pad, pad + 44))

        local_time = time.strftime("%H:%M:%S")
        server_time = str(last_state.get("server_time", "--"))
        pill_text = f"{VERSION}  |  {local_time}  |  {ip}:{DEFAULT_PORT}  |  {robot_name}  |  robot:{server_time}"
        pill_w = small.size(pill_text)[0] + 30

        long_pill_rect = draw_pill(screen, w - pad - pill_w, pad + 10, pill_text, small, ORANGE2)
        hit["back"] = draw_pill(screen, long_pill_rect.left - 120, pad + 10, "BACK", small, ORANGE2)

        left_w = int(min(520, w * 0.41))
        left_rect = pygame.Rect(pad, 86, left_w - pad, h - 86 - pad)
        right_rect = pygame.Rect(left_rect.right + pad, 86, w - (left_rect.right + 2*pad), h - 86 - pad)

        draw_panel(screen, left_rect)
        draw_panel(screen, right_rect)

        speed = float(last_state.get("speed", 0))
        heading = float(last_state.get("forward_heading_deg", heading_edit))

        inner = left_rect.inflate(-18, -18)

        # ---- FIX: better dial position (slightly lower and slightly smaller)
        dial_row_h = 220
        dial_row = pygame.Rect(inner.left, inner.top + 6, inner.width, dial_row_h)

        margin = 14
        cell_w = (dial_row.width - margin) // 2
        dial_r = min(102, (cell_w // 2) - 20)
        dial_cy = dial_row.top + dial_row_h // 2 + 6

        c1 = (dial_row.left + cell_w // 2, dial_cy)
        c2 = (dial_row.left + cell_w + margin + cell_w // 2, dial_cy)

        min_sp = float(last_state.get("limits", {}).get("min_speed", speed_min))
        max_sp = float(last_state.get("limits", {}).get("max_speed", speed_max))

        draw_dial(screen, c1, dial_r, speed, min_sp, max_sp, "SPEED", "units", small, big)
        draw_dial(screen, c2, dial_r, heading, -180, 180, "HEADING", "deg", small, big)

        # Buttons row
        btn_y = dial_row.bottom + 10
        bx = inner.left
        hit["stop"]     = draw_pill(screen, bx, btn_y, "STOP", small, ORANGE2, border=GREEN); bx = hit["stop"].right + 10
        hit["speed_dn"] = draw_pill(screen, bx, btn_y, "SPEED-", small, ORANGE2); bx = hit["speed_dn"].right + 10
        hit["speed_up"] = draw_pill(screen, bx, btn_y, "SPEED+", small, ORANGE2); bx = hit["speed_up"].right + 10
        hit["cam_prev"] = draw_pill(screen, bx, btn_y, "CAM◀", small, ORANGE2); bx = hit["cam_prev"].right + 10
        hit["cam_next"] = draw_pill(screen, bx, btn_y, "CAM▶", small, ORANGE2)

        # ---- FIX: Wrap + clip help text inside left panel width
        help_rect = pygame.Rect(inner.left, btn_y + 46, inner.width, 42)
        draw_wrapped_text(screen, help_str, small, MUTED, help_rect, line_spacing=2)

        # Telemetry blocks start BELOW wrapped help area (no overlap)
        tele_top = help_rect.bottom + 10
        tele_rect = pygame.Rect(inner.left, tele_top, inner.width, inner.bottom - tele_top)

        block_h = min(170, tele_rect.height // 2)
        block1 = pygame.Rect(tele_rect.left, tele_rect.top, tele_rect.width, block_h)
        block2 = pygame.Rect(tele_rect.left, block1.bottom + 12, tele_rect.width, tele_rect.bottom - (block1.bottom + 12))

        pygame.draw.rect(screen, (*ORANGE2, 60), block1, 1, border_radius=12)
        pygame.draw.rect(screen, (*ORANGE2, 60), block2, 1, border_radius=12)
        screen.blit(mono2.render("TELEMETRY", True, ORANGE2), (block1.left + 10, block1.top + 8))
        screen.blit(mono2.render("CAMERA / STREAM", True, ORANGE2), (block2.left + 10, block2.top + 8))

        state_age = time.time() - last_state_ok_ts if last_state_ok_ts else 9999
        latency_str = f"{last_latency_ms} ms" if last_latency_ms is not None else "--"
        link_col = GREEN if state_age < 2.0 else (ORANGE2 if state_age < 6.0 else RED)
        link_str = "OK" if state_age < 2.0 else ("STALE" if state_age < 6.0 else "DOWN")

        last_cmd = str(last_state.get("last_cmd", held_cmd))
        last_cmd_age = last_state.get("last_cmd_age_s", None)
        last_cmd_age_s = f"{last_cmd_age:.1f}s" if isinstance(last_cmd_age, (int, float)) else "--"

        rows1 = [
            ("Link", link_str, link_col),
            ("Latency", latency_str, TEXT),
            ("Held Cmd", held_cmd, TEXT),
            ("Server Cmd", last_cmd, TEXT),
            ("Cmd Age", last_cmd_age_s, TEXT),
            ("Trim/Head", f"{int(round(heading_edit))}°", TEXT),
            ("Video Mode", video_scale_mode.upper(), TEXT),
            ("Gamepad", "YES" if joystick else "NO", TEXT),
        ]
        draw_kv_list(screen, block1.inflate(-10, -38), rows1, mono2, mono2)

        cam_name = "--"
        cam_ok = False
        cam_fps = 0.0
        cam_age_s = None
        if cams and 0 <= active_cam < len(cams):
            cam_name = str(cams[active_cam].get("name", f"Camera {active_cam+1}"))
            cam_ok = bool(cam_last_ok[active_cam]) if active_cam < len(cam_last_ok) else False
            st = cam_streams[active_cam] if active_cam < len(cam_streams) else None
            if st:
                cam_fps = float(st.fps or 0.0)
                if st.last_frame_ts:
                    cam_age_s = time.time() - st.last_frame_ts

        cam_status = "OK" if cam_ok else "OFFLINE"
        cam_col = GREEN if cam_ok else RED
        age_str = f"{cam_age_s:.1f}s" if cam_age_s is not None else "--"

        rows2 = [
            ("Active Cam", f"{active_cam}  {cam_name}", TEXT),
            ("Cam Status", cam_status, cam_col),
            ("Cam FPS", f"{cam_fps:.1f}", TEXT),
            ("Frame Age", age_str, TEXT),
            ("Cams Total", str(len(cams)), TEXT),
            ("Streams", str(len(cam_streams)), TEXT),
        ]
        draw_kv_list(screen, block2.inflate(-10, -38), rows2, mono2, mono2)

        # Right side: thumbs + video
        thumb_h = 96
        thumbs_rect = pygame.Rect(right_rect.left + 16, right_rect.top + 14, right_rect.width - 32, thumb_h)
        pygame.draw.rect(screen, (*ORANGE2, 40), thumbs_rect, 1, border_radius=14)

        hit["thumbs"] = []
        cams_count = len(cams)

        def draw_thumb(idx, cell, is_active):
            if is_active:
                draw_glow_rect(screen, cell, ORANGE2, radius=12, strength=1)
            pygame.draw.rect(screen, (*ORANGE, 160) if is_active else (*ORANGE2, 90),
                             cell, 3 if is_active else 1, border_radius=12)
            nm = cams[idx].get("name", f"Camera {idx+1}")
            label = small.render(f"{idx}: {nm}", True, TEXT if is_active else MUTED)
            screen.blit(label, (cell.left + 10, cell.top + 6))
            surf = cam_surfaces[idx] if idx < len(cam_surfaces) else None
            if surf is not None:
                target = pygame.Rect(cell.left + 8, cell.top + 28, cell.width - 16, cell.height - 36)
                iw, ih = surf.get_width(), surf.get_height()
                sc = min(target.width/iw, target.height/ih)
                nw, nh = int(iw*sc), int(ih*sc)
                s2 = pygame.transform.smoothscale(surf, (nw, nh))
                screen.blit(s2, (target.left + (target.width-nw)//2, target.top + (target.height-nh)//2))
            else:
                t = small.render("offline…", True, RED)
                screen.blit(t, (cell.left + 10, cell.top + 34))

        if cams_count > 0:
            show_idxs = [active_cam]
            if cams_count > 1:
                show_idxs.append((active_cam + 1) % cams_count)
            if cams_count > 2:
                show_idxs.append((active_cam + 2) % cams_count)

            cols = len(show_idxs)
            cell_w = thumbs_rect.width // cols
            for i, idx in enumerate(show_idxs):
                cell = pygame.Rect(thumbs_rect.left + i*cell_w, thumbs_rect.top, cell_w, thumbs_rect.height).inflate(-10, -10)
                draw_thumb(idx, cell, idx == active_cam)
                hit["thumbs"].append((idx, cell))
        else:
            screen.blit(mono.render("No cameras reported by /state", True, RED),
                        (thumbs_rect.left + 10, thumbs_rect.top + 10))

        view_rect = pygame.Rect(right_rect.left + 16, thumbs_rect.bottom + 14, right_rect.width - 32, right_rect.height - (thumb_h + 46))
        pygame.draw.rect(screen, (*ORANGE2, 40), view_rect, 1, border_radius=14)
        screen.blit(mono.render("ACTIVE VIDEO", True, ORANGE2), (view_rect.left + 12, view_rect.top + 10))
        hit["video"] = view_rect

        if cams_count > 0 and 0 <= active_cam < cams_count:
            screen.blit(small.render(f"{active_cam}: {cams[active_cam].get('name','')}", True, MUTED),
                        (view_rect.left + 12, view_rect.top + 30))

        if cams_count > 0 and active_cam < len(cam_surfaces) and cam_surfaces[active_cam] is not None:
            surf = cam_surfaces[active_cam]
            target = pygame.Rect(view_rect.left + 12, view_rect.top + 54, view_rect.width - 24, view_rect.height - 66)
            iw, ih = surf.get_width(), surf.get_height()

            sc = fit_or_fill(iw, ih, target.width, target.height, mode=video_scale_mode)
            nw, nh = int(iw*sc), int(ih*sc)
            s2 = pygame.transform.smoothscale(surf, (nw, nh))

            if video_scale_mode == "fill":
                cx2 = (nw - target.width)//2
                cy2 = (nh - target.height)//2
                crop = pygame.Rect(cx2, cy2, target.width, target.height)
                screen.blit(s2, target.topleft, area=crop)
            else:
                screen.blit(s2, (target.left + (target.width-nw)//2, target.top + (target.height-nh)//2))
        else:
            msg = "No camera frame (stream starting…)" if state_age < 6.0 else "No camera frame (robot/state unreachable)"
            screen.blit(mono.render(msg, True, MUTED if state_age < 6.0 else RED),
                        (view_rect.left + 12, view_rect.top + 70))

        pygame.display.flip()

if __name__ == "__main__":
    main()
