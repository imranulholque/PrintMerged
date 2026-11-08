"""Tests the update logic (no window, no internet): python test_updater.py"""
import hashlib
import http.server
import json
import os
import tempfile
import threading
import zipfile

import updater

REPO = "me/PrintMerged"
URL = "https://github.com/%s/releases/download/build-12/PCPrint-windows.zip" % REPO


def rel(tag="build-12", **kw):
    d = {"tag_name": tag, "name": "Build 12", "body": "fix things", "html_url": "https://github.com/x",
         "assets": [{"name": updater.ASSET, "browser_download_url": URL, "size": 5}]}
    d.update(kw)
    return d


def make_zip(path, names):
    with zipfile.ZipFile(path, "w") as z:
        for n in names:
            z.writestr(n, "x")


def main():
    # --- deciding
    assert updater.parse_release(rel(), REPO, 11)["build"] == 12
    assert updater.parse_release(rel(), REPO, 12) is None            # same build
    assert updater.parse_release(rel(), REPO, 13) is None            # older release
    assert updater.parse_release(rel("v1.0"), REPO, 1) is None       # not one of ours
    assert updater.parse_release(rel(prerelease=True), REPO, 1) is None
    assert updater.parse_release(rel(draft=True), REPO, 1) is None
    assert updater.parse_release(rel(assets=[]), REPO, 1) is None    # zip not attached yet
    bad = rel(assets=[{"name": updater.ASSET, "browser_download_url": "https://evil.example/x.zip"}])
    try:
        updater.parse_release(bad, REPO, 1)
        raise SystemExit("foreign download address accepted")
    except updater.UpdateError:
        pass
    sha = "a" * 64
    r = updater.parse_release(rel(assets=[{"name": updater.ASSET, "browser_download_url": URL, "digest": "sha256:" + sha.upper()}]), REPO, 1)
    assert r["sha256"] == sha

    # --- stage
    d = tempfile.mkdtemp()
    make_zip(d + "/a.zip", ["PCPrint.exe"])
    assert updater.stage(d + "/a.zip", d + "/sa") == os.path.join(d, "sa", "PCPrint.exe")
    make_zip(d + "/b.zip", ["PCPrint/PCPrint.exe"])
    assert updater.stage(d + "/b.zip", d + "/sb") == os.path.join(d, "sb", "PCPrint", "PCPrint.exe")
    make_zip(d + "/c.zip", ["other.txt"])
    for z, why in (("c.zip", "no exe"),):
        try:
            updater.stage(d + "/" + z, d + "/sc")
            raise SystemExit("accepted zip with " + why)
        except updater.UpdateError:
            pass
    open(d + "/d.zip", "wb").write(b"not a zip")
    try:
        updater.stage(d + "/d.zip", d + "/sd")
        raise SystemExit("accepted a broken zip")
    except updater.UpdateError:
        pass

    # --- local fake GitHub: api + download
    payload = open(d + "/a.zip", "rb").read()
    state = {"json": rel(assets=[{"name": updater.ASSET, "browser_download_url": URL}])}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.endswith("/releases/latest"):
                if state.get("code"):
                    self.send_error(state["code"])
                    return
                body = json.dumps(state["json"]).encode()
            else:
                body = payload
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_port
    updater.API_BASE = base
    assert updater.check(REPO, 11)["build"] == 12
    assert updater.check(REPO, 12) is None
    state["code"] = 404
    assert updater.check(REPO, 1) is None                            # no release yet
    state["code"] = 500
    try:
        updater.check(REPO, 1)
        raise SystemExit("500 not reported")
    except updater.UpdateError:
        pass
    try:
        updater.check("", 1)
        raise SystemExit("empty repo not reported")
    except updater.UpdateError:
        pass

    seen = []
    ok_sha = hashlib.sha256(payload).hexdigest()
    updater.download(base + "/f.zip", d + "/dl.zip", len(payload), ok_sha, lambda a, b: seen.append((a, b)))
    assert open(d + "/dl.zip", "rb").read() == payload and seen[-1][0] == len(payload)
    for kw, what in (({"sha256": "0" * 64}, "checksum"), ({"size": len(payload) + 1}, "size")):
        try:
            updater.download(base + "/f.zip", d + "/dl2.zip", **kw)
            raise SystemExit("bad %s accepted" % what)
        except updater.UpdateError:
            pass
    try:
        updater.download(base + "/f.zip", d + "/dl3.zip", cancelled=lambda: True)
        raise SystemExit("cancel ignored")
    except updater.UpdateError as e:
        assert str(e) == "cancelled"

    # --- whole step
    updater.WORK_DIR = os.path.join(d, "work")
    new_exe = updater.download_and_stage({"url": base + "/f.zip", "size": len(payload), "sha256": ok_sha})
    assert os.path.isfile(new_exe) and new_exe.endswith("PCPrint.exe")

    # --- installer script (text only; it runs on Windows)
    bat = updater.installer_script(r"C:\Temp\new\PCPrint.exe", r"C:\Tools\Print 100%\PCPrint.exe", 4321)
    assert 'PID eq 4321' in bat and 'IMAGENAME eq PCPrint.exe' in bat
    assert 'copy /Y "C:\\Temp\\new\\PCPrint.exe" "C:\\Tools\\Print 100%%\\PCPrint.exe"' in bat
    assert 'start "" "C:\\Tools\\Print 100%%\\PCPrint.exe"' in bat and "\r\n" in bat
    assert "if %n% GTR 60 goto failed" in bat
    print("updater tests: all passed")


main()
