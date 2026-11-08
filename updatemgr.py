"""Auto update, server part: background check, state for the page, download + install.
Setting 'auto_update' (Updates menu in the top bar): ON = the program looks for a new build by itself, tells you, asks,
then installs. OFF = it never looks and never shows anything about updates (only 'Check for updates now' still works).

The routes live in a Flask blueprint (registered in server.py). They are PC-only: server.py lists /api/update in
LOCAL_ONLY, so a phone can never start an update. app.py fills HOOKS (quit, tray notification) and calls start()."""
import json
import os
import threading
import time

from flask import Blueprint, jsonify, request

import updater

bp = Blueprint("update", __name__)
HOOKS = {"quit": None, "notify": None, "busy": None, "files": None}   # set by app.py / server.py
CHECK_EVERY = 5 * 60               # seconds, while the program stays open (GitHub allows 60 asks / hour, this is 12)
FIRST_CHECK = 10                   # a few seconds after start, so start-up stays fast
CFG_DIR = os.path.dirname(updater.LOG_FILE)       # %APPDATA%\PCPrint, same folder as the other settings
CFG_FILE = os.path.join(CFG_DIR, "update.json")

LOCK = threading.Lock()
CANCEL = threading.Event()
NOTIFIED = set()                   # builds we already showed a tray message for in this run
S = {"info": None, "state": "idle", "done": 0, "total": 0, "error": "", "msg": "", "checking": False}
_STARTED = {"v": False}


# ---------------------------------------------------------------- setting
def enabled():
    try:
        with open(CFG_FILE, encoding="utf-8") as fh:
            return bool(json.load(fh).get("auto_update", True))
    except Exception:
        return True


def set_enabled(on):
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(CFG_FILE, "w", encoding="utf-8") as fh:
            json.dump({"auto_update": bool(on)}, fh)
    except Exception:
        pass
    if on:
        request_check(False)
    else:
        with LOCK:
            if S["state"] not in ("downloading", "ready"):
                S["info"] = None               # nothing about updates is shown any more
                S["msg"] = ""


def _hook(name, *args, default=None):
    h = HOOKS.get(name)
    if not h:
        return default
    try:
        return h(*args)
    except Exception:
        return default


# --------------------------------------------------------------- checking
def request_check(manual):
    """Starts a check in the background. Returns False when one is already running or an update is being installed."""
    with LOCK:
        if S["checking"] or S["state"] in ("downloading", "ready"):
            return False
        if not manual and not (enabled() and updater.can_self_update()):
            return False
        S["checking"] = True
        S["msg"] = ""
    threading.Thread(target=_run_check, args=(manual,), daemon=True).start()
    return True


def _run_check(manual):
    info, err = None, ""
    try:
        info = updater.check()
    except Exception as e:
        err = str(e)
    notify = None
    with LOCK:
        S["checking"] = False
        if not manual and not enabled():
            return                                   # switched off while the check was running: say nothing
        if err:
            S["msg"] = err if manual else ""
            return
        S["info"] = info
        if info is None:
            S["msg"] = ("You have the newest version (%s)." % updater.version_text()) if manual else ""
        elif not manual and info["build"] not in NOTIFIED:
            NOTIFIED.add(info["build"])
            notify = info["build"]
    if notify is not None:
        _hook("notify", "PC Print update", "A new version (build %d) is ready. Open PC Print to install it." % notify)


def _loop():
    time.sleep(FIRST_CHECK)
    while True:
        request_check(False)
        time.sleep(CHECK_EVERY)


def start():
    """Called once from app.py after the window is built."""
    if _STARTED["v"]:
        return
    _STARTED["v"] = True
    if updater.is_frozen():
        updater.discard()                            # leftovers of the last update
    if updater.can_self_update():
        threading.Thread(target=_loop, daemon=True).start()


# ------------------------------------------------------------- installing
def _busy():
    return bool(_hook("busy", default=False))


def _install(info):
    def progress(done, total):
        S["done"], S["total"] = done, total

    try:
        new_exe = updater.download_and_stage(info, progress, CANCEL.is_set)
    except Exception as e:
        text = str(e)
        updater.log("update failed: " + text)
        with LOCK:
            S.update(state="idle" if text == "cancelled" else "error",
                     error="" if text == "cancelled" else text)
        return
    if _busy():                                      # a print job started while we were downloading
        updater.discard()
        with LOCK:
            S.update(state="error", error="A print job is running. Try again when it is finished.")
        return
    quit_fn = HOOKS.get("quit")
    if quit_fn is None:                              # checked BEFORE the installer starts, so it never waits for nothing
        updater.discard()
        with LOCK:
            S.update(state="error", error="The program could not close itself.")
        return
    with LOCK:
        S["state"] = "ready"
    try:
        updater.launch_installer(new_exe)
    except Exception as e:
        updater.log("could not start the installer: %s" % e)
        updater.discard()
        with LOCK:
            S.update(state="error", error="Could not start the installer (%s)." % e)
        return
    try:
        quit_fn()                                    # the installer script waits until the program is gone
    except Exception as e:
        updater.log("could not close the program: %s" % e)


def snapshot():
    can = updater.can_self_update()
    with LOCK:
        info = S["info"]
        pub = None if info is None else {k: info[k] for k in ("build", "notes", "page", "size")}
        return {"enabled": enabled(), "can": can, "version": updater.version_text(),
                "writable": updater.folder_writable() if can else True,
                "info": pub, "state": S["state"], "done": S["done"], "total": S["total"],
                "error": S["error"], "msg": S["msg"], "checking": S["checking"],
                "busy": _busy(), "files": int(_hook("files", default=0) or 0)}


# ------------------------------------------------------------------ routes
@bp.get("/api/update")
def route_state():
    return jsonify(snapshot())


@bp.post("/api/update/enabled")
def route_enabled():
    set_enabled(bool((request.get_json(silent=True) or {}).get("on")))
    return jsonify(snapshot())


@bp.post("/api/update/check")
def route_check():
    request_check(True)
    return jsonify(snapshot())


@bp.post("/api/update/cancel")
def route_cancel():
    CANCEL.set()
    return jsonify(snapshot())


@bp.post("/api/update/install")
def route_install():
    if not updater.can_self_update():
        return jsonify(error="This copy cannot install updates by itself."), 400
    if not updater.folder_writable():
        return jsonify(error="The program folder is write-protected. Move PCPrint.exe to a normal folder "
                             "(for example C:\\Tools\\PCPrint)."), 400
    if _busy():
        return jsonify(error="A print job is running. Try again when it is finished."), 409
    with LOCK:
        info = S["info"]
        if info is None:
            return jsonify(error="There is no update to install."), 400
        if S["state"] in ("downloading", "ready"):
            return jsonify(ok=True)                  # already on its way
        S.update(state="downloading", done=0, total=info.get("size", 0), error="")
        CANCEL.clear()
    threading.Thread(target=_install, args=(info,), daemon=True).start()
    return jsonify(snapshot())
