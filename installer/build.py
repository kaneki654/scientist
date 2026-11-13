"""
Build Scientist-SETUP.exe

    python installer/build.py [--scrcpy PATH_TO_SCRCPY_FOLDER]

Steps
  1. icon            assets/scientist.ico (generated with Pillow)
  2. app             PyInstaller one-folder build of Scientist.py  → build/app-dist/Scientist/
  0. phone helpers   tools/helper (wallpaper) and tools/tether (reverse-tether client APK) are rebuilt and
                     embedded into Scientist.py when the Android SDK + JDK are available
  3. stage           app + tools/scrcpy (scrcpy + adb) + licenses
  4. payload         build/payload.zip (+ manifest.json)
  5. setup           PyInstaller one-file build of installer/setup.py with the payload inside
                     → dist/Scientist-SETUP.exe

scrcpy is taken from --scrcpy, else from the scrcpy found in PATH, else downloaded from the official
GitHub release. Reverse tethering (relay + client) is built into the app — nothing else is downloaded.
Requires: PyInstaller, Pillow.
"""
import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BUILD = HERE / "build"
DIST = ROOT / "dist"
CACHE = BUILD / "cache"
ASSETS = ROOT / "assets"
ICON = ASSETS / "scientist.ico"

SCRCPY_URL = "https://github.com/Genymobile/scrcpy/releases/download/v4.1/scrcpy-win64-v4.1.zip"
SCRCPY_SKIP = {"open_a_terminal_here.bat", "scrcpy-noconsole.vbs"}


try:  # Windows consoles default to cp1252; keep the build log printable everywhere
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def step(msg):
    print(f"\n=== {msg}")


def app_version():
    m = re.search(r'^APP_VERSION = "([^"]+)"', (ROOT / "Scientist.py").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else "0.0.0"


def download(url, dest):
    if dest.exists():
        print(f"cached: {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {url}")
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    tmp.rename(dest)
    return dest


def make_icon():
    if ICON.exists():
        return
    from PIL import Image, ImageDraw
    ASSETS.mkdir(exist_ok=True)
    n = 512
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = n / 2

    def hexagon(r):
        return [(c + r * math.cos(math.radians(60 * i - 90)), c + r * math.sin(math.radians(60 * i - 90)))
                for i in range(6)]
    d.polygon(hexagon(n * 0.48), fill=(11, 8, 20, 255))
    d.polygon(hexagon(n * 0.48), outline=(168, 85, 247, 255), width=int(n * 0.06))
    d.polygon(hexagon(n * 0.21), fill=(201, 155, 255, 255))
    img.save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"icon → {ICON}")


def pyinstaller(*args):
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN", *args]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)


def build_app():
    pyinstaller("--windowed", "--name", "Scientist", "--icon", str(ICON),
                "--distpath", str(BUILD / "app-dist"), "--workpath", str(BUILD / "work-app"),
                "--specpath", str(BUILD),
                "--hidden-import", "PIL.Image", "--hidden-import", "PIL.ImageDraw",
                "--hidden-import", "PIL.ImageFont", "--hidden-import", "PIL.ImageFilter",
                "--hidden-import", "PIL.ImageChops", "--hidden-import", "PIL.ImageGrab",
                # FB Daily: fb.py (loaded from disk next to the exe) needs uiautomator2 + Windows OCR
                "--collect-data", "uiautomator2", "--collect-submodules", "uiautomator2",
                "--collect-submodules", "winrt", "--collect-binaries", "winrt",
                "--exclude-module", "numpy", "--exclude-module", "matplotlib",
                str(ROOT / "Scientist.py"))
    return BUILD / "app-dist" / "Scientist"


def find_scrcpy(explicit):
    if explicit:
        p = Path(explicit)
        if (p / "scrcpy.exe").exists():
            return p
        sys.exit(f"--scrcpy: scrcpy.exe not found in {p}")
    found = shutil.which("scrcpy")
    if found:
        p = Path(found).resolve().parent
        if (p / "scrcpy-server").exists() and (p / "adb.exe").exists():
            print(f"using local scrcpy: {p}")
            return p
    z = download(SCRCPY_URL, CACHE / Path(SCRCPY_URL).name)
    out = CACHE / "scrcpy"
    if not out.exists():
        with zipfile.ZipFile(z) as zf:
            zf.extractall(out)
    return next(out.rglob("scrcpy.exe")).parent


