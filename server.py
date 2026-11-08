import os, sys, io, re, math, hmac, socket, uuid, time, json, shutil, tempfile, threading, functools
import subprocess, atexit, hashlib, secrets, collections
from flask import Flask, request, jsonify, send_file, session
import fitz
from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageGrab
import printing
import updatemgr


def res(p):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, p)


app = Flask(__name__, static_folder=None)
app.secret_key = os.urandom(24)
app.register_blueprint(updatemgr.bp)   # auto update routes (/api/update...), PC window only
FILES = {}
ORDER = []
GONE = {}   # file id -> set of page numbers removed with the red cross (shared by PC and phone)
JOBS = {}
updatemgr.HOOKS["busy"] = lambda: any(j.get("state") == "run" for j in list(JOBS.values()))
updatemgr.HOOKS["files"] = lambda: len(FILES)
LOCK = threading.Lock()    # fitz is not thread safe
SLOCK = threading.Lock()   # file list changes
REV = {"n": 0}
NOTICE = {"t": ""}
BATCH = {"t": 0.0, "ids": []}
HOOKS = {"rebind": None, "pick": None, "pickdir": None, "front": None, "autostart_get": None, "autostart_set": None}   # set by app.py (file dialog, bring window to front)
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
SCALES = ("fit", "actual", "fill", "custom")

CFG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "PCPrint")
PROF_FILE = os.path.join(CFG_DIR, "profiles.json")
SET_FILE = os.path.join(CFG_DIR, "settings.json")
PLOCK = threading.Lock()
DIALOG_LOCK = threading.Lock()


_UP = {"dir": None, "n": 0}   # temp folder for dropped / pasted files


def upload_dir():
    if not _UP["dir"] or not os.path.isdir(_UP["dir"]):
        _UP["dir"] = tempfile.mkdtemp(prefix="pcprint_")
    return _UP["dir"]


def cleanup():
    try:
        tunnel_stop()
    except Exception:
        pass
    if _UP["dir"]:
        shutil.rmtree(_UP["dir"], ignore_errors=True)


LOCAL = ("127.0.0.1", "::1")
PH = {"ok": False, "port": 0, "fails": 0, "until": 0}
PH_FILE = os.path.join(CFG_DIR, "phone.json")
SREV = {"n": 0}
LOCAL_ONLY = ("/api/add", "/api/pick", "/api/paste", "/api/autostart", "/api/phone/", "/api/x", "/api/update", "/api/save")   # /api/savedir, /api/savepdf: write to this PC's disk


PLOCK2 = threading.Lock()
# lockout state per way of connecting: lan = same Wi-Fi, net = internet link
FAIL = {"lan": {"fails": 0, "until": 0.0, "rounds": 0}, "net": {"fails": 0, "until": 0.0, "rounds": 0}}


