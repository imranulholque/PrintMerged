"""Auto update for PC Print: finds a newer build on GitHub, downloads it and swaps PCPrint.exe.
No web / window code in here (so it can be tested alone). The routes are in server.py, the screen part in index.html.

How it works
  * The GitHub build (build.yml) publishes every finished build as a Release called  build-<number>  with the file
    PCPrint-windows.zip (it holds PCPrint.exe), and writes the number + repository into buildinfo.py before packing.
  * The program asks   https://api.github.com/repos/<repo>/releases/latest   and compares the number with its own.
  * Install = download the zip, unpack it in the temp folder, close the program, a small .bat waits until the old
    PCPrint.exe is gone, copies the new one over it and starts it again.
    Settings, printer profiles and the phone PIN live in %APPDATA%\\PCPrint, so they stay."""
import hashlib
import json
import ntpath
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

try:
    import buildinfo
    BUILD = int(buildinfo.BUILD)
    REPO = str(buildinfo.REPO)
except Exception:
    BUILD, REPO = 0, ""

API_BASE = "https://api.github.com"
ASSET = "PCPrint-windows.zip"
EXE_NAME = "PCPrint.exe"
TAG_RE = re.compile(r"^build-(\d+)$")
WORK_DIR = os.path.join(tempfile.gettempdir(), "PCPrintUpdate")
LOG_FILE = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "PCPrint", "update.log")


class UpdateError(Exception):
    pass


def log(text):
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception:
        pass


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def exe_path():
    return os.path.abspath(sys.executable)


def install_dir():
    return os.path.dirname(exe_path())


def can_self_update():
    """True only for the packed Windows program that knows its GitHub repository."""
    return sys.platform == "win32" and is_frozen() and bool(REPO) and BUILD > 0


def version_text():
    return "build %d" % BUILD if BUILD else "development copy"


# ------------------------------------------------------------------ checking
def parse_release(data, repo, current):
    """GitHub 'latest release' json -> dict about the update, or None when there is nothing newer to install."""
    m = TAG_RE.match(str(data.get("tag_name", "")))
    if not m or data.get("draft") or data.get("prerelease"):
        return None
    build = int(m.group(1))
    if build <= current:
        return None
    for a in data.get("assets") or []:
        if a.get("name") != ASSET:
            continue
        url = str(a.get("browser_download_url", ""))
        if not url.startswith("https://github.com/%s/releases/download/" % repo):
            raise UpdateError("The update has an unexpected download address.")
        digest = str(a.get("digest") or "")
        return {"build": build, "tag": data["tag_name"], "name": data.get("name") or data["tag_name"],
                "notes": (data.get("body") or "").strip(), "url": url, "size": int(a.get("size") or 0),
                "sha256": digest[7:].lower() if digest.startswith("sha256:") else "",
                "page": data.get("html_url") or "https://github.com/%s/releases" % repo}
    return None                                    # release still being uploaded: ask again next time


def check(repo=None, current=None, timeout=15):
    """Asks GitHub. Returns the update dict, or None = you are up to date. Raises UpdateError when it cannot ask."""
    repo = REPO if repo is None else repo
    current = BUILD if current is None else current
    if not repo:
        raise UpdateError("This copy was not made by the GitHub build, so it does not know where to look for updates.")
    req = urllib.request.Request("%s/repos/%s/releases/latest" % (API_BASE, repo),
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "PCPrint-updater"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None                            # no release published yet
        raise UpdateError("GitHub answered %s %s." % (e.code, e.reason))
    except Exception as e:
        raise UpdateError("Could not reach GitHub (%s)." % (getattr(e, "reason", None) or e))
    return parse_release(data, repo, current)


# --------------------------------------------------------------- downloading
def download(url, dest, size=0, sha256="", progress=None, cancelled=None):
    """Streams url to dest. progress(done, total). cancelled() -> True stops it."""
    req = urllib.request.Request(url, headers={"User-Agent": "PCPrint-updater"})
    h = hashlib.sha256()
    done = 0
    try:
        with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or size or 0)
            while True:
                if cancelled is not None and cancelled():
                    raise UpdateError("cancelled")
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
    except UpdateError:
        raise
    except Exception as e:
        raise UpdateError("Download failed (%s)." % (getattr(e, "reason", None) or e))
    if size and done != size:
        raise UpdateError("The download is incomplete (%d of %d bytes)." % (done, size))
    if sha256 and h.hexdigest() != sha256.lower():
        raise UpdateError("The downloaded file is damaged (checksum does not match).")