def stage(app_dir, scrcpy_dir):
    st = BUILD / "stage"
    if st.exists():
        shutil.rmtree(st)
    shutil.copytree(app_dir, st)
    (st / "licenses").mkdir()
    # scrcpy + adb
    dst = st / "tools" / "scrcpy"
    dst.mkdir(parents=True)
    for f in scrcpy_dir.iterdir():
        if f.is_file() and f.name not in SCRCPY_SKIP:
            shutil.copy2(f, dst / f.name)
    if (scrcpy_dir / "LICENSE.txt").exists():
        shutil.copy2(scrcpy_dir / "LICENSE.txt", st / "licenses" / "scrcpy-LICENSE.txt")
    # wallpaper helper (also embedded in the app; kept next to it for transparency)
    helper = ROOT / "tools" / "helper" / "scientist-helper.jar"
    if helper.exists():
        (st / "tools" / "helper").mkdir(parents=True, exist_ok=True)
        shutil.copy2(helper, st / "tools" / "helper" / helper.name)
    # reverse-tether client APK (also embedded in the app; kept next to it like the helper)
    tether = ROOT / "tools" / "tether" / "scientist-tether.apk"
    if tether.exists():
        (st / "tools" / "tether").mkdir(parents=True, exist_ok=True)
        shutil.copy2(tether, st / "tools" / "tether" / tether.name)
    fb_src = ROOT / "fbdaily"
    if fb_src.exists():
        # engine + config only: skip FB Daily's own PyInstaller output, caches, logs and per-phone state
        shutil.copytree(fb_src, st / "fbdaily", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("build", "dist", "__pycache__", "*.log", "*.spec",
                                                      "state.json", "STOP",
                                                      # Python-only launchers of the standalone app:
                                                      # the installed SCIENTIST runs FB Daily itself
                                                      "gui.py", "fbdailyMan.py", "*.bat"))
    fb_reactor = ROOT / "fb.py"
    if fb_reactor.exists():
        shutil.copy2(fb_reactor, st / "fb.py")
    return st


def make_payload(st, version):
    payload = BUILD / "payload.zip"
    files = [f for f in st.rglob("*") if f.is_file()]
    size = sum(f.stat().st_size for f in files)
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("manifest.json", json.dumps({"version": version, "size": size, "files": len(files)}))
        for f in files:
            z.write(f, f.relative_to(st).as_posix())
    print(f"payload: {len(files)} files, {size / 1e6:.1f} MB → {payload.stat().st_size / 1e6:.1f} MB zipped")
    return payload


def build_uninstaller():
    """Same program as the setup, but without the payload: small enough to leave in the install folder."""
    pyinstaller("--onefile", "--windowed", "--name", "uninstall", "--icon", str(ICON),
                "--distpath", str(BUILD / "uninst-dist"), "--workpath", str(BUILD / "work-uninst"),
                "--specpath", str(BUILD), "--paths", str(ROOT),
                "--exclude-module", "PIL", "--exclude-module", "numpy",
                str(HERE / "setup.py"))
    return BUILD / "uninst-dist" / "uninstall.exe"


def build_setup(payload):
    pyinstaller("--onefile", "--windowed", "--name", "Scientist-SETUP", "--icon", str(ICON),
                "--distpath", str(DIST), "--workpath", str(BUILD / "work-setup"), "--specpath", str(BUILD),
                "--paths", str(ROOT), "--add-data", f"{payload};.",
                "--exclude-module", "PIL", "--exclude-module", "numpy",
                str(HERE / "setup.py"))
    return DIST / "Scientist-SETUP.exe"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scrcpy", help="folder containing scrcpy.exe, adb.exe and scrcpy-server")
    ap.add_argument("--setup-only", action="store_true", help="reuse build/payload.zip, rebuild only the setup exe")
    a = ap.parse_args()
    version = app_version()
    if a.setup_only and (BUILD / "payload.zip").exists():
        step("setup only (reusing build/payload.zip)")
        exe = build_setup(BUILD / "payload.zip")
        print(f"\nBUILT  {exe}  ({exe.stat().st_size / 1e6:.1f} MB)")
        return
    step(f"SCIENTIST {version}: phone helpers (wallpaper helper + tether client)")
    for script in ("helper", "tether"):
        try:
            subprocess.run([sys.executable, str(ROOT / "tools" / script / "build.py")], check=True, cwd=ROOT)
        except Exception as e:  # no Android SDK / JDK here: keep the copies already embedded in Scientist.py
            print(f"skipped rebuilding tools/{script} ({e}); using the embedded copy")
    step("icon")
    make_icon()
    step("app (PyInstaller, one folder)")
    app_dir = build_app()
    step("uninstaller (PyInstaller, one file, no payload)")
    uninst = build_uninstaller()
    step("stage app + scrcpy/adb")
    st = stage(app_dir, find_scrcpy(a.scrcpy))
    shutil.copy2(uninst, st / "uninstall.exe")
    step("payload")
    payload = make_payload(st, version)
    step("setup (PyInstaller, one file)")
    exe = build_setup(payload)
    print(f"\nBUILT  {exe}  ({exe.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