def ph_cfg():
    """Phone settings. Same Wi-Fi (pw_on/pin/port) and Internet (net_pw_on/net_pin) are separate.
    Internet PIN is ON by default and a random PIN is made the first time."""
    try:
        with open(PH_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        d = {}
    try:
        port = int(d.get("port", 5050))
    except Exception:
        port = 5050
    npin = str(d.get("net_pin", ""))
    if not (npin.isdigit() and len(npin) == 4):
        npin = f"{secrets.randbelow(10000):04d}"
        d["net_pin"] = npin
        try:
            with PLOCK2:
                os.makedirs(CFG_DIR, exist_ok=True)
                with open(PH_FILE, "w", encoding="utf-8") as fh:
                    json.dump({"pw_on": bool(d.get("pw_on")), "pin": str(d.get("pin", "")), "port": port,
                               "net_pw_on": bool(d.get("net_pw_on", True)), "net_pin": npin}, fh)
        except Exception:
            pass
    return {"pw_on": bool(d.get("pw_on")), "pin": str(d.get("pin", "")), "port": port,
            "net_pw_on": bool(d.get("net_pw_on", True)), "net_pin": npin}


def origin():
    """Where does this request come from?
    pc  = this PC's own window (full access)
    lan = a phone on the same Wi-Fi
    net = somebody using the internet link. The tunnel program connects from 127.0.0.1 too,
          so the headers it adds (and the host name) are checked first, otherwise internet
          visitors would look like this PC."""
    h = request.headers
    host = (request.host or "").split(":")[0].lower()
    if (any(k in h for k in ("Cf-Connecting-Ip", "Cf-Ray", "Cf-Visitor", "X-Forwarded-For", "X-Forwarded-Host"))
            or host.endswith(".trycloudflare.com")):
        return "net"
    if request.remote_addr in LOCAL:
        return "pc"
    return "lan"


def mode_pw(o, c=None):
    c = c or ph_cfg()
    return (c["net_pw_on"], c["net_pin"]) if o == "net" else (c["pw_on"], c["pin"])


def tok(pin):
    """Session proof tied to the current PIN: changing the PIN logs everybody out."""
    return hashlib.sha256(("pcprint|" + pin).encode()).hexdigest()[:20]


@app.before_request
def guard():
    """This PC's own window: full access. Phones (Wi-Fi or internet link): phone page + print API only
    (PIN if it is on for that way of connecting). Anything that touches this PC's disk/clipboard is PC-only."""
    o = origin()
    if o == "pc":
        return
    if o == "net" and not NET["on"]:
        return "internet access is off", 403
    p = request.path
    if p.startswith(LOCAL_ONLY):
        return "forbidden", 403
    if p in ("/", "/api/login", "/api/ping"):
        return
    if not p.startswith("/api/"):
        return "forbidden", 403
    on, pin = mode_pw(o)
    if on and session.get("ok_" + o) != tok(pin):
        return jsonify(error="login"), 401


@app.post("/api/login")
def login():
    o = origin()
    now = time.time()
    on, pin_ok = mode_pw(o)
    if o == "pc" or not on:
        session["ok_" + o] = tok(pin_ok)
        return jsonify(ok=True)
    f = FAIL[o]
    if now < f["until"]:
        return jsonify(error=f"Too many tries. Wait {int(f['until'] - now) + 1} seconds."), 429
    pin = str((request.get_json(silent=True) or {}).get("pin", ""))
    if hmac.compare_digest(pin, pin_ok):
        session["ok_" + o] = tok(pin_ok)
        f["fails"] = f["rounds"] = 0
        return jsonify(ok=True)
    f["fails"] += 1
    if f["fails"] >= 5:
        # every 5 wrong tries: wait 30 s, then 60 s, 120 s ... (max 1 hour). Slows down guessing a lot.
        f["fails"], f["until"] = 0, now + min(3600, 30 * (2 ** f["rounds"]))
        f["rounds"] += 1
    return jsonify(error="Wrong PIN"), 403


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ------------------------------------------------ internet link (Cloudflare quick tunnel)
NET = {"on": False, "state": "off", "url": "", "error": "", "proc": None, "gen": 0}
NLOCK = threading.Lock()
TUN_URL = re.compile(r"https://([a-z0-9-]+)\.trycloudflare\.com")


def find_cloudflared():
    for p in (res(os.path.join("bin", "cloudflared.exe")), os.path.join(CFG_DIR, "cloudflared.exe"),
              shutil.which("cloudflared")):
        if p and os.path.isfile(p):
            return p
    return None


def _tunnel_kill_locked():
    NET["gen"] += 1          # the old watcher thread sees this and stops touching NET
    pr, NET["proc"] = NET["proc"], None
    NET.update(on=False, state="off", url="", error="")
    if pr:
        try:
            pr.terminate()
            try:
                pr.wait(timeout=3)
            except Exception:
                pr.kill()
        except Exception:
            pass


def tunnel_stop():
    with NLOCK:
        _tunnel_kill_locked()


def _tunnel_watch(proc, gen):
    tail = collections.deque(maxlen=6)
    try:
        for line in proc.stdout:
            line = line.strip()
            if line:
                tail.append(line)
            m = TUN_URL.search(line)
            if m and m.group(1) != "api" and gen == NET["gen"] and not NET["url"]:
                NET.update(url=m.group(0), state="on")
    except Exception:
        pass
    try:
        proc.wait(timeout=2)
    except Exception:
        pass
    if gen == NET["gen"]:     # it stopped by itself (not by us)
        NET.update(on=False, state="error", url="", proc=None,
                   error="The internet link stopped. " + (tail[-1][:160] if tail else "Check the internet connection."))


def _tunnel_timeout(gen):
    time.sleep(40)
    with NLOCK:
        if gen == NET["gen"] and NET["state"] == "starting":
            _tunnel_kill_locked()
            NET.update(state="error", error="Could not get an internet link. Check the internet connection and try again.")


def tunnel_start():
    """Start (or restart = brand new link). Returns after the process is launched; the link
    appears a few seconds later (state 'starting' -> 'on')."""
    with NLOCK:
        _tunnel_kill_locked()
        exe = find_cloudflared()
        if not exe:
            NET.update(state="error", error="cloudflared.exe was not found. Internet access needs it next to the app (bin/cloudflared.exe).")
            return
        NET.update(on=True, state="starting")
        gen = NET["gen"]
        try:
            proc = subprocess.Popen(
                [exe, "tunnel", "--url", f"http://127.0.0.1:{PH['port']}", "--no-autoupdate", "--protocol", "http2"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", creationflags=0x08000000 if os.name == "nt" else 0)   # no console window
        except Exception as e:
            NET.update(on=False, state="error", error=f"Could not start: {e}")
            return
        NET["proc"] = proc
    threading.Thread(target=_tunnel_watch, args=(proc, gen), daemon=True).start()
    threading.Thread(target=_tunnel_timeout, args=(gen,), daemon=True).start()


atexit.register(tunnel_stop)


def net_pub():
    return {"state": NET["state"], "url": NET["url"], "error": NET["error"]}


@app.get("/api/phone/info")
def phone_info():
    c = ph_cfg()
    return jsonify(url=f"http://{local_ip()}:{PH['port']}", active=PH["ok"], net=net_pub(), **c)


@app.post("/api/phone/set")
def phone_set():
    d = request.get_json(silent=True) or {}
    c = ph_cfg()
    pin, port = str(d.get("pin", c["pin"])), d.get("port", c["port"])
    on = bool(d.get("pw_on", c["pw_on"]))
    npin = str(d.get("net_pin", c["net_pin"]))
    non = bool(d.get("net_pw_on", c["net_pw_on"]))
    if on and not (pin.isdigit() and len(pin) == 4):
        return jsonify(error="PIN must be exactly 4 digits"), 400
    if non and not (npin.isdigit() and len(npin) == 4):
        return jsonify(error="PIN must be exactly 4 digits"), 400
    try:
        port = int(port)
        assert 1024 <= port <= 65535
    except Exception:
        return jsonify(error="Port must be 1024-65535"), 400
    os.makedirs(CFG_DIR, exist_ok=True)
    with PLOCK2:
        with open(PH_FILE, "w", encoding="utf-8") as fh:
            json.dump({"pw_on": on, "pin": pin, "port": port, "net_pw_on": non, "net_pin": npin}, fh)
    if port != c["port"] and HOOKS["rebind"]:
        HOOKS["rebind"](port)
        if NET["on"]:
            tunnel_start()   # the tunnel must point to the new port (this gives a new link)
    return jsonify(ok=True, active=PH["ok"], port=PH["port"])


@app.post("/api/phone/net")
def phone_net():
    """Internet access on / off. 'new': true = cut the old link and make a new one."""
    d = request.get_json(silent=True) or {}
    if d.get("on") is False:
        tunnel_stop()
    elif d.get("on") is True and (d.get("new") or not NET["on"]):
        tunnel_start()
    return jsonify(net=net_pub())


@app.get("/api/phone/qr")
def phone_qr():
    import qrcode
    if request.args.get("mode") == "net":
        if not NET["url"]:
            return "", 404
        text = NET["url"]
    else:
        text = f"http://{local_ip()}:{PH['port']}"
    b = io.BytesIO()
    qrcode.make(text, box_size=8, border=2).save(b, "PNG")
    b.seek(0)
    r = send_file(b, mimetype="image/png")
    r.headers["Cache-Control"] = "no-store"
    return r


# ------------------------------------------------------------------ files
def bump():
    REV["n"] += 1


def natural(name):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name.lower())]


def open_file(path):
    ext = os.path.splitext(path)[1].lower()
    name = os.path.basename(path)
    if ext == ".pdf":
        with LOCK:
            d = fitz.open(path)
            n = d.page_count
            lands = []
            for i in range(n):
                r = d.load_page(i).rect
                lands.append(r.width > r.height)
            d.close()
        if n < 1:
            raise ValueError("empty PDF")
        kind = "pdf"
    elif ext in IMG_EXT:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            w, h = im.size
            if im.getexif().get(274, 1) in (5, 6, 7, 8):
                w, h = h, w
        kind, n, lands = "img", 1, [w > h]
    else:
        raise ValueError("only PDF and image files are supported")
    fid = uuid.uuid4().hex[:10]
    FILES[fid] = {"id": fid, "name": name, "kind": kind, "pages": n, "lands": lands, "path": path}
    return fid


def register(paths, sort=True):
    """Add files. Files that arrive within 1.5 s of each other (one 'Open with' on
    many files) are treated as one batch and sorted by name."""
    added, errors = 0, []
    with SLOCK:
        if sort:
            now = time.time()
            if now - BATCH["t"] > 1.5:
                BATCH["ids"] = []
            BATCH["t"] = now
        for p in paths:
            p = os.path.abspath(p)
            if any(FILES[i]["path"] == p for i in ORDER if i in FILES):
                continue
            try:   # a picture used earlier by the Transform tool is known already
                fid = next((i for i, x in FILES.items() if x["path"] == p), None) or open_file(p)
            except Exception as e:
                errors.append(f"{os.path.basename(p)}: {e}")
                continue
            ORDER.append(fid)
            added += 1
            if sort:
                BATCH["ids"].append(fid)
        if sort and len(BATCH["ids"]) > 1:
            ids = BATCH["ids"]
            k = len(ids)
            if ORDER[-k:] == ids:
                ordered = sorted(ids, key=lambda i: natural(FILES[i]["name"]))
                ORDER[-k:] = ordered
                BATCH["ids"] = ordered
        if errors:
            NOTICE["t"] = "\n".join(errors)
        if added or errors:
            bump()
    return added, errors


def pub(f):
    return {"id": f["id"], "name": f["name"], "kind": f["kind"], "pages": f["pages"], "lands": f["lands"],
            "gone": sorted(GONE.get(f["id"], ()))}


@app.get("/")
def index():
    page = "index.html" if origin() == "pc" else "phone.html"
    r = send_file(res("static/" + page))
    r.headers["Cache-Control"] = "no-store"
    return r


@app.get("/api/ping")
def ping():
    return jsonify(app="pcprint")


@app.post("/api/add")
def add():
    d = request.get_json(silent=True) or {}
    paths = [str(p) for p in d.get("paths", [])]
    added, errors = register(paths, sort=True)
    if HOOKS["front"]:
        try:
            HOOKS["front"]()
        except Exception:
            pass
    return jsonify(added=added, errors=errors)


@app.post("/api/pick")
def pick():
    if not HOOKS["pick"]:
        return jsonify(error="No file dialog"), 500
    try:
        paths = HOOKS["pick"]()
    except Exception as e:
        return jsonify(error=str(e)), 500
    added, errors = register(paths or [], sort=False)
    return jsonify(added=added, errors=errors)


def sniff_ext(path):
    """Look at the file's first bytes to find out if it is a PDF or a picture."""
    try:
        with open(path, "rb") as fh:
            if fh.read(1024).lstrip()[:4] == b"%PDF":
                return ".pdf"
        with Image.open(path) as im:
            return {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif",
                    "BMP": ".bmp", "TIFF": ".tif"}.get(im.format)
    except Exception:
        return None


def save_upload(up):
    """Save one dropped/pasted file into the temp folder, keeping its real name."""
    name = os.path.basename((up.filename or "").replace("\\", "/")).strip()
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    ext = os.path.splitext(name)[1].lower()
    mt = (up.mimetype or "").lower()
    generic = name.lower() in ("", "image.png", "image.jpg", "image.jpeg", "blob")
    if generic or not ext:
        e = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
             "image/bmp": ".bmp", "application/pdf": ".pdf"}.get(mt)
        if e:   # a pasted picture (screenshot) has no real name
            _UP["n"] += 1
            name = f"Pasted image {_UP['n']}{e}" if e != ".pdf" else f"Pasted file {_UP['n']}.pdf"
    d = os.path.join(upload_dir(), uuid.uuid4().hex[:8])
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name or "file")
    up.save(path)
    if os.path.splitext(path)[1].lower() not in IMG_EXT | {".pdf"}:
        e2 = sniff_ext(path)   # shared files often come without a usable name / type
        if e2:
            os.replace(path, path + e2)
            path += e2
    return path