def stage(zip_path, stage_dir):
    """Unpacks the zip into stage_dir and returns the full path of the new PCPrint.exe."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            if z.testzip() is not None:
                raise UpdateError("The downloaded file is damaged.")
            z.extractall(stage_dir)                # zipfile drops '..' and absolute parts itself
    except zipfile.BadZipFile:
        raise UpdateError("The downloaded file is not a valid zip.")
    direct = os.path.join(stage_dir, EXE_NAME)
    if os.path.isfile(direct):
        return direct
    for name in os.listdir(stage_dir):
        sub = os.path.join(stage_dir, name, EXE_NAME)
        if os.path.isfile(sub):
            return sub
    raise UpdateError("The update does not contain %s." % EXE_NAME)


def download_and_stage(info, progress=None, cancelled=None):
    """Whole download step. Returns the path of the new exe (ready to be copied over the running one)."""
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    os.makedirs(WORK_DIR, exist_ok=True)
    zip_path = os.path.join(WORK_DIR, ASSET)
    try:
        download(info["url"], zip_path, info.get("size", 0), info.get("sha256", ""), progress, cancelled)
        new_exe = stage(zip_path, os.path.join(WORK_DIR, "new"))
    except Exception:
        shutil.rmtree(WORK_DIR, ignore_errors=True)
        raise
    try:
        os.remove(zip_path)
    except OSError:
        pass
    return new_exe


def discard():
    shutil.rmtree(WORK_DIR, ignore_errors=True)


# ---------------------------------------------------------------- installing
def _bat_quote(path):
    return path.replace("%", "%%")


def installer_script(new_exe, target_exe, pid):
    """Text of the .bat. It waits for the old program to close, copies the new exe over it (retrying while Windows
    still holds the file), then starts it. The one-file exe has a small launcher process that closes a moment after
    the program itself, so the copy is retried for up to ~60 seconds."""
    exe_name = ntpath.basename(target_exe)
    q = _bat_quote
    return "\r\n".join([
        "@echo off",
        "chcp 65001 >nul",
        'set "LOG=%s"' % q(LOG_FILE),
        'echo %%date%% %%time%%  installer: waiting for the program (pid %d) to close >> "%%LOG%%"' % pid,
        "set n=0",
        ":wait",
        'tasklist /FI "PID eq %d" /FI "IMAGENAME eq %s" /NH 2>NUL | find /I "%s" >NUL' % (pid, exe_name, exe_name),
        "if errorlevel 1 goto copy",
        "set /a n+=1",
        "if %n% GTR 120 goto stuck",
        "timeout /t 1 /nobreak >NUL",
        "goto wait",
        ":copy",
        "set n=0",
        ":again",
        'copy /Y "%s" "%s" >NUL 2>&1' % (q(new_exe), q(target_exe)),
        "if not errorlevel 1 goto copied",
        "set /a n+=1",
        "if %n% GTR 60 goto failed",
        "timeout /t 1 /nobreak >NUL",
        "goto again",
        ":copied",
        'echo %date% %time%  installer: new program copied >> "%LOG%"',
        "goto start",
        ":failed",
        'echo %date% %time%  installer: COPY FAILED (is the folder writable?) >> "%LOG%"',
        ":start",
        'start "" "%s"' % q(target_exe),
        'rd /s /q "%s"' % q(WORK_DIR),
        "exit /b 0",
        ":stuck",
        'echo %date% %time%  installer: program did not close, update not installed >> "%LOG%"',
        "exit /b 1",
        "",
    ])


def launch_installer(new_exe):
    """Starts the copy script and returns. The program must exit right after this."""
    bat = os.path.join(WORK_DIR, "install.bat")
    with open(bat, "w", encoding="utf-8", newline="") as f:
        f.write(installer_script(new_exe, exe_path(), os.getpid()))
    env = dict(os.environ)
    env.pop("_MEIPASS2", None)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"      # the new one-file exe must unpack itself fresh, not reuse our temp folder
    flags = 0x08000000 | 0x00000200      # hidden console + own process group (NOT detached: a detached script opens a window for every command it runs)
    subprocess.Popen(["cmd", "/c", bat], creationflags=flags, close_fds=True, env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log("update installer launched")


def folder_writable():
    return os.access(install_dir(), os.W_OK)