@app.get("/api/files")
def files_list():
    """Same answer as the old Phone Print server (other apps / share targets rely on it)."""
    with SLOCK:
        return jsonify([pub(FILES[i]) for i in ORDER if i in FILES])


@app.post("/api/upload")
def upload():
    """Files dropped on the window, pasted, or sent from a phone / another app.
    One file: answers with that file (id, name, kind, pages, lands) like the old Phone Print,
    or status 400 + {error}. Several files: {added, errors}."""
    ups = request.files.getlist("file")
    paths = [save_upload(u) for u in ups]
    if not paths:
        return jsonify(added=0, errors=["No file received"], error="No file"), 400
    with SLOCK:
        NOTICE["t"] = ""
    added, errors = register(paths, sort=True)
    with SLOCK:
        if origin() != "pc":
            NOTICE["t"] = ""   # the phone gets the error in this answer
    if len(paths) == 1:
        if errors:
            return jsonify(added=0, errors=errors, error=errors[0]), 400
        f = next((x for x in FILES.values() if x["path"] == os.path.abspath(paths[0])), None)
        return jsonify(added=1, errors=[], **(pub(f) if f else {}))
    return jsonify(added=added, errors=errors)


@app.post("/api/paste")
def paste():
    """Ctrl+V fallback: read the Windows clipboard (copied files, or a copied picture)."""
    try:
        data = ImageGrab.grabclipboard()
    except Exception as e:
        return jsonify(added=0, errors=[f"Cannot read the clipboard: {e}"])
    if isinstance(data, list):   # files copied in Explorer
        added, errors = register([p for p in data if os.path.isfile(p)], sort=True)
        return jsonify(added=added, errors=errors)
    if data is not None:         # a copied picture
        _UP["n"] += 1
        d = os.path.join(upload_dir(), uuid.uuid4().hex[:8])
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"Pasted image {_UP['n']}.png")
        data.convert("RGB").save(path)
        added, errors = register([path], sort=True)
        return jsonify(added=added, errors=errors)
    return jsonify(added=0, errors=["The clipboard has no file or picture"])


# ------------------------- extra pictures for the Transform tool (not added to the page list)
def register_extra(paths):
    """Pictures dropped / pasted / chosen onto a sheet. The server knows them (so they can be
    drawn and printed) but they are NOT added to the list of pages."""
    out, errors = [], []
    with SLOCK:
        for p in paths:
            p = os.path.abspath(p)
            fid = next((i for i, x in FILES.items() if x["path"] == p), None)
            try:
                if fid is None:
                    fid = open_file(p)
                out.append(pub(FILES[fid]))
            except Exception as e:
                errors.append(f"{os.path.basename(p)}: {e}")
    return {"files": out, "errors": errors}


@app.post("/api/xupload")
def xupload():
    paths = [save_upload(u) for u in request.files.getlist("file")]
    if not paths:
        return jsonify(files=[], errors=["No file received"])
    return jsonify(**register_extra(paths))


@app.post("/api/xpaste")
def xpaste():
    try:
        data = ImageGrab.grabclipboard()
    except Exception as e:
        return jsonify(files=[], errors=[f"Cannot read the clipboard: {e}"])
    if isinstance(data, list):
        return jsonify(**register_extra([p for p in data if os.path.isfile(p)]))
    if data is not None:
        _UP["n"] += 1
        d = os.path.join(upload_dir(), uuid.uuid4().hex[:8])
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"Pasted image {_UP['n']}.png")
        data.convert("RGB").save(path)
        return jsonify(**register_extra([path]))
    return jsonify(files=[], errors=["The clipboard has no file or picture"])


@app.post("/api/xpick")
def xpick():
    if not HOOKS["pick"]:
        return jsonify(error="No file dialog"), 500
    try:
        paths = HOOKS["pick"]()
    except Exception as e:
        return jsonify(error=str(e)), 500
    return jsonify(**register_extra(paths or []))


@app.get("/api/autostart")
def autostart_get():
    h = HOOKS.get("autostart_get")
    return jsonify(available=bool(h), on=bool(h()) if h else False)


@app.post("/api/autostart")
def autostart_set():
    h = HOOKS.get("autostart_set")
    if not h:
        return jsonify(error="Not available"), 400
    on = bool((request.get_json(silent=True) or {}).get("on"))
    try:
        h(on)
    except Exception as e:
        return jsonify(error=str(e)), 500
    return jsonify(on=on)


@app.get("/api/state")
def state():
    with SLOCK:
        notice, NOTICE["t"] = NOTICE["t"], ""
        return jsonify(rev=REV["n"], srev=SREV["n"], notice=notice, files=[pub(FILES[i]) for i in ORDER if i in FILES])


@app.delete("/api/files/<fid>")
def del_one(fid):
    with SLOCK:
        GONE.pop(fid, None)
        if FILES.pop(fid, None) is not None and fid in ORDER:
            ORDER.remove(fid)
            bump()
    return jsonify(ok=True)


@app.post("/api/pages/remove")
def pages_remove():
    """Remove one page of a file from the list (PC and phone both follow). Removing the last page removes the file."""
    d = request.get_json(silent=True) or {}
    fid = str(d.get("f", ""))
    try:
        p = int(d.get("p"))
    except Exception:
        return jsonify(error="bad page"), 400
    with SLOCK:
        f = FILES.get(fid)
        if not f or not (0 <= p < f["pages"]):
            return jsonify(ok=True)
        g = GONE.setdefault(fid, set())
        g.add(p)
        if len(g) >= f["pages"]:
            GONE.pop(fid, None)
            FILES.pop(fid, None)
            if fid in ORDER:
                ORDER.remove(fid)
        bump()
    return jsonify(ok=True)


@app.delete("/api/files")
def del_all():
    with SLOCK:
        FILES.clear()
        ORDER.clear()
        GONE.clear()
        bump()
    return jsonify(ok=True)


@app.post("/api/move")
def move():
    d = request.get_json(silent=True) or {}
    fid, step = d.get("id"), -1 if d.get("dir") == "up" else 1
    with SLOCK:
        if fid in ORDER:
            i = ORDER.index(fid)
            j = i + step
            if 0 <= j < len(ORDER):
                ORDER[i], ORDER[j] = ORDER[j], ORDER[i]
                bump()
    return jsonify(ok=True)


# ------------------------------------------------------------- settings
@app.get("/api/settings")
def settings_get():
    try:
        with open(SET_FILE, "r", encoding="utf-8") as fh:
            return jsonify(json.load(fh))
    except Exception:
        return jsonify({})


@app.post("/api/settings")
def settings_set():
    d = request.get_json(silent=True) or {}
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        try:
            with open(SET_FILE, "r", encoding="utf-8") as fh:
                old = json.load(fh)
        except Exception:
            old = {}
        old.update(d)   # keys written by the other side survive
        with open(SET_FILE, "w", encoding="utf-8") as fh:
            json.dump(old, fh)
        SREV["n"] += 1
    except Exception:
        pass
    return jsonify(ok=True)


# ------------------------------------------------------- page numbers
def gnum(fid, p):
    """1-based position of a page in the whole document (all files in order)."""
    n = 0
    for i in list(ORDER):
        f = FILES.get(i)
        if not f:
            continue
        gone = GONE.get(i, ())
        if i == fid:
            if p in gone:
                return None
            return n + sum(1 for q in range(p) if q not in gone) + 1
        n += f["pages"] - len(gone)
    return None


def numspec(args, fid, p):
    if str(args.get("num")) not in ("1", "true", "True"):
        return None
    pos = args.get("npos") if args.get("npos") in ("top", "bottom") else "bottom"
    align = args.get("nalign") if args.get("nalign") in ("left", "center", "right") else "center"
    try:
        size = max(6.0, min(72.0, float(args.get("nsize", 12))))
    except Exception:
        size = 12.0
    try:
        start = int(float(args.get("nstart", 1)))
    except Exception:
        start = 1
    try:
        margin = max(2.0, min(40.0, float(args.get("nmargin", 8))))
    except Exception:
        margin = 8.0
    bold = str(args.get("nbold")) in ("1", "true", "True")
    try:
        n = int(args["nn"]) if args.get("nn") else None   # Transform: serial number of the sheet in the printed list
    except Exception:
        n = None
    if n is None:
        n = gnum(fid, p)   # position in the whole document, so one file keeps its serial number
    if n is None:
        return None
    return {"text": str(start - 1 + n), "pos": pos, "align": align, "size": size,
            "margin": margin, "bold": bold}


@functools.lru_cache(maxsize=64)
def get_font(px, bold=False):
    win = os.environ.get("WINDIR", r"C:\Windows")
    names = (["timesbd.ttf", "Times New Roman Bold.ttf", "LiberationSerif-Bold.ttf", "DejaVuSerif-Bold.ttf"]
             if bold else
             ["times.ttf", "Times New Roman.ttf", "LiberationSerif-Regular.ttf", "DejaVuSerif.ttf"])
    for name in [os.path.join(win, "Fonts", names[0])] + names:
        try:
            return ImageFont.truetype(name, max(6, int(px)))
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=max(6, int(px)))
    except Exception:
        return ImageFont.load_default()


def stamp(img, num, ppmm):
    """Print the page number (Times New Roman) directly on the page, at the chosen place."""
    d = ImageDraw.Draw(img)
    fnt = get_font(round(num["size"] * 25.4 / 72 * ppmm), num["bold"])
    text = num["text"]
    l, t, r, b = d.textbbox((0, 0), text, font=fnt)
    tw, th = r - l, b - t
    W, H = img.size
    m = int(num["margin"] * ppmm)
    if num["align"] == "left":
        x = m
    elif num["align"] == "right":
        x = W - m - tw
    else:
        x = (W - tw) // 2
    y = m if num["pos"] == "top" else H - m - th
    d.text((x - l, y - t), text, font=fnt, fill=0 if img.mode == "L" else (0, 0, 0))


# ------------------------------------------------------------- drawing
def get_pct(v):
    """Custom scaling percent (of actual size), kept between 10 and 400."""
    try:
        return max(10.0, min(400.0, float(v)))
    except Exception:
        return 100.0


def compose(f, idx, W, H, mono, rotate=True, mode="fit", ppmm=None, pct=100.0):
    """Draw one page on a white sheet of W x H pixels.
    mode "fit":    whole page fitted inside the area, nothing cut off
    mode "actual": real size (needs ppmm = pixels per mm), may be cut at the edges
    mode "fill":   page covers the whole area, the extra part is cropped
    mode "custom": real size x pct/100, centered (needs ppmm), may be cut at the edges"""
    def pick(fw, fh, unit_mm):
        if mode == "fill":
            s = max(W / fw, H / fh)
        elif mode == "actual" and ppmm:
            s = ppmm * unit_mm
        elif mode == "custom" and ppmm:
            s = ppmm * unit_mm * pct / 100.0
        else:
            s = min(W / fw, H / fh)
        return min(s, 8000.0 / max(fw, fh))  # safety cap for huge pages

    if f["kind"] == "pdf":
        with LOCK:
            doc = fitz.open(f["path"])
            try:
                pg = doc[idx]
                pw, ph = pg.rect.width, pg.rect.height
                rot = rotate and ((pw > ph) != (W > H))
                fw, fh = (ph, pw) if rot else (pw, ph)
                s = pick(fw, fh, 25.4 / 72)
                pix = pg.get_pixmap(matrix=fitz.Matrix(s, s),
                                    colorspace=fitz.csGRAY if mono else fitz.csRGB, alpha=False)
                img = Image.frombytes("L" if mono else "RGB", (pix.width, pix.height), pix.samples)
            finally:
                doc.close()
    else:
        img = Image.open(f["path"])
        try:
            dpi = float((img.info.get("dpi") or (300, 300))[0])
        except Exception:
            dpi = 300.0
        if dpi < 1:
            dpi = 300.0
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, "white")
            bg.paste(img, mask=img.split()[3])
            img = bg
        else:
            img = img.convert("RGB")
        iw, ih = img.size
        rot = rotate and ((iw > ih) != (W > H))
        fw, fh = (ih, iw) if rot else (iw, ih)
        s = pick(fw, fh, 25.4 / dpi)
        img = img.resize((max(1, int(iw * s)), max(1, int(ih * s))), Image.LANCZOS)
        if mono:
            img = img.convert("L")
    if rot:
        img = img.rotate(90, expand=True)
    canvas = Image.new(img.mode, (W, H), 255 if img.mode == "L" else (255, 255, 255))
    canvas.paste(img, ((W - img.width) // 2, (H - img.height) // 2))  # larger image is cropped
    return canvas


def sheet(f, idx, W, H, mono, mode="fit", ppmm=None, pct=100.0, num=None, rotate=True, items=None):
    """One printed sheet: the page, with the page number printed directly on it.
    items = free layout from the Transform tool (None / empty = the normal way)."""
    if items is not None and ppmm:
        canvas = compose_items(W, H, mono, ppmm, items)
    else:
        canvas = compose(f, idx, W, H, mono, rotate, mode, ppmm, pct)
    if num and ppmm:
        stamp(canvas, num, ppmm)
    return canvas


# ------------------------------------------------- free layout (Transform)
def natural_mm(f, idx):
    """Size of the page at its natural (100%) size, in mm."""
    if f["kind"] == "pdf":
        with LOCK:
            doc = fitz.open(f["path"])
            try:
                r = doc[idx].rect
                return r.width * 25.4 / 72, r.height * 25.4 / 72
            finally:
                doc.close()
    with Image.open(f["path"]) as im:
        try:
            dpi = float((im.info.get("dpi") or (300, 300))[0])
        except Exception:
            dpi = 300.0
        if dpi < 1:
            dpi = 300.0
        w, h = im.size
        if im.getexif().get(274, 1) in (5, 6, 7, 8):
            w, h = h, w
    return w / dpi * 25.4, h / dpi * 25.4


def load_source(f, idx, want_w, mono):
    """The page as one flat picture, about want_w pixels wide (never enlarged for pictures)."""
    want_w = max(16.0, float(want_w))
    if f["kind"] == "pdf":
        with LOCK:
            doc = fitz.open(f["path"])
            try:
                pg = doc[idx]
                pw, ph = pg.rect.width, pg.rect.height
                s = min(want_w / pw, 8000.0 / max(pw, ph))
                pix = pg.get_pixmap(matrix=fitz.Matrix(s, s),
                                    colorspace=fitz.csGRAY if mono else fitz.csRGB, alpha=False)
                return Image.frombytes("L" if mono else "RGB", (pix.width, pix.height), pix.samples)
            finally:
                doc.close()
    img = Image.open(f["path"])
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img, mask=img.split()[3])
        img = bg
    else:
        img = img.convert("RGB")
    if img.width > want_w:
        img = img.resize((max(1, int(want_w)), max(1, int(img.height * want_w / img.width))), Image.LANCZOS)
    return img.convert("L") if mono else img


def clean_items(raw, own=None):
    """Pictures placed on a sheet: centre (cx, cy) and size (w, h) in mm from the printable
    area's top-left corner, rotation in degrees (clockwise), and which file/page to draw
    (f, p; default = the sheet's own page). Bad entries are dropped."""
    out = []
    if not isinstance(raw, list):
        return out
    for it in raw[:100]:
        try:
            v = {k: float(it[k]) for k in ("cx", "cy", "w", "h")}
            v["rot"] = float(it.get("rot", 0))
            fobj = FILES.get(str(it["f"])) if it.get("f") else (own[0] if own else None)
            pg = int(it["p"]) if it.get("p") is not None else (own[1] if own else 0)
        except Exception:
            continue
        if not all(math.isfinite(x) for x in v.values()):
            continue
        if not fobj or not (0 <= pg < fobj["pages"]):
            continue
        v["w"] = max(1.0, min(3000.0, v["w"]))
        v["h"] = max(1.0, min(3000.0, v["h"]))
        v["file"], v["pg"] = fobj, pg
        out.append(v)
    return out


def compose_items(W, H, mono, ppmm, items):
    """White sheet of W x H pixels with every picture placed where it was put,
    each with its own size and rotation (later ones on top)."""
    canvas = Image.new("L" if mono else "RGB", (W, H), 255 if mono else (255, 255, 255))
    need = {}
    for it in items:
        k = (it["file"]["id"], it["pg"])
        need[k] = [it["file"], it["pg"], max(need.get(k, [0, 0, 0])[2], it["w"] * ppmm)]
    srcs = {k: load_source(v[0], v[1], v[2], mono) for k, v in need.items()}
    for it in items:
        src = srcs[(it["file"]["id"], it["pg"])]
        tw, th = max(1, round(it["w"] * ppmm)), max(1, round(it["h"] * ppmm))
        if tw * th > 60_000_000:
            continue
        im = src if src.size == (tw, th) else src.resize((tw, th), Image.LANCZOS)
        rot = it["rot"] % 360
        if 0.01 < rot < 359.99:
            rgba = im.convert("RGBA").rotate(-rot, expand=True, resample=Image.BICUBIC)
            tile, mask = rgba.convert(canvas.mode), rgba.getchannel("A")
        else:
            tile, mask = im, None
        x = round(it["cx"] * ppmm - tile.width / 2)
        y = round(it["cy"] * ppmm - tile.height / 2)
        canvas.paste(tile, (x, y), mask)
    return canvas


def get_page():
    f = FILES.get(request.args.get("f", ""))
    try:
        p = int(request.args.get("p", 0))
    except Exception:
        return None, None
    if not f or not (0 <= p < f["pages"]):
        return None, None
    return f, p


def send_img(img):
    b = io.BytesIO()
    img.save(b, "JPEG", quality=85)
    b.seek(0)
    r = send_file(b, mimetype="image/jpeg")
    r.headers["Cache-Control"] = "no-store"
    return r


def get_area(q):
    """Paper + printable area from the real driver; falls back to plain paper size."""
    orient = q.get("orient", "portrait")
    pw = float(q.get("w", 210))
    ph = float(q.get("h", 297))
    if (orient == "landscape") != (pw > ph):
        pw, ph = ph, pw
    a = {"pw": pw, "ph": ph, "ox": 0, "oy": 0, "aw": pw, "ah": ph, "driver": False}
    try:
        a = printing.area(q.get("printer", ""), q.get("paper"), q.get("color") == "color",
                          q.get("duplex", "off"), orient)
        a["driver"] = True
    except Exception:
        pass
    return a


@app.get("/api/thumb")
def thumb():
    f, p = get_page()
    if not f:
        return "", 404
    return send_img(compose(f, p, 240, 320, False, rotate=False))


@app.get("/api/view")
def view():
    """Reader: the page as it looks on its own sheet, with the page number."""
    f, p = get_page()
    if not f:
        return "", 404
    if f["kind"] == "pdf":
        with LOCK:
            doc = fitz.open(f["path"])
            try:
                r = doc[p].rect
                wmm, hmm = r.width * 25.4 / 72, r.height * 25.4 / 72
            finally:
                doc.close()
    else:  # an image is shown on an A4 sheet
        wmm, hmm = (297.0, 210.0) if f["lands"][0] else (210.0, 297.0)
    try:
        zq = max(1.0, min(3.0, float(request.args.get("q", 1))))   # sharper picture when zoomed in
    except Exception:
        zq = 1.0
    W = int((1000 if wmm > hmm else 800) * zq)
    ppmm = W / wmm
    H = max(100, int(hmm * ppmm))
    num = numspec(request.args, f["id"], p)
    img = sheet(f, p, W, H, False, "fit", ppmm, 100.0, num, rotate=False)
    return send_img(img)


@app.get("/api/area")
def area():
    return jsonify(get_area(request.args))


@app.get("/api/pageinfo")
def pageinfo():
    """Natural size of a page in mm (used by the Transform tool)."""
    f, p = get_page()
    if not f:
        return "", 404
    try:
        w, h = natural_mm(f, p)
    except Exception as e:
        return jsonify(error=str(e)), 500
    return jsonify(w=w, h=h)


@app.get("/api/src")
def src_img():
    """The page as a flat picture (Transform tool draws and moves copies of it)."""
    f, p = get_page()
    if not f:
        return "", 404
    try:
        px = max(200, min(3000, int(float(request.args.get("px", 1600)))))
    except Exception:
        px = 1600
    return send_img(load_source(f, p, px, False))


@app.get("/api/preview")
def preview():
    f, p = get_page()
    if not f:
        return "", 404
    a = get_area(request.args)
    mode = request.args.get("scale", "fit")
    if mode not in SCALES:
        mode = "fit"
    try:
        W = int(max(480.0, min(1800.0, float(request.args.get("pw", 720)))))   # sharper when zoomed in
    except Exception:
        W = 720
    s = W / a["pw"]  # pixels per mm on the preview sheet
    H = max(100, int(a["ph"] * s))
    ox, oy = int(a["ox"] * s), int(a["oy"] * s)
    AW = max(1, min(W - ox, int(a["aw"] * s)))
    AH = max(1, min(H - oy, int(a["ah"] * s)))
    mono = request.args.get("color") != "color"
    num = numspec(request.args, f["id"], p)
    if request.args.get("blank") == "1":   # Transform tool: empty sheet, the pictures are drawn by the page
        page = Image.new("RGB", (AW, AH), (255, 255, 255))
        if num:
            stamp(page, num, s)
    else:
        page = sheet(f, p, AW, AH, mono, mode, s, get_pct(request.args.get("pct")), num).convert("RGB")
    img = Image.new("RGB", (W, H), (205, 208, 214))  # gray = area the printer cannot print on
    img.paste(page, (ox, oy))
    d = ImageDraw.Draw(img)
    blue = (37, 99, 235)
    x1, y1, x2, y2 = ox, oy, ox + AW - 1, oy + AH - 1
    for x in range(x1, x2, 12):  # dashed printable-area border
        d.line((x, y1, min(x + 6, x2), y1), fill=blue, width=2)
        d.line((x, y2, min(x + 6, x2), y2), fill=blue, width=2)
    for y in range(y1, y2, 12):
        d.line((x1, y, x1, min(y + 6, y2)), fill=blue, width=2)
        d.line((x2, y, x2, min(y + 6, y2)), fill=blue, width=2)
    d.rectangle((0, 0, W - 1, H - 1), outline=(120, 124, 132), width=2)
    return send_img(img)


@app.get("/api/printers")
def printers():
    try:
        return jsonify(printing.list_printers())
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.get("/api/printer")
def printer():
    try:
        return jsonify(printing.caps(request.args.get("name", "")))
    except Exception as e:
        return jsonify(error=str(e)), 500


# ------------------------------------------------------------- profiles
def load_profiles():
    try:
        with open(PROF_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_profiles(d):
    os.makedirs(CFG_DIR, exist_ok=True)
    tmp = PROF_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    os.replace(tmp, PROF_FILE)


def find_profile(printer_name, pid):
    if not pid:
        return None
    with PLOCK:
        for p in load_profiles().get(printer_name, []):
            if p["id"] == pid:
                return p["data"]
    return None


@app.get("/api/profiles")
def profiles_list():
    name = request.args.get("printer", "")
    with PLOCK:
        items = load_profiles().get(name, [])
    return jsonify([{"id": p["id"], "name": p["name"]} for p in items])


@app.post("/api/profiles/new")
def profiles_new():
    """Opens the driver's own settings window (like Word's Printer Properties)."""
    d = request.get_json(silent=True) or {}
    printer_name = str(d.get("printer", ""))
    pname = str(d.get("name", "")).strip()[:40]
    if not printer_name or not pname:
        return jsonify(error="Printer and profile name are required"), 400
    if DIALOG_LOCK.locked():
        return jsonify(error="A settings window is already open"), 409
    base = find_profile(printer_name, d.get("base"))
    jid = uuid.uuid4().hex[:8]
    JOBS[jid] = {"state": "run", "kind": "profile"}

    def run():
        with DIALOG_LOCK:
            try:
                data = printing.driver_dialog(printer_name, base)
                if data is None:
                    JOBS[jid]["state"] = "cancel"
                    return
                pid = uuid.uuid4().hex[:8]
                with PLOCK:
                    allp = load_profiles()
                    allp.setdefault(printer_name, []).append({"id": pid, "name": pname, "data": data})
                    save_profiles(allp)
                JOBS[jid].update(state="done", profile={"id": pid, "name": pname})
            except Exception as e:
                JOBS[jid].update(state="error", error=str(e))

    threading.Thread(target=run, daemon=True).start()
    return jsonify(id=jid)


@app.delete("/api/profiles/<pid>")
def profiles_delete(pid):
    name = request.args.get("printer", "")
    with PLOCK:
        allp = load_profiles()
        allp[name] = [p for p in allp.get(name, []) if p["id"] != pid]
        save_profiles(allp)
    return jsonify(ok=True)


# ---------------------------------------------------------------- print
@app.post("/api/print")
def do_print():
    d = request.get_json(silent=True) or {}
    try:
        pages = [(FILES[x[0]], int(x[1]), (str(x[2]) if len(x) > 2 else None)) for x in d["pages"]]   # 3rd = an added blank page
    except Exception:
        return jsonify(error="Files not found. Add them again."), 400
    if not pages:
        return jsonify(error="No pages selected"), 400
    try:
        copies = max(1, min(99, int(d.get("copies", 1))))
    except Exception:
        copies = 1
    orient = "landscape" if d.get("orient") == "landscape" else "portrait"
    jid = uuid.uuid4().hex[:8]
    JOBS[jid] = {"state": "run", "done": 0, "total": len(pages) * copies}
    color = d.get("color") == "color"
    profile = find_profile(d.get("printer", ""), d.get("profile"))  # None = normal way
    mode = d.get("scale") if d.get("scale") in SCALES else "fit"
    pct = get_pct(d.get("pct"))
    lay = d.get("layout") if isinstance(d.get("layout"), dict) else {}   # only sent when Transform is on
    nd = d.get("num") or {}
    nargs = {"num": "1" if nd.get("on") else "0", "npos": nd.get("pos"), "nalign": nd.get("align"),
             "nsize": nd.get("size"), "nstart": nd.get("start"),
             "nmargin": nd.get("margin"), "nbold": "1" if nd.get("bold") else "0"}

    def page_items(i):
        f, p, vk = pages[i]
        lk = vk or f'{f["id"]}:{p}'
        if lk not in lay:
            return [] if vk else None
        return clean_items(lay[lk], (f, p))   # an empty list = a blank page

    def run():
        try:
            area_mm = get_area({"printer": d["printer"], "paper": d.get("paper"),
                                "color": "color" if color else "mono",
                                "duplex": d.get("duplex", "off"), "orient": orient})["aw"]
            printing.print_job(
                d["printer"], d.get("paper"), color, d.get("duplex", "off"), orient, copies, len(pages),
                lambda i, W, H, m: sheet(pages[i][0], pages[i][1], W, H, m, mode, W / area_mm, pct,
                                         numspec({**nargs, "nn": i + 1} if lay else nargs, pages[i][0]["id"], pages[i][1]),
                                         items=page_items(i)),
                lambda done, total: JOBS[jid].update(done=done, total=total),
                d.get("title", "PC Print"), profile=profile)
            JOBS[jid]["state"] = "done"
        except Exception as e:
            JOBS[jid].update(state="error", error=str(e))

    threading.Thread(target=run, daemon=True).start()
    return jsonify(id=jid)


# ------------------------------------------------- save the merged result as a PDF
SAVE_FILE = os.path.join(CFG_DIR, "save.json")   # kept apart from settings.json: phones may read that one
SAVE_DPI = 200.0


def save_cfg():
    try:
        with open(SAVE_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_cfg_set(**kw):
    d = save_cfg()
    d.update(kw)
    os.makedirs(CFG_DIR, exist_ok=True)
    with open(SAVE_FILE, "w", encoding="utf-8") as fh:
        json.dump(d, fh)


def save_dir_now():
    d = save_cfg().get("dir") or ""
    return d if d and os.path.isdir(d) else ""


@app.get("/api/savedir")
def savedir_get():
    return jsonify(dir=save_dir_now())


@app.post("/api/savedir")
def savedir_set():
    """{pick: true} opens the folder window; {dir: path} sets it directly. The choice is remembered."""
    d = request.get_json(silent=True) or {}
    path = ""
    if d.get("pick"):
        if not HOOKS["pickdir"]:
            return jsonify(error="No folder window"), 500
        try:
            with DIALOG_LOCK:
                path = HOOKS["pickdir"]() or ""
        except Exception as e:
            return jsonify(error=str(e)), 500
        if not path:
            return jsonify(dir=save_dir_now(), cancelled=True)
    else:
        path = str(d.get("dir") or "")
    if not path or not os.path.isdir(path):
        return jsonify(error="That folder does not exist."), 400
    try:
        save_cfg_set(dir=path)
    except Exception as e:
        return jsonify(error=str(e)), 500
    return jsonify(dir=path)


@app.post("/api/savedir/open")
def savedir_open():
    d = save_dir_now()
    if not d:
        return jsonify(error="No save folder chosen yet."), 400
    try:
        os.startfile(d)
    except Exception as e:
        return jsonify(error=str(e)), 500
    return jsonify(ok=True)


def safe_pdf_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(name or "")).strip(" .")
    name = re.sub(r"\s+", " ", name)
    if name.lower().endswith(".pdf"):
        name = name[:-4].rstrip(" .")
    if not name:
        name = "Merged " + time.strftime("%Y-%m-%d %H-%M-%S")
    return name[:120]


def unique_path(folder, base):
    path = os.path.join(folder, base + ".pdf")
    n = 2
    while os.path.exists(path):
        path = os.path.join(folder, "%s (%d).pdf" % (base, n))
        n += 1
    return path


@app.post("/api/savepdf")
def save_pdf():
    """Same pages, same order, same layout, page numbers and Transform changes as printing - but into one PDF file
    in the chosen folder (page size = the paper, the picture sits where the printer would put it)."""
    d = request.get_json(silent=True) or {}
    try:
        pages = [(FILES[x[0]], int(x[1]), (str(x[2]) if len(x) > 2 else None)) for x in d["pages"]]
    except Exception:
        return jsonify(error="Files not found. Add them again."), 400
    if not pages:
        return jsonify(error="No pages selected"), 400
    folder = str(d.get("dir") or "") or save_dir_now()
    if not folder or not os.path.isdir(folder):
        return jsonify(error="nodir"), 400
    orient = "landscape" if d.get("orient") == "landscape" else "portrait"
    color = d.get("color") == "color"
    mode = d.get("scale") if d.get("scale") in SCALES else "fit"
    pct = get_pct(d.get("pct"))
    lay = d.get("layout") if isinstance(d.get("layout"), dict) else {}
    nd = d.get("num") or {}
    nargs = {"num": "1" if nd.get("on") else "0", "npos": nd.get("pos"), "nalign": nd.get("align"),
             "nsize": nd.get("size"), "nstart": nd.get("start"),
             "nmargin": nd.get("margin"), "nbold": "1" if nd.get("bold") else "0"}
    area_q = {"printer": d.get("printer", ""), "paper": d.get("paper"), "color": "color" if color else "mono",
              "duplex": "off", "orient": orient, "w": d.get("w", 210), "h": d.get("h", 297)}
    base = safe_pdf_name(d.get("name"))
    jid = uuid.uuid4().hex[:8]
    JOBS[jid] = {"state": "run", "done": 0, "total": len(pages)}

    def page_items(i):
        f, p, vk = pages[i]
        lk = vk or f'{f["id"]}:{p}'
        if lk not in lay:
            return [] if vk else None
        return clean_items(lay[lk], (f, p))

    def run():
        tmp = None
        try:
            a = get_area(area_q)
            sc = SAVE_DPI / 25.4                              # pixels per mm
            W, H = max(1, round(a["pw"] * sc)), max(1, round(a["ph"] * sc))
            ox, oy = round(a["ox"] * sc), round(a["oy"] * sc)
            AW, AH = max(1, min(W - ox, round(a["aw"] * sc))), max(1, min(H - oy, round(a["ah"] * sc)))
            pt_w, pt_h = a["pw"] / 25.4 * 72.0, a["ph"] / 25.4 * 72.0
            with LOCK:
                doc = fitz.open()
            try:
                for i, (f, p, vk) in enumerate(pages):
                    num = numspec({**nargs, "nn": i + 1} if lay else nargs, f["id"], p)
                    pg = sheet(f, p, AW, AH, not color, mode, sc, pct, num, items=page_items(i))
                    paper = Image.new(pg.mode, (W, H), 255 if pg.mode == "L" else (255, 255, 255))
                    paper.paste(pg, (ox, oy))
                    b = io.BytesIO()
                    paper.save(b, "JPEG", quality=90, optimize=True, dpi=(SAVE_DPI, SAVE_DPI))
                    del pg, paper
                    with LOCK:
                        page = doc.new_page(width=pt_w, height=pt_h)
                        page.insert_image(page.rect, stream=b.getvalue())
                    JOBS[jid].update(done=i + 1)
                path = unique_path(folder, base)
                tmp = path + ".part"
                with LOCK:
                    doc.save(tmp, deflate=True, garbage=3)
            finally:
                with LOCK:
                    doc.close()
            os.replace(tmp, path)
            tmp = None
            JOBS[jid].update(state="done", path=path)
        except Exception as e:
            JOBS[jid].update(state="error", error=str(e))
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    threading.Thread(target=run, daemon=True).start()
    return jsonify(id=jid)



@app.get("/api/job/<jid>")
def job(jid):
    return jsonify(JOBS.get(jid, {"state": "error", "error": "unknown job"}))
