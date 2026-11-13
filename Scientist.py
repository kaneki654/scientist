#!/usr/bin/env python3
"""
SCIENTIST // ADB Multi-Mirror  -  cyberpunk edition

- Multi-select devices (click to toggle, double-click to mirror just that one)
- Mirror many devices at once, embedded in a responsive grid (S / M / L tiles)
- Per-mirror toolbar: screen OFF/ON, Back, Home, Recents, Screenshot, Reconnect
- Batch screen OFF/ON for every live mirror, or direct ADB on selected devices
- scrcpy options: stay awake, screen off on connect, mute audio, show touches,
  resolution cap, FPS cap
- Device info (model, Android version, battery) and live hot-plug detection
- ADB toolkit: Wi-Fi connect, enable TCP/IP, run shell commands on selected devices
- Colour-coded log, toasts, keyboard shortcuts, settings remembered between runs
- Native window embedding through ctypes (pywin32 is no longer required)
- Everything is stopped automatically on close

Phone-farm features
- Persistent unit numbers (#01, #02 …) + nicknames; right-click a device to edit
- Numbered SCIENTIST-style wallpapers pushed and set over ADB (no root, no taps; needs Pillow)
- Reverse tethering: phones use the PC's internet over USB (built-in relay + client, no taps on the phone)
- Ctrl+scroll anywhere (even over mirrors) resizes tiles; FIT sizes them to show every phone
- Tiles follow each phone's real screen ratio
- Farm actions on selected phones: wake / lock / home / back, brightness, install APK,
  push files, launch app or URL, force-stop, reboot
- Health: battery temperature per phone, overheat warnings, hottest-phone header chip
- Automation: auto-mirror new devices, auto-reconnect lost links, auto-tether new devices
- FB Daily: human-like Facebook Lite activity on USB phones (engine in fbdaily/fbdaily.py)
- Settings (gear, top right): software colour, show / hide system log, 12 / 24-hour clock
- Time clock: worker time in / out, break and lunch with overbreak + late alerts, screenshot proofs;
  admin options (password) set the shift, the break allowance and the worker list
"""

import base64
import hashlib
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.request
import zipfile
from datetime import datetime, timedelta as _td
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox
import colorsys
import importlib.util
import random      # noqa: F401  (used by the FB Daily engine; keeps it in the frozen build)
import signal      # noqa: F401  (same)

import scirelay

IS_WIN = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0

if IS_WIN:
    import ctypes
    from ctypes import wintypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

APP_VERSION = "2.3.0"
FROZEN = getattr(sys, "frozen", False)          # True when running as the installed Scientist.exe
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
BUNDLED_SCRCPY = APP_DIR / "tools" / "scrcpy"   # installed by Scientist-SETUP.exe (scrcpy + adb)
if (BUNDLED_SCRCPY / "adb.exe").exists() or (BUNDLED_SCRCPY / "scrcpy.exe").exists():
    os.environ["PATH"] = str(BUNDLED_SCRCPY) + os.pathsep + os.environ.get("PATH", "")
SHOT_DIR = APP_DIR / "screenshots"
WALL_DIR = APP_DIR / "wallpapers"
TOOLS_DIR = APP_DIR / "tools"
SETTINGS_PATH = Path.home() / ".scientist_mirror.json"
HELPER_REMOTE = "/data/local/tmp/scientist-helper.jar"
FBDAILY_DIR = APP_DIR / "fbdaily"               # FB Daily engine (fbdaily/fbdaily.py + config.json)
FB_PACKAGE = "com.facebook.lite"
# Mirrors: each scrcpy gets its own port block (27300-27899 → up to 200 phones) and they start one by one
MIRROR_PORT_BASE, MIRROR_PORT_SPAN, MIRROR_PORT_SLOTS = 27300, 3, 200
MIRROR_LAUNCH_GAP_MS = 700

# FB Daily intensity presets (same as fbdaily/gui.py)
FB_INTENSITY = {
    "Gentle": {"session_minutes": [4, 8], "between_sessions_seconds": [180, 420],
               "add_friends_range": [1, 2], "confirm_range": [1, 3],
               "weights_engage": 16, "weights_add": 2, "weights_conf": 2},
    "Normal": {"session_minutes": [5, 12], "between_sessions_seconds": [30, 120],
               "add_friends_range": [2, 5], "confirm_range": [1, 5],
               "weights_engage": 28, "weights_add": 5, "weights_conf": 3},
    "Active": {"session_minutes": [8, 16], "between_sessions_seconds": [15, 45],
               "add_friends_range": [3, 6], "confirm_range": [2, 6],
               "weights_engage": 38, "weights_add": 6, "weights_conf": 4},
}

_FBD = {"mod": None, "err": ""}


def _bundle_fb_deps():  # pragma: no cover - never called: makes PyInstaller pack what fb.py imports
    import uiautomator2  # noqa: F401
    import xml.etree.ElementTree  # noqa: F401
    import winrt.windows.foundation  # noqa: F401
    import winrt.windows.foundation.collections  # noqa: F401
    import winrt.windows.globalization  # noqa: F401
    import winrt.windows.graphics.imaging  # noqa: F401
    import winrt.windows.media.ocr  # noqa: F401
    import winrt.windows.storage.streams  # noqa: F401


def fb_selftest():
    """`Scientist.exe --fb-selftest`: check the FB Daily engine + screen helpers load, write
    fb_selftest.log next to the app and exit (works in the windowed build)."""
    out = []
    eng = load_fbdaily()
    out.append(f"engine: {'ok' if eng else 'FAIL ' + _FBD['err']}")
    helper = getattr(eng, "fb", None) if eng else None
    out.append(f"fb.py: {'ok ' + getattr(helper, '__file__', '') if helper else 'FAIL ' + str(getattr(eng, '_FB_IMPORT_ERR', ''))}")
    if helper:
        out.append(f"ocr: {helper.ocr_available()}")
        out.append(f"adb: {helper.ADB}")
    try:
        import uiautomator2 as _u2
        out.append(f"uiautomator2: ok {_u2.__file__}")
    except Exception as e:
        out.append(f"uiautomator2: FAIL {e}")
    out.append(f"wallpaper helper: {helper_jar_path()}  tether client: {tether_apk_path()}")
    out.append(f"scrcpy: {shutil.which('scrcpy')}")
    if helper and eng:
        try:                                   # read-only end-to-end check on a connected USB phone
            devs = eng.usb_devices()
            out.append(f"usb phones: {devs}")
            if devs:
                d = helper.connect(devs[0])
                lines, img = helper.ocr(d)
                out.append(f"phone {devs[0]}: screen {img.size if img else None}, ocr lines {len(lines)}, "
                           f"tabs {'found' if helper.nav_points(d) else 'not on screen'}")
        except Exception as e:
            out.append(f"phone check: FAIL {e}")
    text = "\n".join(out)
    try:
        (APP_DIR / "fb_selftest.log").write_text(text, encoding="utf-8")
    except Exception:
        pass
    print(text)


def load_fbdaily():
    """Load the FB Daily engine from fbdaily/fbdaily.py (by file path, so the `fbdaily/` folder
    itself is never mistaken for a package). Returns the module, or None (reason in _FBD["err"])."""
    if _FBD["mod"] is not None:
        return _FBD["mod"]
    path = FBDAILY_DIR / "fbdaily.py"
    if not path.exists():
        _FBD["err"] = f"{path} not found"
        return None
    try:
        if str(FBDAILY_DIR) not in sys.path:
            sys.path.insert(0, str(FBDAILY_DIR))
        spec = importlib.util.spec_from_file_location("fbdaily_engine", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["fbdaily_engine"] = mod
        spec.loader.exec_module(mod)
        # share the exact adb SCIENTIST uses: two different adb versions keep killing each other's server
        mod.ADB = shutil.which("adb") or mod.ADB
        if getattr(mod, "fb", None) is not None:
            mod.fb.ADB = mod.ADB
        os.environ.setdefault("ADBUTILS_ADB_PATH", mod.ADB)   # uiautomator2 uses the same adb too
        _FBD["mod"] = mod
        return mod
    except Exception as e:
        sys.modules.pop("fbdaily_engine", None)
        _FBD["err"] = f"{type(e).__name__}: {e}"
        return None

TILE_PRESETS = {"S": 120, "M": 240, "L": 400}
TILE_MIN, TILE_MAX, TILE_STEP = 50, 500, 20
FULL_TILE_MIN, COMPACT_TILE_MIN = 260, 150   # below: compact tile (number + status), then mini strip
DEFAULT_ASPECT = 2.0          # height / width of a tile until the device's real screen ratio is known
HOT_TEMP_C = 42.0             # battery temperature that triggers an overheat warning
RELAY_PORT = 31416            # scirelay (PC side of reverse tethering)

SCALE = 1.0


def px(n):
    return int(round(n * SCALE))


# card padding, corner radius, header height, toolbar height, grid gap — per tile style
TILE_STYLE = {
    "full": (10, 18, 44, 40, 6),
    "compact": (6, 12, 34, 0, 5),
    "mini": (3, 8, 17, 0, 3),
}


def tile_mode(w):
    return "full" if w >= FULL_TILE_MIN else "compact" if w >= COMPACT_TILE_MIN else "mini"


def tile_box(w, aspect, mode=None):
    """Screen space one tile takes in the grid (card + gap) for a mirror `w` px wide."""
    pad, _r, hdr, tb, gap = TILE_STYLE[mode or tile_mode(w)]
    return (px(w) + 2 * px(pad) + 6 + 2 * px(gap),
            int(px(w) * aspect) + 2 * px(pad) + 6 + px(hdr) + px(tb) + 2 * px(gap))


def best_fit_width(n, avail_w, avail_h, aspect):
    """Largest tile width (multiple of 5, TILE_MIN..TILE_MAX) that shows all n tiles without scrolling."""
    for w in range(TILE_MAX, TILE_MIN - 1, -5):
        bw, bh = tile_box(w, aspect)
        cols = int(avail_w // bw)
        if cols >= 1 and math.ceil(n / cols) * bh <= avail_h:
            return w
    return TILE_MIN

# ════════════════════════════════════════════════════════════════════
#  Theme
# ════════════════════════════════════════════════════════════════════
class C:
    BG = "#06040b"
    BG2 = "#0b0814"
    SURFACE = "#110c1d"
    SURFACE2 = "#181127"
    SURFACE3 = "#22183a"
    BORDER = "#2a1f45"
    BORDER_HI = "#4b3480"
    PURPLE = "#a855f7"
    PURPLE_HI = "#c99bff"
    PURPLE_LO = "#7e22ce"
    PURPLE_DIM = "#3b1f66"
    TEXT = "#efe9ff"
    SOFT = "#cfc6ea"
    MUTED = "#8a7fa8"
    FAINT = "#4f4569"
    INK = "#07040d"
    GREEN = "#2bf5a8"
    AMBER = "#ffc247"
    RED = "#ff4d7d"
    RED_DIM = "#6e2443"


class F:
    pass


ICONS = {
    "power": "", "home": "", "back": "", "recents": "",
    "camera": "", "refresh": "", "close": "", "play": "",
    "stop": "", "wifi": "", "terminal": "", "phone": "",
    "link": "", "trash": "", "copy": "", "folder": "",
    "select": "", "clear": "", "chev_down": "", "chev_up": "",
    "wake": "", "lock": "", "bright": "", "picture": "", "globe": "",
    "plus": "", "minus": "", "fit": "", "edit": "", "upload": "",
    "menu": "\uE700", "settings": "\uE713", "popout": "\uE8A7", "dock": "\uE73F",
    "clock": "\uE823", "coffee": "\uEC32", "food": "\uED56", "person": "\uE77B", "warn": "\uE7BA",
    "calendar": "\uE787",
    "package": "", "download": "",
}
FALLBACK = {
    "wake": "☼", "lock": "⌧", "bright": "◐", "picture": "▧", "globe": "◍", "plus": "+", "minus": "−",
    "fit": "⤢", "edit": "✎", "upload": "⇪", "package": "▣", "download": "⇩", "menu": "≡", "settings": "⚙", "popout": "⧉", "dock": "⇲",
    "power": "PWR", "home": "⌂", "back": "◀", "recents": "▣", "camera": "◉",
    "refresh": "↻", "close": "✕", "play": "▶", "stop": "■", "wifi": "≋",
    "terminal": ">_", "phone": "▯", "link": "∞", "trash": "✕", "copy": "⧉",
    "folder": "▤", "select": "☑", "clear": "☐", "chev_down": "▾", "chev_up": "▴",
    "clock": "◷", "coffee": "☕", "food": "◒", "person": "☺", "warn": "⚠", "calendar": "▦",
}


def init_fonts(root):
    fams = set(tkfont.families(root))

    def pick(*names, default):
        return next((n for n in names if n in fams), default)

    ui = pick("Bahnschrift", "Segoe UI Variable Text", "Segoe UI", default="TkDefaultFont")
    mono = pick("Cascadia Mono", "Cascadia Code", "JetBrains Mono", "Consolas", default="TkFixedFont")
    icon = pick("Segoe Fluent Icons", "Segoe MDL2 Assets", default=None)
    F.title = tkfont.Font(family=ui, size=20, weight="bold")
    F.big = tkfont.Font(family=ui, size=16, weight="bold")
    F.h2 = tkfont.Font(family=ui, size=12, weight="bold")
    F.h = tkfont.Font(family=ui, size=10, weight="bold")
    F.body = tkfont.Font(family=ui, size=10)
    F.small = tkfont.Font(family=ui, size=9)
    F.tiny = tkfont.Font(family=ui, size=8, weight="bold")
    F.btn = tkfont.Font(family=ui, size=10, weight="bold")
    F.mono = tkfont.Font(family=mono, size=9)
    F.mono_s = tkfont.Font(family=mono, size=8)
    F.mono_b = tkfont.Font(family=mono, size=10, weight="bold")
    F.icon = tkfont.Font(family=icon, size=11) if icon else None
    F.icon_big = tkfont.Font(family=icon, size=40) if icon else F.big


def glyph(name, text_font):
    if F.icon:
        return ICONS[name], F.icon
    return FALLBACK[name], text_font


def mix(a, b, t):
    a, b = a.lstrip("#"), b.lstrip("#")
    ca = [int(a[i:i + 2], 16) for i in (0, 2, 4)]
    cb = [int(b[i:i + 2], 16) for i in (0, 2, 4)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(ca, cb))


# ════════════════════════════════════════════════════════════════════
#  Colour themes (Settings → accent colour)
# ════════════════════════════════════════════════════════════════════
_BASE_THEME = {k: v for k, v in vars(C).items() if k.isupper()}
DEFAULT_ACCENT = _BASE_THEME["PURPLE"]
THEME_PRESETS = (
    ("Violet", "#a855f7"), ("Cyan", "#22d3ee"), ("Emerald", "#34d399"), ("Blue", "#3b82f6"),
    ("Pink", "#f472b6"), ("Crimson", "#f43f5e"), ("Amber", "#f59e0b"), ("Lime", "#a3e635"),
)
# background / text tones keep their lightness + saturation and take the accent's hue
_TINTED = ("BG", "BG2", "SURFACE", "SURFACE2", "SURFACE3", "BORDER", "BORDER_HI",
           "TEXT", "SOFT", "MUTED", "FAINT", "INK")


def valid_hex(c):
    return isinstance(c, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", c.strip() or "") is not None


def _hls(hexc):
    h = hexc.lstrip("#")
    return colorsys.rgb_to_hls(*(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)))


def _from_hls(h, l, s):
    return "#%02x%02x%02x" % tuple(round(max(0.0, min(1.0, v)) * 255) for v in colorsys.hls_to_rgb(h, l, s))


def palette_for(accent):
    """Full colour palette (same keys as C) built around one accent colour."""
    accent = accent.lower() if valid_hex(accent) else DEFAULT_ACCENT
    if accent == DEFAULT_ACCENT:
        return dict(_BASE_THEME)
    hue, _l, sat = _hls(accent)
    pal = dict(_BASE_THEME)
    for k in _TINTED:
        _h, bl, bs = _hls(_BASE_THEME[k])
        pal[k] = _from_hls(hue, bl, bs * min(1.0, sat / 0.5 + 0.15))   # greys stay greyish
    pal["PURPLE"] = accent
    pal["PURPLE_HI"] = mix(accent, "#ffffff", 0.40)
    pal["PURPLE_LO"] = mix(accent, "#000000", 0.25)
    pal["PURPLE_DIM"] = mix(pal["BG"], accent, 0.35)
    return pal


def apply_palette(pal):
    for k, v in pal.items():
        setattr(C, k, v)
    if "RButton" in globals():
        RButton.VARIANTS = button_variants()


def rrect(c, x1, y1, x2, y2, r, **kw):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


def elide(text, font, maxw):
    if font.measure(text) <= maxw:
        return text
    while text and font.measure(text + "…") > maxw:
        text = text[:-1]
    return text + "…"


def bg_thread(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


# ════════════════════════════════════════════════════════════════════
#  Win32 (ctypes) – embedding scrcpy windows + dark title bar
# ════════════════════════════════════════════════════════════════════
class _Win32:
    GWL_STYLE = -16
    STRIP = 0x00C00000 | 0x00040000 | 0x00080000 | 0x00020000 | 0x00010000

    def __init__(self):
        self.ok = False
        if not IS_WIN:
            return
        try:
            u = ctypes.WinDLL("user32", use_last_error=True)
            H = wintypes.HWND
            u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
            u.FindWindowW.restype = H
            u.SetParent.argtypes = [H, H]
            u.SetParent.restype = H
            u.GetParent.argtypes = [H]
            u.GetParent.restype = H
            u.ShowWindow.argtypes = [H, ctypes.c_int]
            u.MoveWindow.argtypes = [H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL]
            if ctypes.sizeof(ctypes.c_void_p) == 8:
                self._get, self._set = u.GetWindowLongPtrW, u.SetWindowLongPtrW
            else:
                self._get, self._set = u.GetWindowLongW, u.SetWindowLongW
            self._get.argtypes = [H, ctypes.c_int]
            self._get.restype = ctypes.c_ssize_t
            self._set.argtypes = [H, ctypes.c_int, ctypes.c_ssize_t]
            self._set.restype = ctypes.c_ssize_t
            u.GetAncestor.argtypes = [H, wintypes.UINT]
            u.GetAncestor.restype = H
            u.SetForegroundWindow.argtypes = [H]
            u.SetFocus.argtypes = [H]
            u.SetFocus.restype = H
            u.GetWindowThreadProcessId.argtypes = [H, ctypes.c_void_p]
            u.GetWindowThreadProcessId.restype = wintypes.DWORD
            u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
            u.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
            u.MapVirtualKeyW.restype = wintypes.UINT
            u.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
            u.SendInput.restype = wintypes.UINT
            self.k32 = ctypes.WinDLL("kernel32")
            self.u = u
            self.ok = True
        except Exception:
            self.ok = False

    def find(self, title):
        return self.u.FindWindowW("SDL_app", title) or self.u.FindWindowW(None, title)

    def embed(self, hwnd, parent):
        self.u.SetParent(hwnd, parent)
        style = self._get(hwnd, self.GWL_STYLE)
        self._set(hwnd, self.GWL_STYLE, style & ~self.STRIP)
        self.u.ShowWindow(hwnd, 5)

    def move(self, hwnd, x, y, w, h):
        self.u.MoveWindow(hwnd, x, y, max(1, w), max(1, h), True)

    def enable(self, hwnd, on):
        """A disabled mirror window shows the phone but ignores mouse/keyboard (view-only tile)."""
        self.u.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
        self.u.EnableWindow(hwnd, bool(on))

    def is_enabled(self, hwnd):
        self.u.IsWindowEnabled.argtypes = [wintypes.HWND]
        return bool(self.u.IsWindowEnabled(hwnd))

    def double_click_time(self):
        try:
            return self.u.GetDoubleClickTime() / 1000.0
        except Exception:
            return 0.5

    def parent(self, hwnd):
        return self.u.GetParent(hwnd)

    # --- keyboard focus + synthetic key presses (used for scrcpy shortcuts) ---
    def focus_child(self, hwnd):
        """Give keyboard focus to `hwnd` (possibly in another process). Returns a detach() callback."""
        u = self.u
        top = u.GetAncestor(hwnd, 2)  # GA_ROOT
        if top:
            u.SetForegroundWindow(top)
        me = self.k32.GetCurrentThreadId()
        target = u.GetWindowThreadProcessId(hwnd, None)
        attached = bool(target and target != me and u.AttachThreadInput(me, target, True))
        u.SetFocus(hwnd)

        def detach():
            if attached:
                u.AttachThreadInput(me, target, False)
        return detach

    def send_keys(self, vks):
        """Press the virtual keys in order, then release them in reverse order."""
        seq = [(vk, 0) for vk in vks] + [(vk, 0x0002) for vk in reversed(vks)]  # 0x2 = KEYEVENTF_KEYUP
        arr = (_INPUT * len(seq))()
        for i, (vk, flags) in enumerate(seq):
            arr[i].type = 1  # INPUT_KEYBOARD
            arr[i].u.ki = _KEYBDINPUT(vk, self.u.MapVirtualKeyW(vk, 0), flags, 0, 0)
        return self.u.SendInput(len(seq), ctypes.byref(arr), ctypes.sizeof(_INPUT))

    # --- Ctrl+wheel anywhere over our window (including embedded scrcpy mirrors) ---
    def start_ctrl_wheel_hook(self, top_hwnd, callback, on_click=None):
        """Low-level mouse hook: Ctrl+wheel over `top_hwnd` (or any child, e.g. a mirror owned by
        scrcpy's process) calls callback(delta) and is swallowed. Left clicks over the window are reported to
        on_click(x, y) (not swallowed): view-only mirrors swallow clicks, so this is how double-clicks on them
        are seen. Other input passes straight through."""
        if not self.ok:
            return False
        u, k32 = self.u, self.k32
        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        u.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        u.CallNextHookEx.restype = LRESULT
        u.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        u.SetWindowsHookExW.restype = ctypes.c_void_p
        u.WindowFromPoint.argtypes = [wintypes.POINT]
        u.WindowFromPoint.restype = wintypes.HWND
        u.GetAsyncKeyState.argtypes = [ctypes.c_int]
        u.GetAsyncKeyState.restype = ctypes.c_short
        k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        k32.GetModuleHandleW.restype = wintypes.HMODULE
        top = int(top_hwnd)

        def proc(n_code, w_param, l_param):
            try:
                if n_code == 0 and w_param == 0x020A and (u.GetAsyncKeyState(0x11) & 0x8000):  # WM_MOUSEWHEEL, Ctrl
                    info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    hwnd = u.WindowFromPoint(info.pt)
                    if hwnd and (u.GetAncestor(hwnd, 2) or 0) == top:
                        callback(ctypes.c_short((info.mouseData >> 16) & 0xFFFF).value)
                        return 1
                elif n_code == 0 and w_param == 0x0201 and on_click:                     # WM_LBUTTONDOWN
                    info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    hwnd = u.WindowFromPoint(info.pt)
                    if hwnd and (u.GetAncestor(hwnd, 2) or 0) == top:
                        on_click(info.pt.x, info.pt.y)
            except Exception:
                pass
            return u.CallNextHookEx(None, n_code, w_param, l_param)

        self._hook_proc = HOOKPROC(proc)  # keep a reference: the hook dies if this is collected

        def loop():
            hook = u.SetWindowsHookExW(14, self._hook_proc, k32.GetModuleHandleW(None), 0)  # WH_MOUSE_LL
            if not hook:
                return
            msg = wintypes.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))

        threading.Thread(target=loop, daemon=True).start()
        return True


if IS_WIN:
    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

VK_LALT, VK_LSHIFT, VK_O = 0xA4, 0xA0, 0x4F

WIN32 = _Win32()


def colorref(hexc):
    h = hexc.lstrip("#")
    return int(h[0:2], 16) | (int(h[2:4], 16) << 8) | (int(h[4:6], 16) << 16)


def style_titlebar(root):
    if not WIN32.ok:
        return
    try:
        root.update_idletasks()
        hwnd = WIN32.parent(root.winfo_id())
        dwm = ctypes.windll.dwmapi
        for attr, val in ((20, 1), (35, colorref(C.BG)), (34, colorref(C.PURPLE_LO)), (36, colorref(C.PURPLE_HI))):
            v = ctypes.c_int(val)
            dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), attr, ctypes.byref(v), ctypes.sizeof(v))
    except Exception:
        pass


def make_icon(root):
    n = 32
    img = tk.PhotoImage(width=n, height=n)
    rows = []
    for y in range(n):
        row = []
        for x in range(n):
            d = abs(x - 15.5) + abs(y - 15.5)
            row.append(C.PURPLE if d < 9 else C.PURPLE_HI if d < 11 else C.INK if d < 13 else C.PURPLE_LO if d < 15 else C.BG)
        rows.append("{" + " ".join(row) + "}")
    img.put(" ".join(rows))
    return img


# ════════════════════════════════════════════════════════════════════
#  ADB helpers
# ════════════════════════════════════════════════════════════════════
def run(cmd, timeout=10, binary=False):
    kw = {} if binary else {"text": True, "encoding": "utf-8", "errors": "replace"}
    return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW, **kw)


def run_adb(serial, *args, **kw):
    return run(["adb", "-s", serial, *args], **kw)


ADB_DISPLAY_MIN_SDK = 35  # `cmd display power-off/on` exists since Android 15
_BAD_OUTPUT = ("unknown", "error", "exception", "usage:", "invalid", "not found", "no such", "denied")


def adb_display(serial, on, sdk=None):
    """Turn the physical display on/off via `cmd display` (Android 15+).
    Checks the output too: older Android prints an error but may still exit with 0."""
    sub = "power-on" if on else "power-off"
    if sdk is not None and sdk < ADB_DISPLAY_MIN_SDK:
        return False, f"'cmd display {sub}' needs Android 15+ (device is API {sdk})"
    try:
        r = run_adb(serial, "shell", "cmd", "display", sub, "0")
        out = ((r.stdout or "") + " " + (r.stderr or "")).strip()
        if r.returncode == 0 and not any(b in out.lower() for b in _BAD_OUTPUT):
            return True, out
        return False, out or f"exit code {r.returncode}"
    except Exception as e:
        return False, str(e)


def fetch_info(serial):
    info = {}
    try:
        r = run_adb(serial, "shell",
                    "getprop ro.product.manufacturer; getprop ro.product.model; "
                    "getprop ro.build.version.release; getprop ro.build.version.sdk",
                    timeout=8)
        lines = [ln.strip() for ln in (r.stdout or "").splitlines()] + ["", "", "", ""]
        manu, model, rel, sdk = lines[:4]
        if sdk.isdigit():
            info["sdk"] = int(sdk)
        if model:
            info["model"] = model if not manu or model.lower().startswith(manu.lower()) else f"{manu.title()} {model}"
        if rel:
            info["android"] = rel
    except Exception:
        pass
    try:
        r = run_adb(serial, "shell", "dumpsys battery", timeout=8)
        m = re.search(r"level:\s*(\d+)", r.stdout or "")
        if m:
            info["battery"] = int(m.group(1))
        m = re.search(r"status:\s*(\d+)", r.stdout or "")
        if m:
            info["charging"] = m.group(1) == "2"
        m = re.search(r"temperature:\s*(\d+)", r.stdout or "")
        if m:
            info["temp"] = int(m.group(1)) / 10.0
    except Exception:
        pass
    if "res" not in info:
        try:
            r = run_adb(serial, "shell", "wm size", timeout=8)
            sizes = re.findall(r"(\d+)x(\d+)", r.stdout or "")
            if sizes:
                w, h = map(int, sizes[-1])  # "Override size" (if any) comes last
                info["res"] = (min(w, h), max(w, h))
        except Exception:
            pass
    return info


def terminate(proc):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════
#  Numbered wallpapers
# ════════════════════════════════════════════════════════════════════
HELPER_JAR_B64 = """
UEsDBBQAAAAIAHkibV1Gr9W2pgoAAIATAAALAAAAY2xhc3Nlcy5kZXh1mFtsHFcZx38zu+uNZ2dnZ9eXJJvbeJO2bkliO3FcO46T
Jm5CTTZNGgenTdrS8e7Jeur17HhnnLpcqlSVSB9A8FBVfeEigao+8IC4qQLEC+X2BAKEkICHcBNQkIAHpAok0Dkzu92kYebh+853
vvOd7/I/Z+acutg0xg8f4dBLN75dreivfNf+5PrvX/rw209UHl/+1F/fOXFjAAJgc2myRPJ8uQRrxPK9wD4dHCnXQQfG03AAeCgd
t9/IwGt5+FMGXjfgqwZ8w4DvGPB9A35mwC8M+JUBvzXgDwb82YC/GfBPA/5lwHAOJnKwmINmDj6Vg9dz8KMc/CUHGRPKJhw34WkT
XjThMya8acKPTfijCe+YYOdhXx6m87CYh0YeXsjD5/LwzTz8Ig//yMN/81C0wLHgoAUTFhyxYMaC4xacsuC0BQsWPGVB24KbFrxi
wRcteNOCH1jwOwvetuAfFlCAbAHKBRgtwHgBpgpwogCPFaBRgBcK8HIBPl+ArxTgewX4aQFuFeCdAuRs2GHDPTY8aMOCDUs2PG3D
qg3rNnzEhhdtuGnDp234jA1fsOENG75lw1s2/NqGWzb83YZ/29BfhGIRnCI8UIQPFuHJIjSKkAHyQBICNlAEJCAGgEFgCBgGtgLb
gO1AGdgB7AR2AbuBPcRYGQEqHfwA9wD3AvcBo8D9xBg6BEwCR4ApIJtgcL8BfQk/bYABaBJzBmwhxp18JN/RM5LxjyQ6W3p0+nt4
o0dfxnjBgFwS5+PJXIOJvMM/k9iUeSCxJ/OxkuhIPujh5Vgz4TeTsdt65pX5u5Hol3t8kPl82YhzI/Mpn4MJL+caS3iZh/GE/3SP
/EIP/5oBEwm/2SP/vBHnXdZKPocTXvq/lSJPq3oO8T6Vc51qkvu6ynuORSDFNlWDDGlOK5rljKIZHlX56eMxRfNcUnQ3Syr2LZxT
dJgPKbyVlZ0Cw5xQuCvzjKpLPw8rOsCCooN8QNEhzqqaDKtxQ+zhlMpz3B5O7A1jcUzRQkLthBYTmmdF4Xkr86o2OziqaIlHVH1M
nlR0JzVVo+0sKxq3d2DwQYX9OA87SfF+RfOcT+gFlcfYr13s4omkfVVRDaFoikayhuS6SyU1ScualeC+ZFfelazJwZ7+l0swV4In
SrI+skLxoyW6eiL7SX9st8xDt8l//h65fOGX/bH9wElhY6n1JfV+04/K0bpCrckMGdYVasvMsm7LvcOkpO0tOZR037FxMFOlQd9x
mMd39rAXM1XOuPhOkQuYGd+pcBp/fIR7UnuLDmXtIv54CVs38cf3Mq6b7EGnSBadDCne7kfhocw1fGeQZzAoFadJUxoeNctc7kj7
pvvSlO4d1UtTFXayzn7pnR44+3DULipbO1Ma5fTjyp95zJT0Z0D5s5cKBQLH5AKljL+koQ8ZGV9+Xihk8gk3msn3+c4uBPdl4r3O
QuNBVZ8UD6m8Sa8nOMHDqkYy8//u5NeWu6vFFrXKNP7bDx9WsT1AqTjSLqUmSyXK+lXOpLMpGdcpDH1a38I6D8g62BJ1I+3SvslT
hynrq5zJxHoXld79nNGz2shGaWpSZDiTzaZHU+XsHOvO+2hjZJd1qGSGCZxhMvjOVjbw7R1kMPEdC525bIpRvXQ4nmk5K33zVF9W
zZ7GIp/17d1kMZkmS8JnR/8lV0tFH6acSqkZDuE722irGXTMrLQSKCsalsLXosrPZYU3icNBAz6uviPd+mmyfjZl7SS+o2Mj8ZHB
t+U3zNRURKlhyukZNec8M1qGsvYUo/dX0sME9hBpfFtmXKJvO5coZzRizreziXybktOV96NhaL5TZlD5n8bUZN+AaqVUy6KEb6fv
8GM68UPa7GqnpPYhprXtvKt5vKvZp+KSI1KdEUk9LO7FYgSLnTJp41je3ObNY2e9Y8du8pAO1tfffPXczWMnvLOXGcJiRWP21rG5
W3/Xhgpzm096V7WBAk9gvXrAO3vrcW1bQSvl585qQ2o9SN9t0upbEO+tQwqvkpe0L6FDnfowotryW9/ZfbRkj5J0T0Kdrj0nGTeo
5PJ7KGmFnFNr+b6oRaI+hzZG3zHP96Lj9J2+ePH8RYfcI6IZiPbBZ93rLtoCqYVqlcxCtXplAa2KXl1Ar1ZJVauycYXdVdevt1te
fcwNgrHLbrMZuIFon3N9tyHas2zv9tdafiT8aGxe0s1oFuf/dV1uu0EgB2/tajTabrDi1cKxi6IWzTLS7fCFGiYj8q570fPdiYdu
U3lURM+12qt3jkzE827gLntNL/JEOMueu6ks+Nda+xYjNxK9Md2hMMtAt6sVjlVbLRVGpVp3m9e91THX91uRG3ktf+y0X2u2Qs9v
zDfdMJxl9110FnxftJP+kbv0nxNry4mC9Lt8F5VLK+3Wc6F0WRZ0zGuNnfGaYsEPNqLFqC3cNZmoTtf/EV9oe35XXIzFTddvjCWu
DfWITm/WRCBnnqXUIz6//KyqW69sMWp7fqPrWo/s1IbXrMvE3ab+fBiJd/1SMhWdu9wUsmjvitviWlPUFC7CqL1Ri1oKiO9VOOOJ
Zl0m7r1d50S00qrLYWHNG4sXxb7FFdFsdvFr9nTNYp8/6zzXgb/j1efILz5yulr90IWT82dPvv80Zu9otCX0pQX0pSqpJbmWlq6g
XUG/UqV09b05Hrh6l2zeJuykM+fWaiIMzzTdRojlymUhEojOUbqtLSE7R96t1xc3gqAtwlDUKScAPugGwcGTyaq6tNIWbp0+uS79
OunlVrRCvtazbObISEALirXW2sGOjVCGjJlsOcoUfWJ9w22GpMWmF5G91mo/6q4J7IaITvZ6x+CdEukvQ1IaBE2vphDeyeeWhojm
3Y1QMNwQ0cOi1nTbot6DADVBR64KT7FHEtebXENEC34YuX5NkG+I6Jzr+fE6VobvsmEow+eDC25t1W0IFYvVEFFvWzqn9g6lGyO5
4/i7kkXRvu7VhPLh0vNBPLTP86+3VgU5L0y2OVEn3WzVVtnStZFecz2ftC8HZH0ReTJROV881w0l7W80m6RaGxHFoC0Cty16IssG
cok3fYbD+Y12W/jRHYVPhSIiH8p6SHB5y01BfyiSXQEzFFF366cvVOGQj2m3QlErxij3b4RuQxztWS7HAr9x3LkqYfXReNRHZYhP
kbnuNjcE/V1VPqu98MLD0x+pLLu1VeHXK0crdbFZ2V+ptdYCr6kwcWCtVReVo5W2aAo3FJX9lRU3PFBbEbXVcGMtrBy95jZDsb+y
5vkH3MCrHD00sb8SrrgHJipHK+PTk+70zMShKVeMH5mZqh2ZmZmYPFy/duRBd+rI5NTklHvt0PThicOV/ZXroh16Lb9ytDJz8NDB
yQN1cb3yMXRTfzBdvrJ1v57Tnt22S7e0Z3dq2wb0vKI7QUvtu3Ej/SXzHu1POU37T07TXjE1Mmjl0RdvpD9R0Pq/WNCMrxU144d5
zXjL1LbK/w2KJdiizh+wNaF7SvBk9zYJ9QRJu3O+kf+vKyXUGVNS+XcnqTyHdP4jOrRzFyXn69xHSTudOyn539K5l5L/gZ27qc5/
pLyf0pz4zC7vqFJOfB6X5yLNju885H2D7sRzyTustBPblbHJiaQdeRbLOLFP8syVsmM/ZKx9iY48s8mDnNSR92j/A1BLAQIUABQA
AAAIAHkibV1Gr9W2pgoAAIATAAALAAAAAAAAAAAAAAC2gQAAAABjbGFzc2VzLmRleFBLBQYAAAAAAQABADkAAADPCgAAAAA=
"""

FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
WALL_UI_FONTS = [FONT_DIR / "bahnschrift.ttf", FONT_DIR / "segoeuib.ttf", "DejaVuSans-Bold.ttf"]
WALL_MONO_FONTS = [FONT_DIR / "CascadiaMono.ttf", FONT_DIR / "consola.ttf", "DejaVuSansMono.ttf"]


def have_pil():
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


def _wfont(paths, size, variation=None):
    from PIL import ImageFont
    for p in paths:
        try:
            f = ImageFont.truetype(str(p), size)
            if variation:
                try:
                    f.set_variation_by_name(variation)
                except Exception:
                    pass
            return f
        except Exception:
            continue
    return ImageFont.load_default(size)


def _rgb(hexc):
    h = hexc.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def render_wallpaper(num, total, serial, name="", size=(1080, 2340)):
    """Scientist-styled wallpaper with a huge unit number, for identifying phones in a farm."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
    W, H = size
    ss = 2                                   # supersample for smooth edges
    # every colour comes from the current software colour (Settings → software colour)
    dot_col = mix(C.BG2, C.SURFACE3, 0.8)
    hex_fill = mix(C.BG, C.BG2, 0.6)
    hue, _l, sat = _hls(C.PURPLE)
    glitch = _from_hls((hue + 0.07) % 1.0, 0.49, max(0.55, sat))     # neighbouring hue, like the violet/magenta pair
    num_col = mix("#ffffff", C.PURPLE, 0.10)
    w, h = W * ss, H * ss
    cx, cy, R = w / 2, h * 0.40, w * 0.33

    # background: vertical gradient + purple glow
    img = Image.composite(Image.new("RGB", (w, h), _rgb(C.BG2)), Image.new("RGB", (w, h), _rgb(C.BG)),
                          Image.linear_gradient("L").resize((w, h)))
    q = 16
    glow = Image.new("RGB", (w // q, h // q), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gr = R * 1.5 / q
    gd.ellipse((cx / q - gr, cy / q - gr, cx / q + gr, cy / q + gr), fill=_rgb(mix(C.BG, C.PURPLE_LO, 0.55)))
    glow = glow.filter(ImageFilter.GaussianBlur(gr * 0.55)).resize((w, h), Image.BICUBIC)
    img = ImageChops.screen(img, glow)
    d = ImageDraw.Draw(img)

    # dot grid
    step = w // 18
    for x in range(step // 2, w, step):
        for y in range(step // 2, h, step):
            d.rectangle((x, y, x + ss * 2, y + ss * 2), fill=_rgb(dot_col))

    def hexagon(r, rot=-90):
        return [(cx + r * math.cos(math.radians(60 * i + rot)), cy + r * math.sin(math.radians(60 * i + rot)))
                for i in range(6)]

    # neon glow behind the hexagon + number (rendered small, blurred, upscaled)
    txt = f"{num:02d}"
    q = 4
    nf_size = int(R * (1.05 if len(txt) <= 2 else 0.78))
    nglow = Image.new("RGB", (w // q, h // q), (0, 0, 0))
    ngd = ImageDraw.Draw(nglow)
    ngd.polygon([(x / q, y / q) for x, y in hexagon(R)], outline=_rgb(C.PURPLE), width=max(2, int(w * 0.012 / q)))
    ngd.text((cx / q, cy / q), txt, font=_wfont(WALL_UI_FONTS, max(8, nf_size // q), "Bold"),
             fill=_rgb(C.PURPLE), anchor="mm")
    nglow = nglow.filter(ImageFilter.GaussianBlur(w * 0.012 / q)).resize((w, h), Image.BICUBIC)
    img = ImageChops.screen(img, nglow)
    img = ImageChops.screen(img, nglow)
    d = ImageDraw.Draw(img)

    # hexagon frame
    d.polygon(hexagon(R), fill=_rgb(hex_fill))
    d.polygon(hexagon(R), outline=_rgb(C.PURPLE), width=int(w * 0.008))
    d.polygon(hexagon(R * 0.88), outline=_rgb(C.PURPLE_DIM), width=ss * 2)
    for (x1, y1), (x2, y2) in zip(hexagon(R * 1.04), hexagon(R * 1.12)):
        d.line((x1, y1, x2, y2), fill=_rgb(C.PURPLE_HI), width=ss * 3)

    # the number, with a chromatic "glitch" offset
    nf = _wfont(WALL_UI_FONTS, nf_size, "Bold")
    off = w * 0.006
    d.text((cx - off, cy), txt, font=nf, fill=_rgb(glitch), anchor="mm")
    d.text((cx + off, cy), txt, font=nf, fill=_rgb(C.PURPLE_LO), anchor="mm")
    d.text((cx, cy), txt, font=nf, fill=_rgb(num_col), anchor="mm")

    # title above
    tf = _wfont(WALL_UI_FONTS, int(w * 0.062), "Bold")
    title = "SCIENTIST"
    track = w * 0.018
    tw = sum(d.textlength(c, font=tf) for c in title) + track * (len(title) - 1)
    x = cx - tw / 2
    ty = cy - R - w * 0.22
    for c in title:
        d.text((x, ty), c, font=tf, fill=_rgb(C.PURPLE_HI), anchor="lm")
        x += d.textlength(c, font=tf) + track
    mf = _wfont(WALL_MONO_FONTS, int(w * 0.03))
    d.text((cx, ty + w * 0.07), "//  PHONE FARM UNIT  //", font=mf, fill=_rgb(C.MUTED), anchor="mm")

    # labels below
    by = cy + R + w * 0.16
    uf = _wfont(WALL_UI_FONTS, int(w * 0.06), "Bold")
    unit = f"UNIT {num:02d}" + (f"  /  {total:02d}" if total else "")
    d.text((cx, by), unit, font=uf, fill=_rgb(C.TEXT), anchor="mm")
    y = by + w * 0.08
    if name:
        d.text((cx, y), name.upper(), font=_wfont(WALL_UI_FONTS, int(w * 0.042), "SemiBold"),
               fill=_rgb(C.PURPLE_HI), anchor="mm")
        y += w * 0.065
    d.text((cx, y), serial, font=mf, fill=_rgb(C.MUTED), anchor="mm")

    # barcode strip derived from the serial
    digest = hashlib.sha1(serial.encode()).digest() * 2
    bx, bw_total, bh = cx - w * 0.25, w * 0.5, w * 0.05
    byy = y + w * 0.06
    x = bx
    for i, b in enumerate(digest):
        bar = (1 + b % 4) * ss * 2
        if x + bar > bx + bw_total:
            break
        if i % 2 == 0:
            d.rectangle((x, byy, x + bar, byy + bh), fill=_rgb(C.PURPLE if b % 3 else C.PURPLE_DIM))
        x += bar + ss * 2

    # corner brackets (kept clear of the status / navigation bars)
    m, ln, lw = w * 0.06, w * 0.09, ss * 4
    top, bot = h * 0.065, h * 0.935
    for (x0, y0, sx, sy) in ((m, top, 1, 1), (w - m, top, -1, 1), (m, bot, 1, -1), (w - m, bot, -1, -1)):
        d.line((x0, y0 + sy * ln, x0, y0, x0 + sx * ln, y0), fill=_rgb(C.PURPLE), width=lw, joint="curve")
    sf = _wfont(WALL_MONO_FONTS, int(w * 0.024))
    d.text((m + w * 0.03, top + w * 0.05), f"NODE//{num:02d}", font=sf, fill=_rgb(C.FAINT), anchor="lm")
    d.text((w - m - w * 0.03, top + w * 0.05), "SYS.ONLINE", font=sf, fill=_rgb(C.FAINT), anchor="rm")
    d.text((cx, bot - w * 0.05), "ADB MULTI-MIRROR  ·  SCREEN CONTROL MATRIX", font=sf, fill=_rgb(C.FAINT),
           anchor="mm")

    img = img.resize((W, H), Image.LANCZOS)
    # faint scanlines
    lines = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lines)
    for yy in range(0, H, 4):
        ld.line((0, yy, W, yy), fill=(0, 0, 0, 38))
    return Image.alpha_composite(img.convert("RGBA"), lines).convert("RGB")


_helper_local = None


def helper_jar_path():
    """Local copy of the on-device helper (prefers tools/helper/scientist-helper.jar, else the embedded one)."""
    global _helper_local
    built = TOOLS_DIR / "helper" / "scientist-helper.jar"
    if built.exists():
        return built
    if _helper_local is None:
        if not HELPER_JAR_B64.strip():
            raise RuntimeError("wallpaper helper missing — run tools/helper/build.py")
        _helper_local = Path(tempfile.gettempdir()) / "scientist-helper.jar"
        _helper_local.write_bytes(base64.b64decode("".join(HELPER_JAR_B64.split())))
    return _helper_local


def set_device_wallpaper(serial, png_path, lock=True):
    """Push a PNG and set it as wallpaper through app_process — no root, no taps."""
    remote_png = "/data/local/tmp/scientist-wallpaper.png"
    r = run_adb(serial, "push", str(helper_jar_path()), HELPER_REMOTE, timeout=30)
    if r.returncode != 0:
        return False, "push helper: " + (r.stderr or r.stdout).strip()
    r = run_adb(serial, "push", str(png_path), remote_png, timeout=60)
    if r.returncode != 0:
        return False, "push image: " + (r.stderr or r.stdout).strip()
    r = run_adb(serial, "shell",
                f"CLASSPATH={HELPER_REMOTE} app_process / sci.Helper wallpaper {remote_png} "
                f"{'both' if lock else 'system'}; rm -f {remote_png}", timeout=45)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    ok = "OK wallpaper" in out
    return ok, out.splitlines()[-1] if out else f"exit code {r.returncode}"


# ════════════════════════════════════════════════════════════════════
#  Reverse tethering: the phone uses the PC's internet over USB
#    PC side   scirelay (built in)          phone side   SCIENTIST Tether (embedded APK)
# ════════════════════════════════════════════════════════════════════
TETHER_PKG = "com.scientist.tether"
TETHER_VERSION = 4                           # versionCode of the embedded client
LEGACY_PKG = "com.genymobile.gnirehtet"      # older client: migrated automatically
TETHER_APK_B64 = """
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
Nqv2vvxHKqrlWZEbipUm92rqv2zWxTrZZmCpx2syHxPY5kVHUph+kleBokWPSo+uEPtRvwEAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA+A8AAAAAAABxBQAA
AAAAABqHCXFpBQAAZQUAACcDAAAsAAAAKAAAAAMBAAAgAAAARn9se5jBwjg/KHWY5r+/9/hzhlc5ueeDAFR65hWozDHfAgAA2wIA
ADCCAtcwggG/oAMCAQICBHXAi8EwDQYJKoZIhvcNAQELBQAwGzEZMBcGA1UEAxMQU0NJRU5USVNUIFRldGhlcjAgFw0yNjExMTIx
ODA3NDdaGA8yMDU0MDMzMDE4MDc0N1owGzEZMBcGA1UEAxMQU0NJRU5USVNUIFRldGhlcjCCASIwDQYJKoZIhvcNAQEBBQADggEP
ADCCAQoCggEBAIuiPAaN55Ul8lYhjATI4U1qrmZl25rsi9ZDNgSbhpc2/SN4wW17ZuQQzngRoPSkAqUD6Iz1RbOWRlHjPARO02n0
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
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAPgPAAAAAAAAQVBLIFNpZyBCbG9jayA0MlBLAQIAAAAAAAAIAAAAIQBTCAFrEAQAAGAMAAATAAAAAAAAAAAAAAAA
AAAAAABBbmRyb2lkTWFuaWZlc3QueG1sUEsBAgAAAAAAAAAAAAAhAAtQNhMoAAAAKAAAAA4AAAAAAAAAAAAAAAAAQQQAAHJlc291
cmNlcy5hcnNjUEsBAhQAFAAAAAgAgCJtXd11aI/rFwAAkCwAAAsAAAAAAAAAAAAAALaBnAQAAGNsYXNzZXMuZGV4UEsBAhQAFAAA
CAgAgCJtXbotBOosAQAAnAEAABIAAAAAAAAAAAAAAAAAsBwAAE1FVEEtSU5GL1RFVEhFUi5TRlBLAQIUABQAAAgIAIAibV1dFfoQ
FAQAAGkEAAATAAAAAAAAAAAAAAAAAAweAABNRVRBLUlORi9URVRIRVIuUlNBUEsBAhQAFAAACAgAgCJtXXzGdVjaAAAAHQEAABQA
AAAAAAAAAAAAAAAAUSIAAE1FVEEtSU5GL01BTklGRVNULk1GUEsFBgAAAAAGAAYAeQEAAABAAAAAAA==
"""

_tether_apk_local = None


def tether_apk_path():
    global _tether_apk_local
    built = TOOLS_DIR / "tether" / "scientist-tether.apk"
    if built.exists():
        return built
    if _tether_apk_local is None:
        if not TETHER_APK_B64.strip():
            raise RuntimeError("tether client missing — run tools/tether/build.py")
        _tether_apk_local = Path(tempfile.gettempdir()) / "scientist-tether.apk"
        _tether_apk_local.write_bytes(base64.b64decode("".join(TETHER_APK_B64.split())))
    return _tether_apk_local


class Tether:
    def __init__(self, log):
        self.log = log
        self.relay = None
        self.lock = threading.Lock()
        self.dns = "8.8.8.8"

    @staticmethod
    def ready():
        try:
            return tether_apk_path().exists()
        except Exception:
            return False

    # --- relay (PC side, in this process) ---
    def relay_owned(self):
        return self.relay is not None and self.relay.running()

    def relay_alive(self):
        """True if a relay is listening on the port — ours, or one run by another SCIENTIST window."""
        if self.relay_owned():
            return True
        import socket
        try:
            with socket.create_connection(("127.0.0.1", RELAY_PORT), timeout=0.4):
                return True
        except OSError:
            return False

    def ensure_relay(self):
        with self.lock:
            if self.relay_alive():
                return True
            self.relay = scirelay.Relay(RELAY_PORT, log=self.log)
            if self.relay.start():
                return True
            self.log(f"[relay] cannot listen on port {RELAY_PORT}: {self.relay._error}", "err")
            return False

    def stats(self):
        return self.relay.stats if self.relay_owned() else None

    # --- phone side ---
    @staticmethod
    def has_tunnel(serial):
        """adb reverse tunnel phone → relay. adb drops it whenever its server restarts, the phone's adbd
        restarts (MIUI does this) or the phone reconnects — which silently cuts the phone's internet."""
        r = run_adb(serial, "reverse", "--list", timeout=8)
        return f"localabstract:gnirehtet tcp:{RELAY_PORT}" in (r.stdout or "")

    @staticmethod
    def add_tunnel(serial):
        return run_adb(serial, "reverse", "localabstract:gnirehtet", f"tcp:{RELAY_PORT}", timeout=8).returncode == 0

    @staticmethod
    def remove_tunnel(serial):
        run_adb(serial, "reverse", "--remove", "localabstract:gnirehtet", timeout=8)

    @staticmethod
    def client_running(serial):
        r = run_adb(serial, "shell", f"dumpsys activity services {TETHER_PKG}", timeout=8)
        return "TetherService" in (r.stdout or "")

    @staticmethod
    def legacy_running(serial):
        r = run_adb(serial, "shell", f"dumpsys activity services {LEGACY_PKG}", timeout=8)
        return "GnirehtetService" in (r.stdout or "")

    @staticmethod
    def installed_version(serial):
        r = run_adb(serial, "shell", f"dumpsys package {TETHER_PKG}", timeout=10)
        m = re.search(r"versionCode=(\d+)", r.stdout or "")
        return int(m.group(1)) if m else 0

    def ensure_client(self, serial):
        if self.installed_version(serial) >= TETHER_VERSION:
            return True, "up to date"
        apk = str(tether_apk_path())
        r = run_adb(serial, "install", "-r", apk, timeout=120)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in out:        # signed with another key: replace it
            run_adb(serial, "uninstall", TETHER_PKG, timeout=60)
            r = run_adb(serial, "install", apk, timeout=120)
            out = ((r.stdout or "") + (r.stderr or "")).strip()
        return "Success" in out, out.splitlines()[-1] if out else "install failed"

    @staticmethod
    def keep_alive(serial):
        """Pre-approve the VPN (no consent tap on the phone) and stop battery savers from killing it."""
        run_adb(serial, "shell", f"appops set {TETHER_PKG} ACTIVATE_VPN allow; "
                f"dumpsys deviceidle whitelist +{TETHER_PKG} >/dev/null; "
                f"cmd appops set {TETHER_PKG} RUN_IN_BACKGROUND allow; "
                f"cmd appops set {TETHER_PKG} RUN_ANY_IN_BACKGROUND allow", timeout=10)

    @staticmethod
    def internet_ok(serial, timeout=8):
        """Real check from the phone: HTTP request to Google through whatever route the phone uses."""
        try:
            r = run_adb(serial, "shell",
                        r"(printf 'HEAD /generate_204 HTTP/1.1\r\nHost: connectivitycheck.gstatic.com\r\n"
                        r"Connection: close\r\n\r\n'; sleep 4) | "
                        f"toybox nc -w {timeout} connectivitycheck.gstatic.com 80 | head -1",
                        timeout=timeout + 8)
            return "HTTP/1." in (r.stdout or "")
        except Exception:
            return False

    def _start_client(self, serial):
        return run_adb(serial, "shell", f"am start -n {TETHER_PKG}/.StartActivity -a {TETHER_PKG}.START "
                       f"--es dns '{self.dns}'", timeout=15).returncode == 0

    def _stop_legacy(self, serial):
        if self.legacy_running(serial):
            run_adb(serial, "shell", f"am start -a {LEGACY_PKG}.STOP -n {LEGACY_PKG}/.GnirehtetActivity", timeout=15)

    def start(self, serial, dns=None):
        self.dns = dns or self.dns
        if not self.ensure_relay():
            return False, f"relay failed to start (port {RELAY_PORT} busy?)"
        ok, msg = self.ensure_client(serial)
        if not ok:
            return False, f"install client: {msg}"
        self.keep_alive(serial)
        self._stop_legacy(serial)
        if not self.has_tunnel(serial):
            self.add_tunnel(serial)
        if not self._start_client(serial):
            return False, "could not start the client"
        for _ in range(10):
            if self.client_running(serial):
                break
            time.sleep(1)
        else:
            return True, "waiting — accept the VPN request on the phone"
        time.sleep(1)
        return True, "online via PC" if self.internet_ok(serial) else "tunnel up, internet check failed"

    def stop(self, serial):
        run_adb(serial, "shell", f"am start -n {TETHER_PKG}/.StartActivity -a {TETHER_PKG}.STOP", timeout=15)
        self._stop_legacy(serial)
        self.remove_tunnel(serial)
        return True, "stopped"

    def repair(self, serial):
        """Bring a tether back: relay up, tunnel present, client running. Returns what was fixed."""
        fixed = []
        if not self.relay_alive() and self.ensure_relay():
            fixed.append("relay restarted")
        if not self.has_tunnel(serial) and self.add_tunnel(serial):
            fixed.append("tunnel restored")
        if not self.client_running(serial):          # the phone killed the VPN client
            self.keep_alive(serial)
            if self._start_client(serial):
                fixed.append("VPN client restarted")
        return fixed

    def shutdown(self):
        with self.lock:
            if self.relay_owned():
                self.relay.stop()
            self.relay = None


DEFAULTS = {
    "stay_awake": True, "turn_screen_off": False, "show_touches": False, "show_clicks": False,
    "audio_mode": "pc", "tweaks": {},
    "auto_refresh": True, "max_size": "0", "max_fps": "0", "tile_w": 380,
    "auto_mirror": False, "auto_reconnect": True, "auto_tether": False, "wall_lock": True,
    "auto_fit": True, "sidebar_hidden": False,
    "wifi_addr": "", "shell_history": [], "devices": {}, "dns": "8.8.8.8",
    # settings window
    "accent": DEFAULT_ACCENT, "log_visible": True, "clock_24h": False,
    # FB Daily
    "fb_chat": True, "fb_friends": True, "fb_engage": True, "fb_intensity": "Normal", "fb_pacing": False,
    # time clock (workers, required shift, break allowance, admin password hash)
    "timeclock": {},
}
BOOL_KEYS = ("stay_awake", "turn_screen_off", "show_touches", "show_clicks", "auto_refresh",
             "auto_mirror", "auto_reconnect", "auto_tether", "wall_lock", "auto_fit", "sidebar_hidden",
             "log_visible", "clock_24h", "fb_chat", "fb_friends", "fb_engage", "fb_pacing")
STR_KEYS = ("max_size", "max_fps", "audio_mode", "accent", "fb_intensity")
INT_KEYS = ("tile_w",)


def load_settings():
    s = dict(DEFAULTS)
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        s.update(saved)
        if "audio_mode" not in saved and saved.get("no_audio"):
            s["audio_mode"] = "phone"          # old "Mute audio" (--no-audio) = sound stays on the phone
    except Exception:
        pass
    if s.get("audio_mode") not in ("pc", "phone", "mute"):
        s["audio_mode"] = "pc"
    if not valid_hex(s.get("accent")):
        s["accent"] = DEFAULT_ACCENT
    if s.get("fb_intensity") not in FB_INTENSITY:
        s["fb_intensity"] = "Normal"
    return s


# Phone settings SCIENTIST changes while mirroring, and puts back afterwards.
def _tweak_get(serial, key):
    if key == "pointer_location":
        v = run_adb(serial, "shell", "settings get system pointer_location", timeout=8).stdout.strip()
        return v if v in ("0", "1") else "0"
    if key == "volume":
        out = run_adb(serial, "shell", "cmd media_session volume --stream 3 --get", timeout=8).stdout
        m = re.search(r"volume is (\d+)", out or "")
        if m:
            return m.group(1)
        # some ROMs (MIUI) print nothing for --get: read the media stream from dumpsys instead
        out = run_adb(serial, "shell", "dumpsys audio", timeout=10).stdout or ""
        m = re.search(r"- STREAM_MUSIC:.*?streamVolume:\s*(\d+)", out, re.S)
        return m.group(1) if m else None
    return None


def _tweak_set(serial, key, value):
    if key == "pointer_location":
        run_adb(serial, "shell", f"settings put system pointer_location {value}", timeout=8)
    elif key == "volume":
        run_adb(serial, "shell", f"cmd media_session volume --stream 3 --set {value}", timeout=8)


def save_settings(data):
    try:
        SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════
#  Time clock: worker time in / out, break, lunch, overbreak, proofs
# ════════════════════════════════════════════════════════════════════
def _documents_dir():
    if IS_WIN:
        try:  # the real Documents folder, also when it is moved to OneDrive / another drive
            buf = ctypes.create_unicode_buffer(520)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:
                return Path(buf.value)
        except Exception:
            pass
    d = Path.home() / "Documents"
    return d if d.is_dir() else Path.home()


# Records + proof screenshots live in Documents, so uninstalling / updating SCIENTIST never deletes them.
TIMECLOCK_DIR = _documents_dir() / "Scientist Time Clock"
TC_DEFAULT_PASSWORD = "admin"
TC_DEFAULTS = {"workers": ["Worker 1", "Worker 2"], "active": "Worker 1",
               "time_in": "08:00", "time_out": "17:00", "break_min": 60, "pw": ""}
TC_ACTIONS = {"in": "TIME IN", "out": "TIME OUT", "break": "BREAK", "lunch": "LUNCH"}


def parse_hhmm(text):
    """'8', '8:30', '8:30 pm', '17:00', '0830' → 'HH:MM' (24 h), or None."""
    t = str(text or "").strip().lower().replace(".", "").replace(" ", "")
    m = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?(am|pm|a|p|m)?", t)
    if not m or m.group(3) == "m":
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if mi > 59:
        return None
    if ap:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ap.startswith("p") else 0)
    elif h > 23:
        return None
    return f"{h:02d}:{mi:02d}"


def fmt_hhmm(hhmm, h24=False):
    h, m = map(int, hhmm.split(":"))
    return f"{h:02d}:{m:02d}" if h24 else f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def fmt_time(dt, h24=False, sec=False):
    if dt is None:
        return "--"
    if h24:
        return dt.strftime("%H:%M:%S" if sec else "%H:%M")
    return dt.strftime("%I:%M:%S %p" if sec else "%I:%M %p").lstrip("0")


def fmt_mins(m):
    m = max(0, int(round(m)))
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def tc_hash(pw):
    return hashlib.sha256(("scientist-timeclock:" + pw).encode("utf-8")).hexdigest()


def tc_clean_cfg(raw):
    """Validated time-clock config (bad / missing values fall back to the defaults)."""
    cfg = dict(TC_DEFAULTS)
    raw = raw if isinstance(raw, dict) else {}
    ws = []
    for w in raw.get("workers", TC_DEFAULTS["workers"]) or []:
        w = str(w).strip()[:40]
        if w and w.lower() not in (x.lower() for x in ws):
            ws.append(w)
    cfg["workers"] = ws or list(TC_DEFAULTS["workers"])
    cfg["active"] = raw.get("active") if raw.get("active") in cfg["workers"] else cfg["workers"][0]
    for k in ("time_in", "time_out"):
        cfg[k] = parse_hhmm(raw.get(k, cfg[k])) or TC_DEFAULTS[k]
    try:
        cfg["break_min"] = min(600, max(1, int(raw.get("break_min", 60))))
    except (TypeError, ValueError):
        cfg["break_min"] = 60
    cfg["pw"] = raw.get("pw") if isinstance(raw.get("pw"), str) else ""
    # each worker has their own required shift; time_in / time_out / break_min above = default for new workers
    raw_sh = raw.get("shifts") if isinstance(raw.get("shifts"), dict) else {}
    cfg["shifts"] = {w: tc_clean_shift(raw_sh.get(w), cfg) for w in cfg["workers"]}
    return cfg


def tc_clean_shift(raw, default):
    raw = raw if isinstance(raw, dict) else {}
    sh = {}
    for k in ("time_in", "time_out"):
        sh[k] = parse_hhmm(raw.get(k, default[k])) or default[k]
    try:
        sh["break_min"] = min(600, max(1, int(raw.get("break_min", default["break_min"]))))
    except (TypeError, ValueError):
        sh["break_min"] = default["break_min"]
    return sh


def _dt(iso):
    try:
        return datetime.fromisoformat(iso) if iso else None
    except (TypeError, ValueError):
        return None


class TimeClock:
    """Per-worker shifts stored in records.json:  {"YYYY-MM-DD": {worker: record}}

    record = {"in": iso, "out": iso|None, "req": {"in": "HH:MM", "out": "HH:MM", "break": min},
              "breaks": [{"kind": "break"|"lunch", "start": iso, "end": iso|None}], "proofs": [...]}
    A shift belongs to the day its TIME IN was pressed, so night shifts that pass midnight stay one record.
    """

    def __init__(self, folder):
        self.dir = Path(folder)
        self.path = self.dir / "records.json"
        self.csv_path = self.dir / "timesheet.csv"
        self.data = {}
        self.error = ""
        self.csv_locked = False
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                self.data = d
        except FileNotFoundError:
            pass
        except Exception as e:  # keep the unreadable file instead of overwriting it
            self.error = f"records.json unreadable ({e}) — a backup copy was kept"
            try:
                shutil.copy2(self.path, self.path.with_name(f"records.broken-{int(time.time())}.json"))
            except Exception:
                pass

    # ── storage ──
    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            self.write_csv()
            self.csv_locked = False
        except PermissionError:  # timesheet.csv open in Excel: records.json is saved, CSV catches up later
            self.csv_locked = True

    # ── lookup ──
    def current(self, worker, now=None):
        """(day, record) of the worker's open shift, else today's record, else a shift that ended in the
        last 4 hours (night shift timed out after midnight), else (today, None)."""
        now = now or datetime.now()
        recent = None
        for day in sorted(self.data, reverse=True)[:3]:
            rec = self.data[day].get(worker)
            if not rec or not rec.get("in"):
                continue
            if not rec.get("out"):
                t_in = _dt(rec["in"])
                if t_in and (now - t_in).total_seconds() < 20 * 3600:
                    return day, rec
            elif recent is None:
                t_out = _dt(rec["out"])
                if t_out and 0 <= (now - t_out).total_seconds() < 4 * 3600:
                    recent = (day, rec)
        today = now.strftime("%Y-%m-%d")
        rec = self.data.get(today, {}).get(worker)
        if rec:
            return today, rec
        return recent or (today, None)

    def undo_last(self, worker, now=None):
        """Remove the worker's last punch. Returns its label, or None."""
        day, rec = self.current(worker, now)
        if not rec or not rec.get("in"):
            return None
        brs = rec.setdefault("breaks", [])
        if rec.get("out"):
            out, rec["out"] = rec["out"], None
            for b in brs:
                if b.get("end") == out:   # TIME OUT closed this break: it is open again
                    b["end"] = None
            label = "TIME OUT"
        elif brs and not brs[-1].get("end"):
            label = TC_ACTIONS.get(brs.pop().get("kind"), "BREAK") + " START"
        elif brs:
            brs[-1]["end"] = None
            label = TC_ACTIONS.get(brs[-1].get("kind"), "BREAK") + " END"
        else:
            self.data[day].pop(worker, None)
            if not self.data[day]:
                self.data.pop(day)
            label = "TIME IN"
        self.save()
        return label

    def set_record(self, day, worker, rec):
        if rec is None:
            self.data.get(day, {}).pop(worker, None)
            if day in self.data and not self.data[day]:
                self.data.pop(day)
        else:
            self.data.setdefault(day, {})[worker] = rec
        self.save()

    @staticmethod
    def state(rec):
        if not rec or not rec.get("in"):
            return "off"
        if rec.get("out"):
            return "out"
        for b in rec.get("breaks", []):
            if not b.get("end"):
                return b.get("kind", "break")
        return "working"

    # ── punches ──
    def punch(self, worker, action, cfg, now=None):
        """action: in | out | break | lunch.  Returns (ok, message, event_label)."""
        now = (now or datetime.now()).replace(microsecond=0)
        day, rec = self.current(worker, now)
        st = self.state(rec)
        iso = now.isoformat()
        if action == "in":
            if st == "out":
                return False, f"{worker} already timed out today — the admin can edit the times", None
            if st != "off":
                return False, f"{worker} is already timed in", None
            self.data.setdefault(day, {})[worker] = {
                "in": iso, "out": None, "breaks": [], "proofs": [],
                "req": {"in": cfg["time_in"], "out": cfg["time_out"], "break": cfg["break_min"]}}
            label = "TIME IN"
        elif action in ("break", "lunch"):
            if st in ("off", "out"):
                return False, f"{worker} is not timed in", None
            name = TC_ACTIONS[action]
            if st == action:  # second press ends it
                rec["breaks"][-1]["end"] = iso
                label = f"{name} END"
            elif st in ("break", "lunch"):
                return False, f"End the {st} first", None
            else:
                rec["breaks"].append({"kind": action, "start": iso, "end": None})
                label = f"{name} START"
        elif action == "out":
            if st in ("off", "out"):
                return False, f"{worker} is not timed in", None
            for b in rec["breaks"]:
                if not b.get("end"):
                    b["end"] = iso
            rec["out"] = iso
            label = "TIME OUT"
        else:
            return False, "unknown action", None
        self.save()
        return True, f"{worker}: {label} at {fmt_time(now, sec=True)}", label

    def reset(self, worker, now=None):
        day, rec = self.current(worker, now)
        if rec:
            self.data[day].pop(worker, None)
            if not self.data[day]:
                self.data.pop(day)
            self.save()
            return True
        return False

    def rename(self, old, new):
        for day in self.data.values():
            if old in day and new not in day:
                day[new] = day.pop(old)
        self.save()

    # ── maths ──
    @staticmethod
    def summary(rec, now=None):
        """Late / break / overbreak / undertime figures (minutes) for one record."""
        now = now or datetime.now()
        out = {"state": TimeClock.state(rec), "in": None, "out": None, "late": 0, "break": 0, "lunch": 0,
               "used": 0, "allow": 60, "over": 0, "under": 0, "ot": 0, "worked": 0, "n_break": 0,
               "n_lunch": 0, "open": None, "req_in": None, "req_out": None}
        if not rec or not rec.get("in"):
            return out
        req = rec.get("req") or {}
        t_in, t_out = _dt(rec["in"]), _dt(rec.get("out"))
        out.update({"in": t_in, "out": t_out, "allow": int(req.get("break", 60)),
                    "req_in": req.get("in"), "req_out": req.get("out")})
        for b in rec.get("breaks", []):
            s, e = _dt(b.get("start")), _dt(b.get("end"))
            if not s:
                continue
            mins = ((e or t_out or now) - s).total_seconds() / 60
            kind = "lunch" if b.get("kind") == "lunch" else "break"
            out[kind] += mins
            out["n_" + kind] += 1
            if not e and not t_out:
                out["open"] = (kind, s)
        out["used"] = out["break"] + out["lunch"]
        out["over"] = max(0.0, out["used"] - out["allow"])
        if req.get("in"):
            h, m = map(int, req["in"].split(":"))
            sched = t_in.replace(hour=h, minute=m, second=0, microsecond=0)
            diff = (t_in - sched).total_seconds() / 60
            if diff > 720:      # e.g. 00:00 shift, timed in 23:55 the day before → shift starts tomorrow
                sched = sched + _td(days=1)
                diff -= 1440
            elif diff < -720:   # e.g. 22:00 shift, timed in 00:10 the next day → shift started yesterday
                sched = sched - _td(days=1)
                diff += 1440
            out["late"] = max(0.0, diff)
            if req.get("out"):
                h2, m2 = map(int, req["out"].split(":"))
                end = sched.replace(hour=h2, minute=m2)
                if end <= sched:
                    end += _td(days=1)
                if t_out:
                    out["under"] = max(0.0, (end - t_out).total_seconds() / 60)
                    out["ot"] = max(0.0, (t_out - end).total_seconds() / 60)
        out["worked"] = max(0.0, ((t_out or now) - t_in).total_seconds() / 60 - out["used"])
        return out

    @staticmethod
    def events(rec):
        """[(label, datetime)] in time order."""
        ev = []
        if rec and rec.get("in"):
            ev.append(("TIME IN", _dt(rec["in"])))
            for b in rec.get("breaks", []):
                name = TC_ACTIONS.get(b.get("kind"), "BREAK")
                if b.get("start"):
                    ev.append((f"{name} START", _dt(b["start"])))
                if b.get("end") and b.get("end") != rec.get("out"):
                    ev.append((f"{name} END", _dt(b["end"])))
            if rec.get("out"):
                ev.append(("TIME OUT", _dt(rec["out"])))
        return sorted((e for e in ev if e[1]), key=lambda e: e[1])

    def write_csv(self):
        import csv
        rows = []
        for day in sorted(self.data):
            for worker, rec in sorted(self.data[day].items()):
                s = self.summary(rec)
                rows.append([
                    day, worker,
                    fmt_hhmm(s["req_in"]) if s["req_in"] else "", fmt_time(s["in"]), int(round(s["late"])),
                    int(round(s["break"])), s["n_break"], int(round(s["lunch"])), s["n_lunch"],
                    int(round(s["used"])), s["allow"], int(round(s["over"])),
                    fmt_hhmm(s["req_out"]) if s["req_out"] else "", fmt_time(s["out"]) if s["out"] else "",
                    int(round(s["under"])), int(round(s["ot"])),
                    f"{s['worked'] / 60:.2f}" if s["out"] else "on duty",
                    "OVERBREAK" if s["over"] >= 1 else "", len(rec.get("proofs", [])),
                ])
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Date", "Worker", "Required in", "Time in", "Late (min)", "Break (min)", "Breaks",
                        "Lunch (min)", "Lunches", "Total break (min)", "Allowed (min)", "Overbreak (min)",
                        "Required out", "Time out", "Undertime (min)", "Overtime (min)", "Hours worked",
                        "Flag", "Proofs"])
            w.writerows(rows)


# ════════════════════════════════════════════════════════════════════
#  Widgets
# ════════════════════════════════════════════════════════════════════
class Tooltip:
    def __init__(self, widget, text):
        self.w, self.text, self.tip, self.job = widget, text, None, None
        widget.bind("<Enter>", self._sched, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _sched(self, _e):
        self._cancel()
        self.job = self.w.after(450, self._show)

    def _cancel(self):
        if self.job:
            try:
                self.w.after_cancel(self.job)
            except Exception:
                pass
            self.job = None

    def _show(self):
        self.job = None
        if self.tip:
            return
        try:
            x = self.w.winfo_rootx() + self.w.winfo_width() // 2
            y = self.w.winfo_rooty() + self.w.winfo_height() + px(6)
            tw = tk.Toplevel(self.w)
            tw.wm_overrideredirect(True)
            tw.configure(bg=C.PURPLE)
            tk.Label(tw, text=self.text, bg=C.BG2, fg=C.TEXT, font=F.small, padx=px(8), pady=px(4)).pack(padx=1, pady=1)
            tw.update_idletasks()
            tw.geometry(f"+{x - tw.winfo_width() // 2}+{y}")
            tw.attributes("-topmost", True)
            self.tip = tw
        except Exception:
            pass

    def _hide(self, _e=None):
        self._cancel()
        if self.tip:
            self.tip.destroy()
            self.tip = None


def button_variants():
    return {
        "primary": dict(fill=C.PURPLE, hover=C.PURPLE_HI, press=C.PURPLE_LO, fg=C.INK, hfg=C.INK,
                        outline=C.PURPLE, houtline=C.PURPLE_HI),
        "ghost": dict(fill=C.SURFACE2, hover=C.SURFACE3, press=C.BG2, fg=C.SOFT, hfg=C.PURPLE_HI,
                      outline=C.BORDER, houtline=C.PURPLE),
        "danger": dict(fill=C.SURFACE2, hover=C.RED, press="#c2185b", fg=C.RED, hfg=C.INK,
                       outline=C.RED_DIM, houtline=C.RED),
    }


class RButton(tk.Canvas):
    VARIANTS = button_variants()

    def __init__(self, parent, text="", command=None, variant="ghost", icon=None,
                 width=None, height=34, radius=10, font=None, tooltip=None):
        self.font = font or F.btn
        self.text, self.icon, self.command, self.variant = text, icon, command, variant
        self.radius = px(radius)
        self._hover = self._press = self._disabled = False
        h = px(height)
        w = px(width) if width else self._natural_width(h)
        super().__init__(parent, width=w, height=h, bg=parent.cget("bg"),
                         highlightthickness=0, bd=0, cursor="hand2")
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._down)
        self.bind("<ButtonRelease-1>", self._up)
        if tooltip:
            Tooltip(self, tooltip)

    def _parts(self):
        parts = []
        if self.icon:
            parts.append(glyph(self.icon, self.font))
        if self.text:
            parts.append((self.text, self.font))
        return parts

    def _natural_width(self, h):
        if not self.text:
            return h
        parts = self._parts()
        return sum(f.measure(t) for t, f in parts) + (px(8) if len(parts) == 2 else 0) + px(30)

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            w, h = int(self["width"]), int(self["height"])
        v = self.VARIANTS[self.variant]
        if self._disabled:
            fill, fg, ol = C.BG2, C.FAINT, C.BORDER
        elif self._press:
            fill, fg, ol = v["press"], v["hfg"], v["houtline"]
        elif self._hover:
            fill, fg, ol = v["hover"], v["hfg"], v["houtline"]
        else:
            fill, fg, ol = v["fill"], v["fg"], v["outline"]
        rrect(self, 1, 1, w - 2, h - 2, self.radius, fill=fill, outline=ol, width=1)
        parts = self._parts()
        widths = [f.measure(t) for t, f in parts]
        gap = px(8) if len(parts) == 2 else 0
        x = (w - sum(widths) - gap) / 2
        for (t, f), wd in zip(parts, widths):
            self.create_text(x, h / 2, text=t, font=f, fill=fg, anchor="w")
            x += wd + gap

    def _enter(self, _e):
        self._hover = True
        self._draw()

    def _leave(self, _e):
        self._hover = self._press = False
        self._draw()

    def _down(self, _e):
        if not self._disabled:
            self._press = True
            self._draw()

    def _up(self, e):
        if self._disabled:
            return
        was = self._press
        self._press = False
        self._draw()
        if was and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height() and self.command:
            self.command()

    def set(self, text=None, variant=None, icon=None):
        if text is not None:
            self.text = text
        if variant is not None:
            self.variant = variant
        if icon is not None:
            self.icon = icon
        self._draw()

    def set_disabled(self, disabled):
        self._disabled = disabled
        self.config(cursor="arrow" if disabled else "hand2")
        self._draw()


class Card(tk.Canvas):
    """Rounded neon-edged panel. Put children in `.body`; header widgets in `.header_right`."""

    def __init__(self, parent, title=None, pad=14, radius=16, fill=None, border=None, accent=True):
        super().__init__(parent, bg=parent.cget("bg"), highlightthickness=0, bd=0)
        self.fill = fill or C.SURFACE
        self.border = border or C.BORDER
        self.r = px(radius)
        self.accent = accent
        self.body = tk.Frame(self, bg=self.fill)
        self.body.pack(fill=tk.BOTH, expand=True, padx=px(pad), pady=px(pad))
        self.header_right = None
        if title:
            hdr = tk.Frame(self.body, bg=self.fill)
            hdr.pack(fill=tk.X, pady=(0, px(10)))
            tk.Frame(hdr, bg=C.PURPLE, width=px(3), height=px(14)).pack(side=tk.LEFT, padx=(0, px(8)))
            tk.Label(hdr, text=title.upper(), font=F.h, fg=C.TEXT, bg=self.fill).pack(side=tk.LEFT)
            self.header_right = tk.Frame(hdr, bg=self.fill)
            self.header_right.pack(side=tk.RIGHT)
        self.bind("<Configure>", self._draw)

    def _draw(self, _e=None):
        self.delete("deco")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10:
            return
        rrect(self, 1, 1, w - 2, h - 2, self.r, fill=self.fill, outline=self.border, width=1, tags="deco")
        if self.accent:
            self.create_line(self.r + px(2), 1, self.r + px(48), 1, fill=C.PURPLE, width=2, tags="deco")
            self.create_line(w - self.r - px(2), h - 2, w - self.r - px(22), h - 2,
                             fill=C.PURPLE_DIM, width=2, tags="deco")

    def set_border(self, color):
        self.border = color
        self._draw()


class Chip(tk.Canvas):
    def __init__(self, parent, text="", color=None, height=22, dot=True, font=None):
        self.bgc = parent.cget("bg")
        self.text, self.color, self.dot = text, color or C.MUTED, dot
        self.font = font or F.tiny
        super().__init__(parent, height=px(height), width=10, bg=self.bgc, highlightthickness=0)
        self._draw()

    def set(self, text=None, color=None):
        if text is not None:
            self.text = text
        if color is not None:
            self.color = color
        self._draw()

    def _draw(self):
        self.delete("all")
        h = int(self["height"])
        pad, dotw = px(9), (px(12) if self.dot else 0)
        w = self.font.measure(self.text) + 2 * pad + dotw
        self.config(width=w)
        rrect(self, 1, 1, w - 2, h - 2, (h - 2) / 2,
              fill=mix(self.bgc, self.color, 0.14), outline=mix(self.bgc, self.color, 0.5))
        x = pad
        if self.dot:
            r = px(3)
            self.create_oval(x, h / 2 - r, x + 2 * r, h / 2 + r, fill=self.color, outline="")
            x += dotw
        self.create_text(x, h / 2, text=self.text, anchor="w", font=self.font, fill=self.color)


class Toggle(tk.Frame):
    def __init__(self, parent, text, var, sub=None, command=None):
        bgc = parent.cget("bg")
        super().__init__(parent, bg=bgc)
        self.var, self.command = var, command
        self.sw = tk.Canvas(self, width=px(38), height=px(20), bg=bgc, highlightthickness=0, cursor="hand2")
        self.sw.pack(side=tk.RIGHT)
        txt = tk.Frame(self, bg=bgc)
        txt.pack(side=tk.LEFT, fill=tk.X, expand=True)
        labels = [tk.Label(txt, text=text, font=F.body, fg=C.TEXT, bg=bgc, anchor="w", cursor="hand2")]
        if sub:
            labels.append(tk.Label(txt, text=sub, font=F.mono_s, fg=C.MUTED, bg=bgc, anchor="w", cursor="hand2"))
        for lb in labels:
            lb.pack(fill=tk.X)
            lb.bind("<Button-1>", self._flip)
        self.sw.bind("<Button-1>", self._flip)
        var.trace_add("write", lambda *a: self._draw())
        self._draw()

    def _flip(self, _e=None):
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def _draw(self):
        c = self.sw
        c.delete("all")
        w, h, on = px(38), px(20), bool(self.var.get())
        rrect(c, 1, 1, w - 2, h - 2, h / 2, fill=C.PURPLE if on else C.SURFACE3,
              outline=C.PURPLE_HI if on else C.BORDER)
        r = h / 2 - px(4)
        cx = w - 1 - h / 2 if on else 1 + h / 2
        cy = h / 2 - 0.5
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=C.INK if on else C.MUTED, outline="")


class Segmented(tk.Canvas):
    def __init__(self, parent, options, var, command=None, height=30, width=120):
        self.options, self.var, self.command, self._hover = options, var, command, -1
        super().__init__(parent, height=px(height), width=px(width), bg=parent.cget("bg"),
                         highlightthickness=0, cursor="hand2")
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", lambda e: self._set_hover(self._idx(e.x)))
        self.bind("<Leave>", lambda e: self._set_hover(-1))
        var.trace_add("write", lambda *a: self._draw())

    def _idx(self, x):
        n = len(self.options)
        sw = max(1, (self.winfo_width() - 6) / n)
        return min(max(int((x - 3) / sw), 0), n - 1)

    def _set_hover(self, i):
        if i != self._hover:
            self._hover = i
            self._draw()

    def _click(self, e):
        self.var.set(self.options[self._idx(e.x)][0])
        if self.command:
            self.command()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10:
            return
        rrect(self, 1, 1, w - 2, h - 2, px(10), fill=C.BG, outline=C.BORDER)
        n = len(self.options)
        sw = (w - 6) / n
        cur = str(self.var.get())
        for i, (val, label) in enumerate(self.options):
            x1, x2 = 3 + i * sw, 3 + (i + 1) * sw
            sel = str(val) == cur
            if sel:
                rrect(self, x1 + 1, 4, x2 - 1, h - 5, px(8), fill=C.PURPLE, outline="")
            elif i == self._hover:
                rrect(self, x1 + 1, 4, x2 - 1, h - 5, px(8), fill=C.SURFACE3, outline="")
            fg = C.INK if sel else (C.PURPLE_HI if i == self._hover else C.MUTED)
            self.create_text((x1 + x2) / 2, h / 2, text=label, font=F.tiny, fill=fg)


class REntry(tk.Canvas):
    def __init__(self, parent, placeholder="", height=34):
        super().__init__(parent, height=px(height), width=px(100), bg=parent.cget("bg"), highlightthickness=0)
        self.var = tk.StringVar()
        self.placeholder = placeholder
        self.focused = False
        self._ph = False
        self.entry = tk.Entry(self, textvariable=self.var, bg=C.BG, fg=C.TEXT, insertbackground=C.PURPLE_HI,
                              relief="flat", bd=0, font=F.mono, highlightthickness=0,
                              selectbackground=C.PURPLE, selectforeground=C.INK)
        self.win = self.create_window(px(12), px(height) / 2, window=self.entry, anchor="w")
        self.bind("<Configure>", lambda e: self._draw())
        self.entry.bind("<FocusIn>", self._focus_in)
        self.entry.bind("<FocusOut>", self._focus_out)
        self.bind("<Button-1>", lambda e: self.entry.focus_set())
        self._show_placeholder()

    def _draw(self):
        self.delete("frame")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10:
            return
        rrect(self, 1, 1, w - 2, h - 2, px(10), fill=C.BG,
              outline=C.PURPLE if self.focused else C.BORDER, tags="frame")
        self.tag_lower("frame")
        self.itemconfig(self.win, width=max(10, w - px(24)))

    def _show_placeholder(self):
        if not self.var.get():
            self._ph = True
            self.entry.config(fg=C.FAINT)
            self.var.set(self.placeholder)

    def _focus_in(self, _e):
        self.focused = True
        if self._ph:
            self._ph = False
            self.var.set("")
            self.entry.config(fg=C.TEXT)
        self._draw()

    def _focus_out(self, _e):
        self.focused = False
        self._show_placeholder()
        self._draw()

    def get(self):
        return "" if self._ph else self.var.get().strip()

    def set(self, value):
        if value:
            self._ph = False
            self.entry.config(fg=C.TEXT)
            self.var.set(value)
        else:
            self.var.set("")
            if not self.focused:
                self._show_placeholder()


class CyberScrollbar(tk.Canvas):
    def __init__(self, parent, command, width=8):
        super().__init__(parent, width=px(width), height=px(20), bg=parent.cget("bg"), highlightthickness=0)
        self.command, self.lo, self.hi = command, 0.0, 1.0
        self._drag, self._hover = None, False
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", lambda e: self._end())
        self.bind("<Enter>", lambda e: self._hov(True))
        self.bind("<Leave>", lambda e: self._hov(False))

    def _hov(self, v):
        self._hover = v
        self._draw()

    def _end(self):
        self._drag = None
        self._draw()

    def set(self, lo, hi):
        self.lo, self.hi = float(lo), float(hi)
        self._draw()

    def _thumb(self):
        h = self.winfo_height()
        y1 = self.lo * h
        y2 = max(self.hi * h, y1 + px(30))
        return y1, min(y2, h)

    def _draw(self):
        self.delete("all")
        if self.hi - self.lo >= 0.999:
            return
        w = self.winfo_width()
        y1, y2 = self._thumb()
        col = C.PURPLE if (self._hover or self._drag) else C.PURPLE_DIM
        rrect(self, 1, y1 + 1, w - 1, y2 - 1, (w - 2) / 2, fill=col, outline="")

    def _press(self, e):
        y1, y2 = self._thumb()
        if y1 <= e.y <= y2:
            self._drag = (e.y, self.lo)
        else:
            self.command("scroll", 1 if e.y > y2 else -1, "pages")

    def _motion(self, e):
        if self._drag:
            y0, lo0 = self._drag
            self.command("moveto", lo0 + (e.y - y0) / max(1, self.winfo_height()))


class ScrollFrame(tk.Frame):
    def __init__(self, parent, bg):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.sb = CyberScrollbar(self, self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.sb.pack(side=tk.RIGHT, fill=tk.Y, padx=(px(3), 0))
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_resize)
        self.on_resize = None

    def _on_resize(self, e):
        self.canvas.itemconfig(self.win, width=e.width)
        if self.on_resize:
            self.on_resize()

    def wheel(self, delta):
        lo, hi = self.canvas.yview()
        if lo <= 0 and hi >= 1:
            return
        steps = max(1, abs(int(delta / 120)))
        self.canvas.yview_scroll(-steps if delta > 0 else steps, "units")


class NeonLine(tk.Canvas):
    def __init__(self, parent, height=2):
        super().__init__(parent, height=px(height), bg=C.BG, highlightthickness=0)
        self.pos = 0
        self.bind("<Configure>", self._draw)
        self._anim()

    def _draw(self, _e=None):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        step = px(6)
        n = max(1, w // step)
        for i in range(n):
            k = 1 - i / n
            self.create_rectangle(i * step, 0, (i + 1) * step, h, outline="",
                                  fill=mix(C.PURPLE_DIM, C.PURPLE, k ** 1.6))
        for i, t in enumerate((0.3, 0.6, 1.0, 0.6, 0.3)):
            self.create_rectangle(0, 0, 0, 0, outline="", fill=mix(C.PURPLE, "#ffffff", t * 0.7), tags=f"p{i}")

    def _anim(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w > 1:
            seg = px(26)
            self.pos = (self.pos + px(7)) % (w + seg * 5)
            for i in range(5):
                x = self.pos - seg * 5 + i * seg
                self.coords(f"p{i}", x, 0, x + seg, h)
        self.after(30, self._anim)


class Header(tk.Frame):
    def __init__(self, parent, on_settings=None, on_worker=None):
        super().__init__(parent, bg=C.BG)
        top = tk.Frame(self, bg=C.BG)
        top.pack(fill=tk.X, padx=px(16), pady=(px(10), px(8)))
        self.logo = tk.Canvas(top, width=px(640), height=px(56), bg=C.BG, highlightthickness=0)
        self.logo.pack(side=tk.LEFT)
        self._draw_logo()
        right = tk.Frame(top, bg=C.BG)
        right.pack(side=tk.RIGHT)
        self.settings_btn = RButton(right, icon="settings", height=34, radius=10, command=on_settings,
                                    tooltip="Settings — colours, system log, clock, admin options")
        self.settings_btn.pack(side=tk.RIGHT, padx=(px(14), 0))
        # worker on duty (time clock): name + status, click = switch worker
        wk = tk.Frame(right, bg=C.BG, cursor="hand2")
        wk.pack(side=tk.RIGHT, padx=(px(14), 0))
        g, gf = glyph("person", F.h2)
        self.worker_icon = tk.Label(wk, text=g, font=gf, fg=C.PURPLE, bg=C.BG, cursor="hand2")
        self.worker_icon.pack(side=tk.LEFT, padx=(0, px(8)))
        wtxt = tk.Frame(wk, bg=C.BG, cursor="hand2")
        wtxt.pack(side=tk.LEFT)
        self.worker_lbl = tk.Label(wtxt, text="", font=F.h2, fg=C.TEXT, bg=C.BG, anchor="w", cursor="hand2")
        self.worker_lbl.pack(fill=tk.X)
        self.worker_sub = tk.Label(wtxt, text="", font=F.tiny, fg=C.MUTED, bg=C.BG, anchor="w", cursor="hand2")
        self.worker_sub.pack(fill=tk.X)
        if on_worker:
            for w in (wk, wtxt, self.worker_icon, self.worker_lbl, self.worker_sub):
                w.bind("<Button-1>", lambda e: on_worker(e.widget))
        Tooltip(self.worker_lbl, "Worker on duty — click to switch")
        tk.Frame(right, bg=C.BORDER, width=1, height=px(34)).pack(side=tk.RIGHT, padx=(px(14), 0))
        # wall clock: small analog face + time + date
        clk = tk.Frame(right, bg=C.BG)
        clk.pack(side=tk.RIGHT, padx=(px(14), 0))
        self.face = tk.Canvas(clk, width=px(34), height=px(34), bg=C.BG, highlightthickness=0)
        self.face.pack(side=tk.LEFT, padx=(0, px(8)))
        txt = tk.Frame(clk, bg=C.BG)
        txt.pack(side=tk.LEFT)
        self.clock = tk.Label(txt, text="--:--", font=F.mono_b, fg=C.PURPLE_HI, bg=C.BG, anchor="w")
        self.clock.pack(fill=tk.X)
        self.date_lbl = tk.Label(txt, text="", font=F.tiny, fg=C.MUTED, bg=C.BG, anchor="w")
        self.date_lbl.pack(fill=tk.X)
        self.link_chip = Chip(right, "LINKS 0", C.MUTED, height=26)
        self.link_chip.pack(side=tk.RIGHT, padx=(px(6), 0))
        self.net_chip = Chip(right, "NET 0", C.MUTED, height=26)
        self.net_chip.pack(side=tk.RIGHT, padx=(px(6), 0))
        self.temp_chip = Chip(right, "TEMP --", C.MUTED, height=26)
        self.temp_chip.pack(side=tk.RIGHT, padx=(px(6), 0))
        self.dev_chip = Chip(right, "DEVICES 0", C.PURPLE, height=26)
        self.dev_chip.pack(side=tk.RIGHT)
        NeonLine(self, height=2).pack(fill=tk.X)
        self._cursor = True
        self._blink()

    def _draw_logo(self):
        c = self.logo
        h = px(56)
        cx, cy, r = px(26), h / 2, px(19)
        outer = []
        inner = []
        for i in range(6):
            a = math.radians(60 * i - 90)
            outer += [cx + r * math.cos(a), cy + r * math.sin(a)]
            inner += [cx + r * 0.42 * math.cos(a), cy + r * 0.42 * math.sin(a)]
        c.create_polygon(outer, outline=C.PURPLE, fill=C.BG2, width=2)
        c.create_polygon(inner, outline="", fill=C.PURPLE)
        x, ty = px(60), px(21)
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, -1), (1, 1), (-1, 1), (1, -1)):
            c.create_text(x + dx, ty + dy, text="SCIENTIST", font=F.title, fill=C.PURPLE_DIM, anchor="w")
        c.create_text(x, ty, text="SCIENTIST", font=F.title, fill=C.PURPLE_HI, anchor="w")
        tw = F.title.measure("SCIENTIST")
        self.cursor_id = c.create_rectangle(x + tw + px(5), ty - px(9), x + tw + px(16), ty + px(9),
                                            fill=C.PURPLE, outline="")
        vx = x + tw + px(28)
        rrect(c, vx, ty - px(9), vx + px(46), ty + px(9), px(9), fill=mix(C.BG, C.PURPLE, 0.15), outline=C.PURPLE_DIM)
        c.create_text(vx + px(23), ty, text="v" + ".".join(APP_VERSION.split(".")[:2]), font=F.tiny,
                      fill=C.PURPLE_HI)
        c.create_text(x + 1, px(45), text="ADB MULTI-MIRROR  //  SCREEN CONTROL MATRIX",
                      font=F.mono, fill=C.MUTED, anchor="w")

    def set_worker(self, name, status, color):
        name = elide(name, F.h2, px(190))
        if self.worker_lbl.cget("text") != name:
            self.worker_lbl.config(text=name)
        self.worker_sub.config(text=status, fg=color)
        self.worker_icon.config(fg=color if color != C.MUTED else C.PURPLE)

    def set_time(self, now, h24=False):
        if h24:
            self.clock.config(text=now.strftime("%H:%M"))
        else:
            self.clock.config(text=now.strftime("%I:%M %p").lstrip("0"))
        self.date_lbl.config(text=now.strftime("%a %d %b").upper())
        c = self.face
        c.delete("all")
        n = px(34)
        cx = cy = n / 2
        r = n / 2 - 2
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=C.BG2, outline=C.PURPLE, width=2)
        for i in range(12):
            a = math.radians(i * 30)
            r1 = r - (px(4) if i % 3 == 0 else px(2))
            c.create_line(cx + r1 * math.sin(a), cy - r1 * math.cos(a),
                          cx + (r - 1) * math.sin(a), cy - (r - 1) * math.cos(a),
                          fill=C.PURPLE_HI if i % 3 == 0 else C.FAINT)
        sec = now.second
        mins = now.minute + sec / 60
        hrs = (now.hour % 12) + mins / 60
        for val, length, width, col in ((hrs / 12, 0.50, 2, C.TEXT), (mins / 60, 0.72, 2, C.PURPLE_HI),
                                        (sec / 60, 0.80, 1, C.PURPLE)):
            a = math.radians(val * 360)
            c.create_line(cx, cy, cx + r * length * math.sin(a), cy - r * length * math.cos(a),
                          fill=col, width=width, capstyle="round")
        c.create_oval(cx - 2, cy - 2, cx + 2, cy + 2, fill=C.PURPLE, outline="")

    def _blink(self):
        self._cursor = not self._cursor
        self.logo.itemconfig(self.cursor_id, state="normal" if self._cursor else "hidden")
        self.after(530, self._blink)


class EmptyState(tk.Canvas):
    def __init__(self, parent):
        super().__init__(parent, bg=C.BG, highlightthickness=0, width=px(400), height=px(300))
        self.bind("<Configure>", self._draw)

    def _draw(self, _e=None):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 50:
            return
        rrect(self, 2, 2, w - 3, h - 3, px(20), fill=C.BG2, outline=C.BORDER)
        step = px(28)
        for x in range(step, w - step // 2, step):
            for y in range(step, h - step // 2, step):
                self.create_rectangle(x, y, x + 2, y + 2, outline="", fill=C.SURFACE3)
        m, ln = px(22), px(30)
        for (x, y, sx, sy) in ((m, m, 1, 1), (w - m, m, -1, 1), (m, h - m, 1, -1), (w - m, h - m, -1, -1)):
            self.create_line(x, y + sy * ln, x, y, x + sx * ln, y, fill=C.PURPLE, width=2)
        cx, cy = w / 2, h / 2 - px(40)
        for rr, col in ((px(62), C.SURFACE), (px(50), mix(C.BG2, C.PURPLE, 0.12))):
            self.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, fill=col, outline=C.PURPLE_DIM)
        g, gf = glyph("phone", F.big)
        self.create_text(cx, cy, text=g, font=F.icon_big if F.icon else F.big, fill=C.PURPLE_HI)
        self.create_text(cx, cy + px(92), text="NO ACTIVE LINKS", font=F.big, fill=C.TEXT)
        self.create_text(cx, cy + px(122), text="Select devices on the left and hit START MIRROR",
                         font=F.body, fill=C.MUTED)
        self.create_text(cx, cy + px(146), text="or double-click a device  ·  Ctrl+M", font=F.mono, fill=C.FAINT)


class Toast:
    def __init__(self, root):
        self.root, self.c, self.job = root, None, None

    def show(self, text, color=None):
        color = color or C.PURPLE
        self.hide()
        w, h = F.body.measure(text) + px(52), px(44)
        c = tk.Canvas(self.root, width=w, height=h, bg=C.SURFACE, highlightthickness=0)
        rrect(c, 1, 1, w - 2, h - 2, px(12), fill=C.SURFACE2, outline=color, width=1)
        c.create_rectangle(px(14), h / 2 - px(8), px(17), h / 2 + px(8), fill=color, outline="")
        c.create_text(px(28), h / 2, text=text, anchor="w", font=F.body, fill=C.TEXT)
        c.place(relx=1, rely=1, anchor="se", x=-px(28), y=-px(52))
        tk.Misc.lift(c)
        self.c = c
        self.job = self.root.after(2800, self.hide)

    def hide(self):
        if self.job:
            self.root.after_cancel(self.job)
            self.job = None
        if self.c:
            self.c.destroy()
            self.c = None


# ════════════════════════════════════════════════════════════════════
#  Device list
# ════════════════════════════════════════════════════════════════════
class DeviceRow(tk.Canvas):
    def __init__(self, parent, serial, dstate, on_toggle, on_double, on_menu):
        super().__init__(parent, height=px(62), bg=parent.cget("bg"), highlightthickness=0, cursor="hand2")
        self.serial, self.dstate = serial, dstate
        self.selected = self.live = self.net = self._hover = False
        self.meta = {}
        self.num, self.name = None, ""
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", lambda e: self._hov(True))
        self.bind("<Leave>", lambda e: self._hov(False))
        self.bind("<Button-1>", lambda e: on_toggle(self))
        self.bind("<Double-Button-1>", lambda e: on_double(self))
        self.bind("<Button-3>", lambda e: on_menu(self, e))

    @property
    def usable(self):
        return self.dstate == "device"

    def _hov(self, v):
        self._hover = v
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 40:
            return
        if self.selected:
            fill, ol = mix(C.SURFACE2, C.PURPLE, 0.14), C.PURPLE
        elif self._hover and self.usable:
            fill, ol = C.SURFACE3, C.BORDER_HI
        else:
            fill, ol = C.SURFACE2, C.BORDER
        rrect(self, 1, 1, w - 2, h - 2, px(12), fill=fill, outline=ol)
        cb, x0 = px(18), px(12)
        y0 = (h - cb) / 2
        if self.usable:
            rrect(self, x0, y0, x0 + cb, y0 + cb, px(6),
                  fill=C.PURPLE if self.selected else C.BG, outline=C.PURPLE if self.selected else C.BORDER_HI)
            if self.selected:
                self.create_line(x0 + cb * .26, y0 + cb * .52, x0 + cb * .44, y0 + cb * .70, x0 + cb * .76,
                                 y0 + cb * .32, fill=C.INK, width=2, capstyle="round", joinstyle="round")
        else:
            g, gf = glyph("phone", F.btn)
            self.create_text(x0 + cb / 2, h / 2, text=g, font=gf, fill=C.FAINT)
        # unit number badge
        bx = x0 + cb + px(9)
        bw, bh = px(36), px(30)
        num_txt = f"{self.num:02d}" if self.num is not None else "--"
        rrect(self, bx, (h - bh) / 2, bx + bw, (h + bh) / 2, px(9),
              fill=mix(C.SURFACE2, C.PURPLE, 0.22 if self.usable else 0.06),
              outline=C.PURPLE if self.selected else C.PURPLE_DIM)
        self.create_text(bx + bw / 2, h / 2, text=num_txt, font=F.h2, fill=C.PURPLE_HI if self.usable else C.FAINT)
        tx = bx + bw + px(10)
        right_w = px(92)
        maxw = w - tx - right_w - px(6)
        model = self.meta.get("model") or ""
        title = self.name or model or self.serial
        self.create_text(tx, h / 2 - px(9), anchor="w", font=F.btn, fill=C.TEXT if self.usable else C.MUTED,
                         text=elide(title, F.btn, maxw))
        if self.usable:
            parts = []
            if self.name and model:
                parts.append(model)
            parts.append(self.serial)
            if self.meta.get("android"):
                parts.append(f"A{self.meta['android']}")
            sub, subc = " · ".join(parts), C.MUTED
        else:
            sub = {"unauthorized": "accept RSA prompt on device", "offline": "device offline"}.get(
                self.dstate, self.dstate)
            subc = C.AMBER if self.dstate == "unauthorized" else C.RED
        self.create_text(tx, h / 2 + px(10), anchor="w", font=F.mono_s, fill=subc, text=elide(sub, F.mono_s, maxw))

        # right column: status flags (top), battery + temperature (bottom)
        rx = w - px(12)
        ty = h / 2 - px(10)
        if not self.usable:
            self.create_text(rx, ty, anchor="e", text=self.dstate.upper(), font=F.tiny,
                             fill=C.AMBER if self.dstate == "unauthorized" else C.RED)
        else:
            x = rx
            for on, label, col in ((self.live, "● LIVE", C.GREEN), (self.net, "⇅ NET", C.PURPLE_HI)):
                if on:
                    self.create_text(x, ty, anchor="e", text=label, font=F.tiny, fill=col)
                    x -= F.tiny.measure(label) + px(8)
        batt = self.meta.get("battery")
        by = h / 2 + px(10)
        x = rx
        if self.usable and batt is not None:
            bw, bh = px(20), px(10)
            bx2, bx1 = rx - px(3), rx - px(3) - bw
            col = C.PURPLE_HI if self.meta.get("charging") else C.GREEN if batt > 50 else C.AMBER if batt > 20 else C.RED
            self.create_rectangle(bx1, by - bh / 2, bx2, by + bh / 2, outline=C.MUTED)
            self.create_rectangle(bx2 + 1, by - bh * .2, bx2 + px(3), by + bh * .2, outline="", fill=C.MUTED)
            self.create_rectangle(bx1 + 2, by - bh / 2 + 2, bx1 + 2 + (bw - 3) * batt / 100, by + bh / 2 - 1,
                                  outline="", fill=col)
            self.create_text(bx1 - px(4), by, anchor="e", text=f"{batt}%", font=F.mono_s, fill=C.SOFT)
            x = bx1 - px(4) - F.mono_s.measure(f"{batt}%") - px(6)
        temp = self.meta.get("temp")
        if self.usable and temp is not None:
            hot = temp >= HOT_TEMP_C
            self.create_text(x, by, anchor="e", text=f"{temp:.0f}°", font=F.mono_s,
                             fill=C.RED if hot else C.AMBER if temp >= HOT_TEMP_C - 4 else C.MUTED)


class DeviceList(tk.Frame):
    def __init__(self, parent, on_change, on_double, on_menu):
        super().__init__(parent, bg=parent.cget("bg"))
        self.on_change, self.on_double, self.on_menu = on_change, on_double, on_menu
        self.rows = {}
        self._packed = []
        self._autoselected = False
        self.empty = tk.Label(self, text="NO DEVICES DETECTED\nplug in a phone & enable USB debugging",
                              font=F.small, fg=C.MUTED, bg=self.cget("bg"), justify="center", pady=px(18))
        self.empty.pack(fill=tk.X)

    def set_devices(self, devs, info_map, units):
        """devs: [(serial, state)], units: {serial: {"num": int, "name": str}}"""
        serials = [s for s, _ in devs]
        for s, st in devs:
            row = self.rows.get(s)
            if row is None:
                row = self.rows[s] = DeviceRow(self, s, st, self._toggle, self._double, self.on_menu)
            row.dstate = st
            row.meta = info_map.get(s, row.meta)
            u = units.get(s, {})
            row.num, row.name = u.get("num"), u.get("name", "")
            if not row.usable:
                row.selected = False
        for s in list(self.rows):
            if s not in serials:
                self.rows.pop(s).destroy()

        def key(s):
            r = self.rows[s]
            return (0 if r.usable else 1, r.num if r.num is not None else 9999, s)
        order = sorted(serials, key=key)
        if order != self._packed:
            for r in self.rows.values():
                r.pack_forget()
            self.rows = {s: self.rows[s] for s in order}
            for r in self.rows.values():
                r.pack(fill=tk.X, pady=(0, px(6)))
            self._packed = order
        if self.rows:
            self.empty.pack_forget()
        else:
            self.empty.pack(fill=tk.X)
        if not self._autoselected and not self.selected():
            first = next((r for r in self.rows.values() if r.usable), None)
            if first:
                first.selected = True
                self._autoselected = True
        for r in self.rows.values():
            r._draw()
        self.on_change()

    def _toggle(self, row):
        if row.usable:
            row.selected = not row.selected
            row._draw()
            self.on_change()

    def _double(self, row):
        if row.usable:
            row.selected = True
            row._draw()
            self.on_change()
            self.on_double(row.serial)

    def selected(self):
        return [s for s, r in self.rows.items() if r.selected and r.usable]

    def usable(self):
        return [s for s, r in self.rows.items() if r.usable]

    def usable_count(self):
        return len(self.usable())

    def select_all(self):
        for r in self.rows.values():
            r.selected = r.usable
            r._draw()
        self.on_change()

    def clear(self):
        for r in self.rows.values():
            r.selected = False
            r._draw()
        self.on_change()

    def set_info(self, serial, meta):
        r = self.rows.get(serial)
        if r:
            r.meta = meta
            r._draw()

    def set_flag(self, serial, live=None, net=None):
        r = self.rows.get(serial)
        if not r:
            return
        changed = False
        if live is not None and r.live != live:
            r.live, changed = live, True
        if net is not None and r.net != net:
            r.net, changed = net, True
        if changed:
            r._draw()


# ════════════════════════════════════════════════════════════════════
#  Mirror panel
# ════════════════════════════════════════════════════════════════════
class MirrorPanel:
    PHASES = {
        "boot": (C.AMBER, "BOOT"), "live": (C.GREEN, "LIVE"), "window": (C.AMBER, "WINDOWED"),
        "lost": (C.RED, "OFFLINE"), "fail": (C.RED, "FAILED"), "queued": (C.MUTED, "QUEUED"),
    }

    def __init__(self, app, parent, serial, meta, opts, tile_w, unit=None):
        self.app, self.serial, self.opts = app, serial, dict(opts)
        self.proc = self.hwnd = None
        self._gen, self._closed, self._busy = 0, False, False
        self.screen_off = False
        self.tweaks = {}                 # phone settings changed for this mirror → value to restore
        self._phase, self._dots = "boot", 0
        self.window_title = ""
        self.port_slot = None            # own scrcpy port block, so 20+ phones never fight over ports
        self._boot_t = 0.0

        self.focused = False             # the big, controllable tile
        self.focus_w = None
        self.pop = None                  # floating window while the phone is popped out of the grid
        self.pop_holder = None
        self._drag = None
        self.input_enabled = True        # False = view-only (mirror window disabled)
        self.mode = None                 # full / compact / mini
        self.card = Card(parent, pad=10, radius=18)
        b = self.card.body
        unit = unit or {}
        self.hdr = hdr = tk.Frame(b, bg=C.SURFACE)
        self.num_chip = Chip(hdr, self._num_text(unit.get("num")), C.PURPLE_HI, dot=False, font=F.h, height=28)
        self.num_chip.pack(side=tk.LEFT, padx=(0, px(6)))
        self.chip = Chip(hdr, "BOOT", C.AMBER)
        self.chip.pack(side=tk.LEFT)
        RButton(hdr, icon="close", height=26, radius=8, command=self.close, tooltip="Close link").pack(side=tk.RIGHT)
        self.names = names = tk.Frame(hdr, bg=C.SURFACE)
        # mini tiles: a thin strip with a status dot and the unit number
        self.strip = tk.Frame(b, bg=C.SURFACE)
        self.strip_dot = tk.Canvas(self.strip, width=px(8), height=px(8), bg=C.SURFACE, highlightthickness=0)
        self.strip_dot.pack(side=tk.LEFT, padx=(px(1), px(3)))
        self.strip_lbl = tk.Label(self.strip, text=self._num_text(unit.get("num")), font=F.tiny, fg=C.PURPLE_HI,
                                  bg=C.SURFACE, anchor="w")
        self.strip_lbl.pack(side=tk.LEFT)
        self.meta, self.unit_name = dict(meta), unit.get("name", "")
        self.title_lbl = tk.Label(names, text=self.unit_name or meta.get("model") or serial, font=F.btn, fg=C.TEXT,
                                  bg=C.SURFACE, anchor="w", width=1)
        self.title_lbl.pack(fill=tk.X)
        self.sub_lbl = tk.Label(names, text=self._subtitle(meta), font=F.mono_s, fg=C.MUTED,
                                bg=C.SURFACE, anchor="w", width=1)
        self.sub_lbl.pack(fill=tk.X)

        self.tile_w = tile_w
        tw, th = self._tile_dims()
        self.holder = tk.Frame(b, bg="#000000", width=tw, height=th, highlightthickness=1,
                               highlightbackground=C.BORDER, highlightcolor=C.BORDER)
        self.holder.pack()
        self.holder.pack_propagate(False)
        self.holder.bind("<Configure>", self._resize)
        self.ph = tk.Label(self.holder, text="", font=F.mono_b, fg=C.PURPLE, bg="#000000", justify="center")
        self.ph.place(relx=0.5, rely=0.5, anchor="center")

        self.tb = tb = tk.Frame(b, bg=C.SURFACE)
        for icon, tip, cmd in (
            ("popout", "Pop out into its own movable window (or drag the big tile's header)", self.toggle_popout),
            ("refresh", "Reconnect", self.restart),
            ("camera", "Screenshot → ./screenshots", self.screenshot),
            ("recents", "Recent apps", lambda: self.key(187, "RECENTS")),
            ("home", "Home", lambda: self.key(3, "HOME")),
            ("back", "Back", lambda: self.key(4, "BACK")),
        ):
            RButton(tb, icon=icon, height=32, radius=9, command=cmd, tooltip=tip).pack(side=tk.RIGHT, padx=(px(4), 0))
        self.power_btn = RButton(tb, "SCREEN OFF", icon="power", height=32, radius=9, width=60,
                                 command=self.toggle_screen,
                                 tooltip="Turn the physical display off/on — the mirror keeps running")
        self.power_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        for w in (self.card, hdr, names, self.title_lbl, self.sub_lbl, self.num_chip, self.chip, self.strip,
                  self.strip_lbl, self.strip_dot, self.holder, self.ph):
            w.bind("<Double-Button-1>", lambda e: self._dock_or_focus())
        # drag the big tile by its header -> it pops out and follows the mouse
        for w in (hdr, names, self.title_lbl, self.sub_lbl, self.num_chip, self.chip):
            w.bind("<ButtonPress-1>", self._drag_start, add="+")
            w.bind("<B1-Motion>", self._drag_move, add="+")
            w.bind("<ButtonRelease-1>", self._drag_end, add="+")
        self.apply_layout()
        self._animate()

    # ----- info / layout -----
    def _subtitle(self, meta):
        return self.serial + (f" · Android {meta['android']}" if meta.get("android") else "")

    @staticmethod
    def _num_text(num):
        return f"#{num:02d}" if num is not None else "#--"

    def aspect(self):
        res = self.meta.get("res")
        return (res[1] / res[0]) if res and res[0] else DEFAULT_ASPECT

    def _tile_dims(self):
        w = self.focus_w if self.focused and self.focus_w else px(self.tile_w)
        return w, int(w * self.aspect())

    def apply_layout(self):
        """Size the mirror and pick the tile style: full (header + toolbar), compact (number + status) or
        mini (thin number strip). The focused tile is always full."""
        tw, th = self._tile_dims()
        self.holder.config(width=tw, height=th)
        mode = "full" if self.focused else tile_mode(self.tile_w)
        self.ph.config(font=F.mono_b if mode == "full" else F.tiny, wraplength=max(20, tw - 8))
        if mode == self.mode:
            return
        self.mode = mode
        pad, radius = TILE_STYLE[mode][:2]
        for w in (self.hdr, self.strip, self.tb):
            w.pack_forget()
        if mode == "mini":
            self.strip.pack(fill=tk.X, pady=(0, px(2)), before=self.holder)
        else:
            self.hdr.pack(fill=tk.X, pady=(0, px(8 if mode == "full" else 6)), before=self.holder)
            if mode == "full":
                self.names.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(px(8), px(6)))
            else:
                self.names.pack_forget()
        if mode == "full":
            self.tb.pack(fill=tk.X, pady=(px(8), 0))
        self.card.body.pack_configure(padx=px(pad), pady=px(pad))
        self.card.r = px(radius)
        self.card._draw()

    def set_focus(self, on, w=None):
        self.focused = bool(on)
        self.focus_w = w if on else None
        self.apply_layout()
        self._resize()

    def set_input(self, on):
        """View-only tiles show the phone but can't be clicked (a double-click enlarges them instead)."""
        self.input_enabled = bool(on)
        if self.hwnd and WIN32.ok:
            try:
                WIN32.enable(self.hwnd, self.input_enabled or self.pop is not None)
            except Exception:
                pass

    def set_info(self, meta):
        if self._closed:
            return
        old_aspect = self.aspect()
        self.meta = dict(meta)
        if not self.unit_name and meta.get("model"):
            self.title_lbl.config(text=meta["model"])
        self.sub_lbl.config(text=self._subtitle(meta))
        if abs(self.aspect() - old_aspect) > 0.01:
            self.set_tile(self.tile_w)
            self.app.root.after(30, self.app._relayout)

    def set_unit(self, unit):
        if self._closed:
            return
        self.num_chip.set(self._num_text(unit.get("num")))
        self.strip_lbl.config(text=self._num_text(unit.get("num")))
        self.unit_name = unit.get("name", "")
        self.title_lbl.config(text=self.unit_name or self.meta.get("model") or self.serial)

    def set_tile(self, tile_w):
        self.tile_w = tile_w
        self.apply_layout()

    @property
    def active(self):
        return self.proc is not None and self._phase in ("boot", "live", "window")

    def _set_phase(self, phase):
        if self._closed:
            return
        self._phase = phase
        col, label = self.PHASES[phase]
        self.chip.set(label, col)
        self.strip_dot.delete("all")
        d = px(8)
        self.strip_dot.create_oval(1, 1, d - 1, d - 1, fill=col, outline="")
        border = C.PURPLE if phase == "live" else C.RED_DIM if phase in ("lost", "fail") else C.BORDER
        self.card.set_border(border)
        self.holder.config(highlightbackground=C.PURPLE_DIM if phase == "live" else border)
        texts = {
            "window": ("MIRROR RUNNING IN\nA SEPARATE WINDOW", C.AMBER),
            "lost": ("LINK TERMINATED\n\npress ↻ to reconnect", C.RED),
            "fail": ("LAUNCH FAILED\n\ncheck the log below", C.RED),
        }
        if phase in texts:
            t, c = texts[phase]
            self.ph.config(text=t, fg=c)
        self.app.on_panel_state()

    def _animate(self):
        if self._closed:
            return
        if self._phase == "boot":
            self._dots = (self._dots + 1) % 4
            slow = self._boot_t and time.time() - self._boot_t > 25
            self.ph.config(text="ESTABLISHING LINK" + "." * self._dots + " " * (3 - self._dots)
                           + ("\n\nstill connecting — many phones\nstart one after another" if slow else ""),
                           fg=C.PURPLE)
        elif self._phase == "queued":
            self.ph.config(text="WAITING TO CONNECT\n\nphones start one by one", fg=C.MUTED)
        self.app.root.after(400, self._animate)

    # ----- process -----
    def _build_cmd(self):
        o = self.opts
        cmd = ["scrcpy", "-s", self.serial, "--window-title", self.window_title,
               "--shortcut-mod=lalt,lsuper"]  # the screen ON/OFF buttons press Alt+O / Alt+Shift+O
        if o.get("stay_awake"):
            cmd.append("--stay-awake")
        if o.get("turn_screen_off"):
            cmd.append("--turn-screen-off")
        if o.get("audio_mode") in ("phone", "mute"):   # phone: sound stays on the phone; mute: + volume 0
            cmd.append("--no-audio")
        if o.get("show_touches"):
            cmd.append("--show-touches")
        if str(o.get("max_size", "0")) != "0":
            cmd += ["--max-size", str(o["max_size"])]
        if str(o.get("max_fps", "0")) != "0":
            cmd += ["--max-fps", str(o["max_fps"])]
        if self.port_slot is not None:
            # scrcpy's default range 27183:27199 is only 17 ports: with 20 phones starting, the last ones
            # cannot listen and never link. Every mirror gets its own block instead.
            base = MIRROR_PORT_BASE + self.port_slot * MIRROR_PORT_SPAN
            cmd.append(f"--port={base}:{base + MIRROR_PORT_SPAN - 1}")
        if WIN32.ok:
            # open off-screen: the window is pulled into the tile, it never shows up as a separate window
            cmd += ["--window-x=-20000", "--window-y=-20000"]
        return cmd

    def start(self):
        if self._closed:
            return
        self._gen += 1
        gen = self._gen
        self.window_title = f"Scrcpy_{self.serial.replace(':', '_')}_{int(time.time() * 1000)}"
        if self.port_slot is None:
            self.port_slot = self.app.alloc_port_slot()
        self._boot_t = time.time()
        cmd = self._build_cmd()
        self.screen_off = bool(self.opts.get("turn_screen_off"))
        self._update_power()
        self._set_phase("boot")
        self.app.log(f"[{self.serial}] start: {' '.join(cmd)}", "sys")
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", bufsize=1, creationflags=CREATE_NO_WINDOW,
            )
        except Exception as e:
            self.proc = None
            self.app.log(f"[{self.serial}] launch failed: {e}", "err")
            self._set_phase("fail")
            return
        bg_thread(self._reader, self.proc, gen)
        bg_thread(self._apply_tweaks, gen)
        self._try_embed(0, gen)

    def _apply_tweaks(self, gen):
        """Phone-side options scrcpy has no flag for: pointer trace for mirror clicks, muted media volume."""
        want = {}
        if self.opts.get("show_clicks"):
            want["pointer_location"] = "1"
        if self.opts.get("audio_mode") == "mute":
            want["volume"] = "0"
        for key, value in want.items():
            if self._closed or gen != self._gen:
                return
            try:
                prev = self.tweaks.get(key) or _tweak_get(self.serial, key)
                if prev is None:
                    self.app.log(f"[{self.serial}] could not read {key}; leaving it alone", "warn")
                    continue
                if key not in self.tweaks:
                    self.tweaks[key] = prev
                    self.app.ui(self.app.remember_tweak, self.serial, key, prev)
                _tweak_set(self.serial, key, value)
                self.app.log(f"[{self.serial}] " + {"pointer_location": "showing mirror clicks (pointer trace)",
                                                     "volume": "media volume muted"}[key], "info")
            except Exception as e:
                self.app.log(f"[{self.serial}] {key}: {e}", "err")

    def restore_tweaks(self):
        for key, prev in list(self.tweaks.items()):
            try:
                _tweak_set(self.serial, key, prev)
                self.app.ui(self.app.forget_tweak, self.serial, key)
            except Exception as e:
                self.app.log(f"[{self.serial}] restore {key}: {e}", "err")
            self.tweaks.pop(key, None)

    def _reader(self, proc, gen):
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if "Texture:" in line:      # first video frame is up: now it is safe to embed
                    self._frame_gen = gen
                if line:
                    lvl = "err" if "ERROR" in line else "warn" if "WARN" in line else "info"
                    self.app.log(f"[{self.serial}] {line}", lvl)
        except Exception:
            pass
        code = proc.wait()
        self.app.ui(self._on_exit, gen, code)

    def _on_exit(self, gen, code):
        if self._closed or gen != self._gen:
            return
        self.proc = self.hwnd = None
        self.app.log(f"[{self.serial}] scrcpy exited (code {code})", "warn")
        self._set_phase("lost")
        self.app.on_link_lost(self)

    def _try_embed(self, attempt, gen):
        if self._closed or gen != self._gen or not (self.proc and self.proc.poll() is None):
            return
        if not WIN32.ok:
            self._set_phase("window")
            return
        hwnd = WIN32.find(self.window_title)
        # Embed only after the first video frame: scrcpy 4.x sizes its window on that frame from the
        # screen it is on, and a window already moved inside a tile crashes it (divide by zero, exit
        # 0xC0000094). The window waits off-screen until then. 60 s without the frame line: embed anyway.
        if hwnd and (getattr(self, "_frame_gen", None) == gen or attempt >= 300):
            self._embed(hwnd)
            return
        # Never give up while scrcpy runs: with many phones the window can take a minute to appear.
        # The window is created off-screen, so nothing shows outside the app while we wait.
        if attempt == 300:
            self.app.log(f"[{self.serial}] still connecting (phone slow to answer) — waiting", "warn")
        self.app.root.after(100 if attempt < 150 else 400, lambda: self._try_embed(attempt + 1, gen))

    def _embed(self, hwnd):
        gen = self._gen
        try:
            view = self.view
            view.update_idletasks()
            WIN32.embed(hwnd, view.winfo_id())
            self.hwnd = hwnd
            WIN32.enable(hwnd, self.input_enabled or self.pop is not None)
            self.retries = 0
            self._set_phase("live")
            self._resize()
            self.app.log(f"[{self.serial}] embedded", "ok")
        except Exception as e:
            self.app.log(f"[{self.serial}] embed failed: {e}", "err")
            try:  # it was opened off-screen: bring it where it can be seen
                WIN32.move(hwnd, px(60), px(60), px(360), px(760))
            except Exception:
                pass
            self._set_phase("window")
        self._link_up(gen)

    def _link_up(self, gen):
        """Auto screen-off: scrcpy's --turn-screen-off can be undone while the device is still
        connecting (wake-up, unlock), so press the screen-off shortcut again once the window is up."""
        if not self.opts.get("turn_screen_off"):
            return

        def apply():
            if self._closed or gen != self._gen or not self._scrcpy_window():
                return
            self.set_screen(True, auto=True)
        self.app.root.after(1500, apply)

    @property
    def view(self):
        """Where the mirror window lives right now: the grid tile, or the pop-out window."""
        return self.pop_holder if self.pop is not None else self.holder

    def _resize(self, _e=None):
        if not self.hwnd:
            return
        try:
            view = self.view
            bw = int(view["highlightthickness"])
            w = view.winfo_width() - 2 * bw
            h = view.winfo_height() - 2 * bw
            if w < 40 or h < 40:  # not laid out yet: a 1x1 window can crash scrcpy (divide by zero)
                return
            if self.pop is not None:   # free-size window: keep the phone's ratio, centred (letterbox)
                fw, fh = w, int(w * self.aspect())
                if fh > h:
                    fh, fw = h, int(h / self.aspect())
                WIN32.move(self.hwnd, bw + (w - fw) // 2, bw + (h - fh) // 2, fw, fh)
            else:
                WIN32.move(self.hwnd, bw, bw, w, h)
        except Exception:
            pass

    # ----- pop-out: the phone in its own movable / resizable window -----
    def _dock_or_focus(self):
        if self.pop is not None:
            self.dock()
        else:
            self.app.toggle_focus(self)

    def toggle_popout(self):
        if self.pop is not None:
            self.dock()
        else:
            self.popout()

    def popout(self, x=None, y=None):
        if self._closed or self.pop is not None:
            return
        root = self.app.root
        top = tk.Toplevel(root)
        top.configure(bg=C.BG)
        top.title(f"{self._num_text(self.app.units.get(self.serial, {}).get('num'))}  "
                  f"{self.unit_name or self.meta.get('model') or self.serial}  —  SCIENTIST")
        try:
            top.iconphoto(False, self.app._icon)
        except Exception:
            pass
        bar = tk.Frame(top, bg=C.SURFACE)
        bar.pack(side=tk.BOTTOM, fill=tk.X)
        inner = tk.Frame(bar, bg=C.SURFACE)
        inner.pack(fill=tk.X, padx=px(8), pady=px(6))
        RButton(inner, "DOCK", icon="dock", height=30, radius=9, command=self.dock,
                tooltip="Put the phone back in the mirror grid").pack(side=tk.RIGHT)
        for icon, tip, cmd in (
            ("camera", "Screenshot → ./screenshots", self.screenshot),
            ("recents", "Recent apps", lambda: self.key(187, "RECENTS")),
            ("home", "Home", lambda: self.key(3, "HOME")),
            ("back", "Back", lambda: self.key(4, "BACK")),
        ):
            RButton(inner, icon=icon, height=30, radius=9, command=cmd, tooltip=tip).pack(
                side=tk.RIGHT, padx=(0, px(4)))
        RButton(inner, icon="power", height=30, radius=9, command=self.toggle_screen,
                tooltip="Screen off / on").pack(side=tk.LEFT)
        holder = tk.Frame(top, bg="#000000", highlightthickness=0)
        holder.pack(fill=tk.BOTH, expand=True)
        self.pop, self.pop_holder = top, holder
        # size: a comfortable phone height, never taller than the screen
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        tbh = px(48)
        hh = min(int(sh * 0.72) - tbh, px(820))      # leaves room for the title bar + taskbar
        ww = max(px(260), int(hh / self.aspect()))
        if x is None or y is None:
            x = root.winfo_rootx() + (root.winfo_width() - ww) // 2
            y = root.winfo_rooty() + px(60)
        x = min(max(0, int(x)), max(0, sw - ww - px(8)))
        y = min(max(0, int(y)), max(0, sh - (hh + tbh) - px(90)))
        top.geometry(f"{ww}x{hh + tbh}+{x}+{y}")
        top.minsize(px(180), px(260))
        top.protocol("WM_DELETE_WINDOW", self.dock)
        holder.bind("<Configure>", self._resize)
        top.bind("<Escape>", lambda e: self.dock())
        style_titlebar(top)
        self.ph.config(text="POPPED OUT\n\ndouble-click to dock", fg=C.PURPLE_HI)
        top.update_idletasks()
        if self.hwnd and WIN32.ok:
            try:
                WIN32.embed(self.hwnd, holder.winfo_id())
                WIN32.enable(self.hwnd, True)
            except Exception:
                pass
        self._resize()
        self.app.on_popout(self, True)
        self.app.log(f"[{self.serial}] popped out — drag the window anywhere", "sys")

    def dock(self):
        top, self.pop, self.pop_holder = self.pop, None, None
        if top is None:
            return
        if self.hwnd and WIN32.ok and not self._closed:
            try:
                self.holder.update_idletasks()
                WIN32.embed(self.hwnd, self.holder.winfo_id())
                WIN32.enable(self.hwnd, self.input_enabled)
            except Exception:
                pass
        try:
            top.destroy()
        except Exception:
            pass
        if self._closed:
            return
        self.ph.config(text="")
        self._resize()
        self.app.on_popout(self, False)

    def _drag_start(self, e):
        self._drag = (e.x_root, e.y_root, False)

    def _drag_move(self, e):
        if not self._drag:
            return
        x0, y0, moving = self._drag
        if not moving:
            if not self.focused or abs(e.x_root - x0) + abs(e.y_root - y0) < px(24):
                return
            self.popout(e.x_root - px(120), e.y_root - px(18))
            self._drag = (x0, y0, True)
        if self.pop is not None:
            self.pop.geometry(f"+{e.x_root - px(120)}+{e.y_root - px(18)}")

    def _drag_end(self, _e):
        self._drag = None

    def stop(self, sync=False):
        self._gen += 1
        proc, self.proc, self.hwnd = self.proc, None, None
        if proc and proc.poll() is None:
            if sync:
                terminate(proc)
            else:
                bg_thread(terminate, proc)
        if self.tweaks:
            if sync:
                self.restore_tweaks()
            else:
                bg_thread(self.restore_tweaks)

    def restart(self):
        if self._closed:
            return
        self.app.log(f"[{self.serial}] reconnecting…", "sys")
        self.stop()
        self._phase = "boot"
        self.chip.set("BOOT", C.AMBER)
        self.app.root.after(500, lambda: self.app.queue_start(self))

    def close(self, sync=False):
        if self._closed:
            return
        self._closed = True
        self.stop(sync=sync)
        self.app.release_port_slot(self.port_slot)
        self.port_slot = None
        if self.pop is not None:
            try:
                self.pop.destroy()
            except Exception:
                pass
            self.pop = self.pop_holder = None
        try:
            self.app._on_panel_close(self)
        except Exception:
            pass
        try:
            self.card.destroy()
        except Exception:
            pass

    # ----- device actions -----
    def _update_power(self):
        if self._closed:
            return
        if self.screen_off:
            self.power_btn.set(text="SCREEN ON", variant="primary")
        else:
            self.power_btn.set(text="SCREEN OFF", variant="ghost")

    def sync_screen(self, off):
        if not self._closed:
            self.screen_off = off
            self._update_power()

    def _scrcpy_window(self):
        if not (WIN32.ok and self.proc and self.proc.poll() is None):
            return None
        return self.hwnd or WIN32.find(self.window_title)

    def _send_shortcut(self, off, done):
        """Press scrcpy's own shortcut: Alt+O = screen off (keeps mirroring), Alt+Shift+O = screen on.
        Works on every Android version. Runs through the app's exclusive queue because it moves focus."""
        def job(finish):
            hwnd = self._scrcpy_window() if not self._closed else None
            if not hwnd:
                finish()
                done(False, "mirror window not available")
                return
            was_enabled = WIN32.is_enabled(hwnd)
            if not was_enabled:
                WIN32.enable(hwnd, True)
            try:
                detach = WIN32.focus_child(hwnd)
            except Exception as e:
                finish()
                done(False, f"focus failed: {e}")
                return

            def press():
                try:
                    keys = [VK_LALT, VK_O] if off else [VK_LALT, VK_LSHIFT, VK_O]
                    sent = WIN32.send_keys(keys)
                    err = None if sent else "SendInput was blocked"
                except Exception as e:
                    err = str(e)
                self.app.root.after(150, lambda: release(err))

            def release(err):
                try:
                    detach()
                finally:
                    if not was_enabled and not self.input_enabled:
                        WIN32.enable(hwnd, False)
                    finish()
                done(err is None, err or "")

            self.app.root.after(60, press)

        self.app.run_exclusive(job)

    def set_screen(self, off, auto=False):
        if self._busy or self._closed:
            return
        self._busy = True
        self.power_btn.set_disabled(True)
        state = "OFF" if off else "ON"
        sdk = self.app.sdk(self.serial)
        res = {}

        def finish():
            self._busy = False
            if self._closed:
                return
            self.power_btn.set_disabled(False)
            used = [name for name in ("scrcpy", "adb") if res.get(name, (False,))[0]]
            if used:
                self.screen_off = off
                how = " + ".join({"scrcpy": "scrcpy Alt+O" if off else "scrcpy Alt+Shift+O",
                                  "adb": "cmd display"}[u] for u in used)
                self.app.log(f"[{self.serial}] display {state}{' (auto)' if auto else ''} via {how}", "ok")
            else:
                why = "; ".join(f"{k}: {v[1]}" for k, v in res.items() if v[1]) or "no method available"
                self.app.log(f"[{self.serial}] display {state} failed — {why}", "err")
                self.app.toast.show(f"Screen {state} failed — see log", C.RED)
            self._update_power()

        def adb_step():
            # cmd display only exists on Android 15+; on older devices scrcpy's shortcut does the job alone
            if res.get("scrcpy", (False,))[0] and sdk is not None and sdk < ADB_DISPLAY_MIN_SDK:
                finish()
                return

            def work():
                res["adb"] = adb_display(self.serial, on=not off, sdk=sdk)
                self.app.ui(finish)
            bg_thread(work)

        def shortcut_done(ok, msg):
            res["scrcpy"] = (ok, msg)
            adb_step()

        if self._scrcpy_window():
            self._send_shortcut(off, shortcut_done)
        else:
            adb_step()

    def toggle_screen(self):
        self.set_screen(not self.screen_off)

    def force_screen(self, off):
        if off != self.screen_off:
            self.set_screen(off)

    def key(self, code, name):
        def work():
            try:
                ok = run_adb(self.serial, "shell", "input", "keyevent", str(code), timeout=8).returncode == 0
            except Exception:
                ok = False
            self.app.log(f"[{self.serial}] key {name}" + ("" if ok else " failed"), "info" if ok else "err")
        bg_thread(work)

    def screenshot(self):
        def work():
            try:
                r = run_adb(self.serial, "exec-out", "screencap", "-p", timeout=20, binary=True)
                if r.returncode == 0 and r.stdout.startswith(b"\x89PNG"):
                    SHOT_DIR.mkdir(exist_ok=True)
                    safe = re.sub(r"[^\w.-]", "_", self.serial)
                    path = SHOT_DIR / f"{safe}_{datetime.now():%Y%m%d_%H%M%S}.png"
                    path.write_bytes(r.stdout)
                    self.app.log(f"[{self.serial}] screenshot saved → {path}", "ok")
                    self.app.ui(self.app.toast.show, "Screenshot saved", C.GREEN)
                else:
                    self.app.log(f"[{self.serial}] screenshot failed", "err")
            except Exception as e:
                self.app.log(f"[{self.serial}] screenshot error: {e}", "err")
        bg_thread(work)


# ════════════════════════════════════════════════════════════════════
#  App
# ════════════════════════════════════════════════════════════════════
class AdbScrcpyApp:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.panels = {}
        self.device_info = {}
        self.known = {}
        self._fetched = set()
        self._refreshing = False
        self._last_full = 0.0
        self._hist_idx = None
        self._excl = []
        self._excl_busy = False
        self._tile_job = None
        self.focused = None              # serial of the enlarged tile
        self._last_click = None
        self._hot_warned = set()
        self.tethered = set()
        self._watching = False
        self.settings_win = None
        # FB Daily engine state
        self.fb_threads = {}             # serial -> worker thread
        self.fb_totals = {}
        self.fb_lock = threading.Lock()
        self._fb_stopping = False

        s = load_settings()
        self.vars = {k: tk.BooleanVar(value=bool(s[k])) for k in BOOL_KEYS}
        self.vars.update({k: tk.StringVar(value=str(s[k])) for k in STR_KEYS})
        self.vars.update({k: tk.IntVar(value=int(s[k])) for k in INT_KEYS})
        self.tile_preset = tk.StringVar(value="")
        self._wifi_saved = s.get("wifi_addr", "")
        self._dns_saved = s.get("dns", "8.8.8.8")
        self.shell_history = list(s.get("shell_history", []))[-30:]
        self.units = {k: v for k, v in (s.get("devices") or {}).items() if isinstance(v, dict) and "num" in v}
        self.tweaks_dirty = {k: dict(v) for k, v in (s.get("tweaks") or {}).items() if isinstance(v, dict)}
        # time clock
        self.tc_cfg = tc_clean_cfg(s.get("timeclock"))
        self.tc = TimeClock(TIMECLOCK_DIR)
        self._tc_shown = {}
        self.admin_win = None
        self.tether_mgr = Tether(self.log)
        self.tether_mgr.dns = self._dns_saved or "8.8.8.8"
        for v in self.vars.values():
            v.trace_add("write", lambda *a: self._save())
        self.vars["tile_w"].trace_add("write", lambda *a: self._apply_tile())
        self.vars["log_visible"].trace_add("write", lambda *a: self._apply_log_visibility())
        self.vars["clock_24h"].trace_add("write", lambda *a: self.header.set_time(datetime.now(),
                                                                                  self.vars["clock_24h"].get()))

        root.title("SCIENTIST // ADB Multi-Mirror")
        root.configure(bg=C.BG)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self.small_screen = sh < px(900)
        root.geometry(f"{min(px(1500), sw - 40)}x{min(px(950), sh - 80)}+20+10")
        root.minsize(min(px(1050), sw - 40), min(px(620), sh - 80))
        if sw < px(1500) or sh < px(950):
            try:
                root.state("zoomed")
            except tk.TclError:
                pass
        self._icon = make_icon(root)
        root.iconphoto(True, self._icon)

        self.toast = Toast(root)
        self._build()
        self._bind_keys()
        root.bind_all("<MouseWheel>", self._route_wheel)
        root.bind_all("<Control-MouseWheel>", lambda e: (self._zoom_wheel(e.delta), "break")[1])
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        style_titlebar(root)
        if WIN32.ok:  # Ctrl+wheel also over the embedded mirrors (they belong to scrcpy's process)
            WIN32.start_ctrl_wheel_hook(WIN32.parent(root.winfo_id()), lambda d: self.ui(self._zoom_wheel, d),
                                        on_click=lambda x, y: self.ui(self._on_screen_click, x, y))
        self._sync_tile_preset()

        self._pump()
        self._tick()
        root.after(3000, self._auto_tick)
        root.after(6000, self._tether_watch)
        root.after(1500, self._fb_tick)
        self.log("SCIENTIST online — scanning for devices…", "sys")
        if not WIN32.ok:
            self.log("Native embedding unavailable — mirrors will open in their own windows.", "warn")
        self.refresh_devices()
        bg_thread(self._probe_tools)

    # ───────────────────────── UI build ─────────────────────────
    def _build(self):
        self.header = Header(self.root, on_settings=self.open_settings, on_worker=self._tc_worker_menu)
        self.header.pack(fill=tk.X)
        self._build_status().pack(side=tk.BOTTOM, fill=tk.X)
        self._build_log()
        main = tk.Frame(self.root, bg=C.BG)
        main.pack(fill=tk.BOTH, expand=True, padx=px(16), pady=(px(14), px(12)))
        self.main = main
        self._build_sidebar(main)
        self._build_grid(main)
        if self.vars["sidebar_hidden"].get():
            self.side.pack_forget()
        self._apply_log_visibility()

    def _label(self, parent, text, top=12):
        tk.Label(parent, text=text, font=F.tiny, fg=C.MUTED, bg=parent.cget("bg"),
                 anchor="w").pack(fill=tk.X, pady=(px(top), px(6)))

    def _pair(self, parent, a, b):
        row = tk.Frame(parent, bg=parent.cget("bg"))
        row.pack(fill=tk.X)
        for i, (text, icon, variant, cmd, tip) in enumerate((a, b)):
            RButton(row, text, icon=icon, variant=variant, height=34, width=60, command=cmd, tooltip=tip).pack(
                side=tk.LEFT, expand=True, fill=tk.X, padx=(0, px(3)) if i == 0 else (px(3), 0))
        return row

    def _build_sidebar(self, parent):
        self.side = ScrollFrame(parent, bg=C.BG)
        self.side.canvas.config(width=px(352))
        self.side.pack(side=tk.LEFT, fill=tk.Y, padx=(0, px(14)))
        inner = self.side.inner

        def card(title):
            c = Card(inner, title=title)
            c.pack(fill=tk.X, pady=(0, px(12)), padx=(0, px(2)))
            return c, c.body

        # Time clock (workers)
        c, b = card("Time clock")
        self.tc_card = c
        self.tc_chip = Chip(c.header_right, "OFF", C.MUTED)
        self.tc_chip.pack(side=tk.RIGHT)
        self.tc_worker_btn = RButton(b, "", icon="person", height=38, radius=12, command=self._tc_worker_menu,
                                     tooltip="Worker on duty — click to switch")
        self.tc_worker_btn.pack(fill=tk.X)
        self.tc_shift_lbl = tk.Label(b, text="", font=F.mono_s, fg=C.MUTED, bg=C.SURFACE, anchor="w")
        self.tc_shift_lbl.pack(fill=tk.X, pady=(px(6), px(8)))
        self.tc_btns = {}
        for pair in ((("in", "TIME IN", "clock", "primary"), ("out", "TIME OUT", "power", "danger")),
                     (("break", "BREAK", "coffee", "ghost"), ("lunch", "LUNCH", "food", "ghost"))):
            row = tk.Frame(b, bg=C.SURFACE)
            row.pack(fill=tk.X, pady=(0, px(6)))
            for i, (act, text, icon, variant) in enumerate(pair):
                btn = RButton(row, text, icon=icon, variant=variant, height=40, width=60, radius=12,
                              command=lambda a=act: self.tc_punch(a))
                btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, px(3)) if i == 0 else (px(3), 0))
                self.tc_btns[act] = btn
        self.tc_info = tk.Label(b, text="", font=F.mono, fg=C.SOFT, bg=C.SURFACE, anchor="w", justify="left")
        self.tc_info.pack(fill=tk.X, pady=(px(4), 0))
        self.tc_alert = tk.Label(b, text="", font=F.h, fg=C.RED, bg=C.SURFACE, anchor="w", justify="left")
        self.tc_alert.pack(fill=tk.X, pady=(px(4), 0))
        row = tk.Frame(b, bg=C.SURFACE)
        row.pack(fill=tk.X, pady=(px(8), 0))
        RButton(row, icon="folder", height=38, radius=12, command=self.tc_open_folder,
                tooltip="Open proofs + timesheet.csv folder").pack(side=tk.RIGHT, padx=(px(6), 0))
        RButton(row, "SAVE PROOF", icon="camera", variant="primary", height=38, radius=12,
                command=self.tc_save_proof,
                tooltip="Screenshot of this window with the worker, time and punch stamped on it").pack(
            side=tk.LEFT, fill=tk.X, expand=True)
        self.tc_proof_lbl = tk.Label(b, text="", font=F.mono_s, fg=C.FAINT, bg=C.SURFACE, anchor="w",
                                     justify="left")
        self.tc_proof_lbl.pack(fill=tk.X, pady=(px(6), 0))

        # Devices
        c, b = card("Devices")
        RButton(c.header_right, icon="refresh", height=26, radius=8, command=self.refresh_devices,
                tooltip="Rescan devices (F5)").pack(side=tk.RIGHT)
        self.sel_chip = Chip(c.header_right, "0 SELECTED", C.PURPLE, dot=False)
        self.sel_chip.pack(side=tk.RIGHT, padx=(0, px(6)))
        self.device_list = DeviceList(b, on_change=self._on_selection, on_double=lambda s: self.start_mirror([s]),
                                      on_menu=self._device_menu)
        self.device_list.pack(fill=tk.X)
        self._pair(b, ("ALL", "select", "ghost", self.device_list.select_all, "Select every device (Ctrl+A)"),
                   ("NONE", "clear", "ghost", self.device_list.clear, "Clear selection")).pack_configure(pady=(px(4), 0))
        Toggle(b, "Live hot-plug detection", self.vars["auto_refresh"], sub="rescans every 3 s").pack(
            fill=tk.X, pady=(px(12), 0))
        tk.Label(b, text="click select · 2×click mirror · right-click menu", font=F.mono_s, fg=C.FAINT,
                 bg=C.SURFACE, anchor="w").pack(fill=tk.X, pady=(px(8), 0))

        # Mirror
        c, b = card("Mirror")
        RButton(b, "START MIRROR", icon="play", variant="primary", height=42, radius=12,
                command=self.start_mirror_selected, tooltip="Mirror selected devices (Ctrl+M)").pack(fill=tk.X)
        RButton(b, "STOP ALL MIRRORS", icon="stop", variant="danger", height=36,
                command=self.stop_all_mirrors, tooltip="Ctrl+Shift+S").pack(fill=tk.X, pady=(px(6), 0))
        self._label(b, "SCRCPY OPTIONS", top=14)
        for key, text, sub in (
            ("stay_awake", "Keep devices awake", "--stay-awake"),
            ("turn_screen_off", "Screen off on connect", "--turn-screen-off"),
            ("show_touches", "Show touches", "finger taps on the phone (--show-touches)"),
            ("show_clicks", "Show mirror clicks", "trace of clicks made from this PC"),
            ("auto_mirror", "Auto-mirror new devices", "plug in = mirror starts"),
            ("auto_reconnect", "Auto-reconnect lost links", "retries when a phone drops"),
        ):
            Toggle(b, text, self.vars[key], sub=sub).pack(fill=tk.X, pady=(0, px(8)))
        self._label(b, "AUDIO", top=4)
        Segmented(b, [("pc", "ON PC"), ("phone", "ON PHONE"), ("mute", "MUTE")], self.vars["audio_mode"]).pack(fill=tk.X)
        tk.Label(b, text="MUTE = silent, phone volume 0 while mirrored", font=F.mono_s,
                 fg=C.FAINT, bg=C.SURFACE, anchor="w").pack(fill=tk.X, pady=(px(4), 0))
        self._label(b, "RESOLUTION  ·  LONG SIDE")
        Segmented(b, [("0", "NATIVE"), ("1920", "1920"), ("1280", "1280"), ("1024", "1024")],
                  self.vars["max_size"]).pack(fill=tk.X)
        self._label(b, "FRAME RATE")
        Segmented(b, [("0", "AUTO"), ("60", "60"), ("30", "30"), ("15", "15")], self.vars["max_fps"]).pack(fill=tk.X)
        RButton(b, "APPLY TO OPEN MIRRORS", icon="refresh", height=32, command=self.apply_options,
                tooltip="Restart open mirrors with the options above").pack(fill=tk.X, pady=(px(10), 0))

        # Screen control
        c, b = card("Screen control")
        self._label(b, "ALL ACTIVE MIRRORS", top=0)
        self._pair(b, ("ALL OFF", "power", "ghost", self.screen_off_all, "Turn off every mirrored display"),
                   ("ALL ON", "power", "ghost", self.screen_on_all, "Turn on every mirrored display"))
        self._label(b, "SELECTED DEVICES  ·  DIRECT ADB")
        self._pair(b, ("ADB OFF", "power", "ghost", lambda: self.adb_screen_selected(True), "cmd display power-off 0"),
                   ("ADB ON", "power", "ghost", lambda: self.adb_screen_selected(False), "cmd display power-on 0"))
        

        # Farm actions
        c, b = card("Farm actions")
        self._label(b, "SELECTED DEVICES", top=0)
        row = tk.Frame(b, bg=C.SURFACE)
        row.pack(fill=tk.X)
        for i, (icon, tip, cmd) in enumerate((
            ("wake", "Wake screen", lambda: self.bulk_key(224, "WAKE")),
            ("lock", "Lock / sleep", lambda: self.bulk_key(223, "SLEEP")),
            ("home", "Home", lambda: self.bulk_key(3, "HOME")),
            ("back", "Back", lambda: self.bulk_key(4, "BACK")),
            ("bright", "Brightness to minimum (less heat & battery)", lambda: self.bulk_brightness(False)),
            ("picture", "Brightness back to auto", lambda: self.bulk_brightness(True)),
        )):
            RButton(row, icon=icon, height=34, width=40, command=cmd, tooltip=tip).pack(
                side=tk.LEFT, expand=True, fill=tk.X, padx=(0 if i == 0 else px(4), 0))
        self._label(b, "APPS & FILES")
        self._pair(b, ("INSTALL APK", "package", "ghost", self.bulk_install, "adb install -r -g on every selected phone"),
                   ("PUSH FILE", "upload", "ghost", self.bulk_push, "copies to /sdcard/Download"))
        row = tk.Frame(b, bg=C.SURFACE)
        row.pack(fill=tk.X, pady=(px(6), 0))
        self.launch_entry = REntry(row, "com.android.chrome  or  https://...")
        RButton(row, icon="stop", height=34, command=lambda: self.bulk_launch(stop=True),
                tooltip="Force-stop the package").pack(side=tk.RIGHT, padx=(px(4), 0))
        RButton(row, "GO", icon="play", variant="primary", height=34, command=self.bulk_launch,
                tooltip="Launch an app by package, or open a URL").pack(side=tk.RIGHT, padx=(px(6), 0))
        self.launch_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.launch_entry.entry.bind("<Return>", lambda e: self.bulk_launch())
        RButton(b, "REBOOT SELECTED", icon="refresh", variant="danger", height=32,
                command=self.bulk_reboot).pack(fill=tk.X, pady=(px(10), 0))

        # FB Daily (fbdaily/fbdaily.py engine)
        c, b = card("FB Daily")
        self.fb_chip = Chip(c.header_right, "IDLE", C.MUTED)
        self.fb_chip.pack(side=tk.RIGHT)
        tk.Label(b, text="human-like Facebook Lite activity on USB phones", font=F.small, fg=C.MUTED,
                 bg=C.SURFACE, anchor="w").pack(fill=tk.X, pady=(0, px(8)))
        row = tk.Frame(b, bg=C.SURFACE)
        row.pack(fill=tk.X)
        self.fb_start_btn = RButton(row, "START", icon="play", variant="primary", height=36, width=60,
                                    command=self.bulk_fbdaily_start,
                                    tooltip="Run FB Daily on the selected USB phones (all USB phones if none selected)")
        self.fb_start_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, px(3)))
        self.fb_stop_btn = RButton(row, "STOP", icon="stop", variant="danger", height=36, width=60,
                                   command=self.bulk_fbdaily_stop,
                                   tooltip="Each phone finishes its step, then FB Lite is closed")
        self.fb_stop_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(px(3), 0))
        self._label(b, "ACTIVITIES", top=12)
        for key, text, sub in (
            ("fb_chat", "Reply to messages", "AI replies in Messenger"),
            ("fb_friends", "Add / confirm friends", "a few per session"),
            ("fb_engage", "React / comment / share", "on timeline posts"),
            ("fb_pacing", "Slow & safe pacing", "warm-up for new accounts, 1–2 rest days a week"),
        ):
            Toggle(b, text, self.vars[key], sub=sub).pack(fill=tk.X, pady=(0, px(8)))
        self._label(b, "INTENSITY", top=4)
        Segmented(b, [("Gentle", "GENTLE"), ("Normal", "NORMAL"), ("Active", "ACTIVE")],
                  self.vars["fb_intensity"]).pack(fill=tk.X)
        self.fb_stats = tk.Label(b, text="", font=F.mono_s, fg=C.SOFT, bg=C.SURFACE, anchor="w",
                                 justify="left")
        self.fb_stats.pack(fill=tk.X, pady=(px(10), 0))
        tk.Label(b, text="applies on next START · fbdaily/config.json", font=F.mono_s,
                 fg=C.FAINT, bg=C.SURFACE, anchor="w").pack(fill=tk.X, pady=(px(6), 0))
        self._fb_refresh_ui()


        # Unit IDs / numbered wallpapers
        c, b = card("Unit IDs")
        RButton(b, "PUSH NUMBERED WALLPAPER", icon="picture", variant="primary", height=40, radius=12,
                command=self.push_wallpapers, tooltip="Sets a big unit number as wallpaper on every selected phone"
                ).pack(fill=tk.X)
        self._pair(b, ("PREVIEW", "picture", "ghost", self.preview_wallpaper, "Render one and open it on this PC"),
                   ("RENUMBER", "edit", "ghost", self.renumber_units, "Compact unit numbers to 1...N")
                   ).pack_configure(pady=(px(6), 0))
        Toggle(b, "Lock screen too", self.vars["wall_lock"], sub="home + lock screen").pack(
            fill=tk.X, pady=(px(12), 0))
        tk.Label(b, text="right-click a device to set its number or name", font=F.mono_s, fg=C.FAINT,
                 bg=C.SURFACE, anchor="w").pack(fill=tk.X, pady=(px(8), 0))

        # Reverse tethering
        c, b = card("Reverse tether")
        self.tether_chip = Chip(c.header_right, "...", C.MUTED)
        self.tether_chip.pack(side=tk.RIGHT)
        tk.Label(b, text="phones use this PC's internet over USB", font=F.small, fg=C.MUTED,
                 bg=C.SURFACE, anchor="w").pack(fill=tk.X, pady=(0, px(8)))
        self._pair(b, ("TETHER ON", "globe", "primary", lambda: self.tether(True),
                       "Start reverse tethering on selected phones"),
                   ("TETHER OFF", "stop", "ghost", lambda: self.tether(False),
                    "Stop reverse tethering on selected phones"))
        RButton(b, "TEST INTERNET ON SELECTED", icon="globe", height=32, command=self.test_internet,
                tooltip="Real HTTP request from each phone").pack(fill=tk.X, pady=(px(6), 0))
        row = tk.Frame(b, bg=C.SURFACE)
        row.pack(fill=tk.X, pady=(px(8), 0))
        tk.Label(row, text="DNS", font=F.tiny, fg=C.MUTED, bg=C.SURFACE).pack(side=tk.LEFT, padx=(0, px(8)))
        self.dns_entry = REntry(row, "8.8.8.8")
        self.dns_entry.set(self._dns_saved)
        self.dns_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        Toggle(b, "Auto-tether new devices", self.vars["auto_tether"], sub="internet on plug-in").pack(
            fill=tk.X, pady=(px(10), 0))
        tk.Label(b, text="built-in relay + client · no taps needed on the phone", font=F.mono_s, fg=C.FAINT,
                 bg=C.SURFACE, anchor="w").pack(fill=tk.X, pady=(px(8), 0))
        self._update_tether_ui()

        # Toolkit
        c, b = card("ADB toolkit")
        self._label(b, "WIRELESS", top=0)
        row = tk.Frame(b, bg=C.SURFACE)
        row.pack(fill=tk.X)
        self.wifi_entry = REntry(row, "192.168.1.20:5555")
        self.wifi_entry.set(self._wifi_saved)
        RButton(row, "CONNECT", icon="link", variant="primary", height=34,
                command=self.wifi_connect).pack(side=tk.RIGHT, padx=(px(6), 0))
        self.wifi_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.wifi_entry.entry.bind("<Return>", lambda e: self.wifi_connect())
        RButton(b, "ENABLE WI-FI ADB ON SELECTED", icon="wifi", height=32, command=self.enable_tcpip,
                tooltip="adb tcpip 5555 + fills in the device IP").pack(fill=tk.X, pady=(px(6), 0))
        self._label(b, "SHELL  ·  SELECTED DEVICES")
        row = tk.Frame(b, bg=C.SURFACE)
        row.pack(fill=tk.X)
        self.shell_entry = REntry(row, "input keyevent 26")
        RButton(row, "RUN", icon="terminal", variant="primary", height=34,
                command=self.run_shell, tooltip="Enter to run · ↑/↓ history").pack(side=tk.RIGHT, padx=(px(6), 0))
        self.shell_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        e = self.shell_entry.entry
        e.bind("<Return>", self.run_shell)
        e.bind("<Up>", lambda ev: self._hist(-1))
        e.bind("<Down>", lambda ev: self._hist(1))
        RButton(b, "OPEN SCREENSHOTS FOLDER", icon="folder", height=32,
                command=self.open_shots).pack(fill=tk.X, pady=(px(12), 0))

    def _build_grid(self, parent):
        right = tk.Frame(parent, bg=C.BG)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.grid_right = right
        tb = tk.Frame(right, bg=C.BG)
        tb.pack(fill=tk.X, pady=(0, px(10)))
        RButton(tb, icon="menu", height=30, command=self.toggle_sidebar,
                tooltip="Hide / show the side panel (Ctrl+B)").pack(side=tk.LEFT, padx=(0, px(10)))
        tk.Frame(tb, bg=C.PURPLE, width=px(3), height=px(18)).pack(side=tk.LEFT, padx=(0, px(8)))
        tk.Label(tb, text="MIRROR GRID", font=F.h2, fg=C.TEXT, bg=C.BG).pack(side=tk.LEFT)
        self.grid_chip = Chip(tb, "0 LINKS", C.MUTED)
        self.grid_chip.pack(side=tk.LEFT, padx=px(10))
        RButton(tb, "STOP ALL", icon="stop", variant="danger", height=30,
                command=self.stop_all_mirrors, tooltip="Ctrl+Shift+S").pack(side=tk.RIGHT)
        Segmented(tb, [("S", "S"), ("M", "M"), ("L", "L")], self.tile_preset, height=30, width=96,
                  command=lambda: self._manual_tile(TILE_PRESETS.get(self.tile_preset.get(), 240))).pack(
            side=tk.RIGHT, padx=(0, px(12)))
        self.fit_btn = RButton(tb, "FIT", icon="fit", height=30, command=self.fit_tiles,
                               tooltip="Auto-fit: tiles resize so every phone stays visible (Ctrl+0)")
        self.fit_btn.pack(side=tk.RIGHT, padx=(0, px(8)))
        self.vars["auto_fit"].trace_add("write", lambda *a: self._sync_fit_btn())
        RButton(tb, icon="plus", height=30, command=lambda: self._zoom_wheel(120),
                tooltip="Bigger tiles (Ctrl+scroll up / Ctrl+=)").pack(side=tk.RIGHT, padx=(0, px(8)))
        self.tile_chip = Chip(tb, f"{self.vars['tile_w'].get()} PX", C.PURPLE_HI, dot=False, height=30)
        self.tile_chip.pack(side=tk.RIGHT, padx=(0, px(4)))
        RButton(tb, icon="minus", height=30, command=lambda: self._zoom_wheel(-120),
                tooltip="Smaller tiles (Ctrl+scroll down / Ctrl+-)").pack(side=tk.RIGHT, padx=(0, px(4)))
        tk.Label(tb, text="TILE", font=F.tiny, fg=C.MUTED, bg=C.BG).pack(side=tk.RIGHT, padx=(0, px(6)))
        self.grid_scroll = ScrollFrame(right, bg=C.BG)
        self.grid_scroll.pack(fill=tk.BOTH, expand=True)
        self.grid_scroll.on_resize = self._relayout
        # containers for focus mode (created before any tile so tiles stack above them)
        self.focus_box = tk.Frame(self.grid_scroll.inner, bg=C.BG)
        self.side_box = tk.Frame(self.grid_scroll.inner, bg=C.BG)
        self.empty = EmptyState(self.grid_scroll.inner)

    def _build_log(self):
        self.log_card = Card(self.root, title="System log", pad=12)
        self.log_card.pack(side=tk.BOTTOM, fill=tk.X, padx=px(16), pady=(0, px(12)))
        hr = self.log_card.header_right
        self.log_toggle_btn = RButton(hr, icon="chev_down", height=26, radius=8,
                                      command=self._toggle_log, tooltip="Collapse / expand")
        self.log_toggle_btn.pack(side=tk.RIGHT)
        RButton(hr, icon="trash", height=26, radius=8, command=self.clear_log,
                tooltip="Clear (Ctrl+L)").pack(side=tk.RIGHT, padx=px(4))
        RButton(hr, icon="copy", height=26, radius=8, command=self.copy_log, tooltip="Copy log").pack(side=tk.RIGHT)
        self.log_holder = tk.Frame(self.log_card.body, bg=C.BG)
        self.log_holder.pack(fill=tk.X)
        self.log_text = tk.Text(self.log_holder, height=4 if self.small_screen else 7, bg=C.BG, fg=C.SOFT, font=F.mono, bd=0, relief="flat",
                                highlightthickness=0, wrap="word", padx=px(10), pady=px(8),
                                insertbackground=C.PURPLE, selectbackground=C.PURPLE_DIM, state="disabled",
                                cursor="arrow")
        sb = CyberScrollbar(self.log_holder, self.log_text.yview, width=6)
        self.log_text.config(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, px(4)), pady=px(4))
        self.log_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        t = self.log_text
        t.tag_config("time", foreground=C.FAINT)
        t.tag_config("dev", foreground=C.PURPLE_HI)
        t.tag_config("info", foreground=C.SOFT)
        t.tag_config("sys", foreground=C.PURPLE)
        t.tag_config("ok", foreground=C.GREEN)
        t.tag_config("warn", foreground=C.AMBER)
        t.tag_config("err", foreground=C.RED)

    def _build_status(self):
        sb = tk.Frame(self.root, bg=C.BG2)
        tk.Frame(sb, bg=C.BORDER, height=1).pack(fill=tk.X, side=tk.TOP)
        row = tk.Frame(sb, bg=C.BG2)
        row.pack(fill=tk.X, padx=px(16), pady=px(5))
        self.adb_chip = Chip(row, "ADB …", C.MUTED)
        self.adb_chip.pack(side=tk.LEFT)
        self.scrcpy_chip = Chip(row, "SCRCPY …", C.MUTED)
        self.scrcpy_chip.pack(side=tk.LEFT, padx=px(6))
        Chip(row, "EMBED · NATIVE" if WIN32.ok else "EMBED · OFF", C.GREEN if WIN32.ok else C.AMBER).pack(side=tk.LEFT)
        tk.Label(row, text="2×click tile = enlarge  ·  Esc = grid  ·  Ctrl+B panel  ·  Ctrl+scroll size  ·  Ctrl+0 fit",
                 font=F.mono_s, fg=C.FAINT, bg=C.BG2).pack(side=tk.RIGHT)
        return sb

    # ───────────────────────── plumbing ─────────────────────────
    def ui(self, fn, *args):
        self.q.put((fn, args))

    def sdk(self, serial):
        return self.device_info.get(serial, {}).get("sdk")

    def run_exclusive(self, job):
        """Run focus-stealing jobs (synthetic key presses) one at a time. job(finish) must call finish()."""
        self._excl.append(job)
        if not self._excl_busy:
            self._excl_next()

    def _excl_next(self):
        if not self._excl:
            self._excl_busy = False
            return
        self._excl_busy = True
        job = self._excl.pop(0)
        done = []

        def finish():
            if not done:
                done.append(1)
                self.root.after(120, self._excl_next)
        try:
            job(finish)
        except Exception as e:
            print("exclusive job error:", e)
            finish()

    def _pump(self):
        try:
            while True:
                fn, args = self.q.get_nowait()
                try:
                    fn(*args)
                except tk.TclError:
                    pass
                except Exception as e:
                    print("ui error:", e)
        except queue.Empty:
            pass
        self.root.after(30, self._pump)

    def _tick(self):
        self.header.set_time(datetime.now(), self.vars["clock_24h"].get())
        try:
            self._tc_refresh()
        except tk.TclError:
            pass
        self.root.after(1000 - datetime.now().microsecond // 1000 + 5, self._tick)

    def _auto_tick(self):
        if self.vars["auto_refresh"].get() and not self._refreshing:
            self.refresh_devices(quiet=True)
        self.root.after(3000, self._auto_tick)

    def _route_wheel(self, e):
        try:
            w = self.root.winfo_containing(e.x_root, e.y_root)
        except Exception:
            return
        while w is not None:
            if isinstance(w, ScrollFrame):
                w.wheel(e.delta)
                return
            w = getattr(w, "master", None)

    def _bind_keys(self):
        r = self.root
        r.bind("<F5>", lambda e: self.refresh_devices())
        r.bind("<Control-m>", lambda e: self.start_mirror_selected())
        r.bind("<Control-S>", lambda e: self.stop_all_mirrors())
        r.bind("<Control-l>", lambda e: self.clear_log())
        r.bind("<Control-a>", self._ctrl_a)
        for seq in ("<Control-equal>", "<Control-plus>"):
            r.bind(seq, lambda e: self._zoom_wheel(120))
        r.bind("<Control-minus>", lambda e: self._zoom_wheel(-120))
        r.bind("<Control-0>", lambda e: self.fit_tiles())
        r.bind("<Control-b>", lambda e: self.toggle_sidebar())
        r.bind("<Escape>", lambda e: self.unfocus())

    def _ctrl_a(self, _e):
        if isinstance(self.root.focus_get(), (tk.Entry, tk.Text)):
            return None
        self.device_list.select_all()
        return "break"

    def _save(self):
        data = {k: v.get() for k, v in self.vars.items()}
        entry = getattr(self, "wifi_entry", None)
        data["wifi_addr"] = entry.get() if entry else self._wifi_saved
        data["shell_history"] = self.shell_history[-30:]
        data["devices"] = self.units
        data["tweaks"] = getattr(self, "tweaks_dirty", {})
        dns = getattr(self, "dns_entry", None)
        data["dns"] = (dns.get() if dns else self._dns_saved) or "8.8.8.8"
        data["timeclock"] = self.tc_cfg
        save_settings(data)

    # ───────────────────────── log ─────────────────────────
    def log(self, message, level=None):
        message = str(message)
        if level is None:
            low = message.lower()
            level = "err" if ("error" in low or "fail" in low) else "warn" if "warn" in low else "info"
        self.ui(self._append_log, message, level)

    def _append_log(self, msg, level):
        t = self.log_text
        t.config(state="normal")
        t.insert(tk.END, datetime.now().strftime("%H:%M:%S  "), "time")
        if msg.startswith("[") and "]" in msg:
            i = msg.index("]") + 1
            t.insert(tk.END, msg[:i], "dev")
            msg = msg[i:]
        t.insert(tk.END, msg + "\n", level)
        if int(t.index("end-1c").split(".")[0]) > 3000:
            t.delete("1.0", "1000.0")
        t.see(tk.END)
        t.config(state="disabled")

    def clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state="disabled")

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log_text.get("1.0", tk.END))
        self.toast.show("Log copied to clipboard")

    def _toggle_log(self):
        if self.log_holder.winfo_ismapped():
            self.log_holder.pack_forget()
            self.log_toggle_btn.set(icon="chev_up")
        else:
            self.log_holder.pack(fill=tk.X)
            self.log_toggle_btn.set(icon="chev_down")

    # ───────────────────────── tools / devices ─────────────────────────
    def _probe_tools(self):
        def first_line(cmd):
            try:
                r = run(cmd, timeout=8)
                return ((r.stdout or r.stderr or "").strip().splitlines() or [""])[0]
            except Exception:
                return None

        a = first_line(["adb", "version"])
        s = first_line(["scrcpy", "--version"])

        def apply():
            if a:
                m = re.search(r"version ([\d.]+)", a)
                self.adb_chip.set(f"ADB {m.group(1) if m else 'OK'}", C.GREEN)
            else:
                self.adb_chip.set("ADB MISSING", C.RED)
            if s:
                m = re.search(r"scrcpy v?([\d.]+)", s)
                self.scrcpy_chip.set(f"SCRCPY {m.group(1) if m else 'OK'}", C.GREEN)
            else:
                self.scrcpy_chip.set("SCRCPY MISSING", C.RED)
        self.ui(apply)

    def refresh_devices(self, quiet=False):
        if self._refreshing:
            return
        self._refreshing = True

        def work():
            try:
                try:
                    run(["adb", "start-server"], timeout=15)
                    r = run(["adb", "devices", "-l"], timeout=15)
                except FileNotFoundError:
                    self.ui(self._adb_missing)
                    return
                except Exception as e:
                    self.log(f"ADB error: {e}", "err")
                    return
                devs = []
                for line in (r.stdout or "").splitlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 2:
                        extras = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
                        devs.append((parts[0], parts[1], extras))
                self.ui(self._apply_devices, devs, quiet)
                full = not quiet or time.time() - self._last_full > 30
                if full:
                    self._last_full = time.time()
                for serial, st, _ in devs:
                    if st == "device" and (full or serial not in self._fetched):
                        self._fetched.add(serial)
                        self.ui(self._apply_info, serial, fetch_info(serial))
            finally:
                self._refreshing = False

        bg_thread(work)

    def _adb_missing(self):
        self.toast.show("adb is not in PATH", C.RED)
        self.log("adb is not in PATH.", "err")
        self.adb_chip.set("ADB MISSING", C.RED)

    def _apply_devices(self, devs, quiet):
        new = {s: st for s, st, _ in devs}
        for s, st, extras in devs:
            if s not in self.device_info and extras.get("model"):
                self.device_info[s] = {"model": extras["model"].replace("_", " ")}
        old = self.known
        if quiet:
            for s, st in new.items():
                if s not in old:
                    self.log(f"[{s}] detected ({st})", "ok" if st == "device" else "warn")
                elif old[s] != st:
                    self.log(f"[{s}] state → {st}", "ok" if st == "device" else "warn")
            for s in old:
                if s not in new:
                    self.log(f"[{s}] disconnected", "warn")
                    self._fetched.discard(s)
        else:
            usable = [s for s, st in new.items() if st == "device"]
            if usable:
                self.log("Devices: " + ", ".join(usable), "ok")
            else:
                self.log("No devices. Enable USB debugging and authorize the PC.", "warn")
        if new != old:
            for s, st in new.items():
                if st == "unauthorized" and old.get(s) != st:
                    self.log(f"[{s}] unauthorized — accept the RSA prompt on the device", "warn")
        newly_usable = [s for s, st in new.items() if st == "device" and old.get(s) != "device"]
        for s in new:
            if new[s] == "device":
                self.unit(s)
        for s in old:
            if s not in new or new[s] != "device":
                self.tethered.discard(s)
        self.known = new
        self.device_list.set_devices(list(new.items()), self.device_info, self.units)
        self._update_counts()
        if newly_usable:
            if self.vars["auto_mirror"].get():
                self.start_mirror([s for s in newly_usable if s not in self.panels], quiet=True)
            if self.vars["auto_tether"].get() and self.tether_mgr.ready():
                self.tether(True, [s for s in newly_usable if s not in self.tethered])
            bg_thread(self._adopt_tethers, list(newly_usable))
            if self.tweaks_dirty:
                bg_thread(self._restore_leftover_tweaks, list(newly_usable))

    def _apply_info(self, serial, info):
        if not info:
            return
        self.device_info.setdefault(serial, {}).update(info)
        meta = self.device_info[serial]
        self.device_list.set_info(serial, meta)
        if serial in self.panels:
            self.panels[serial].set_info(meta)
        temp = meta.get("temp")
        if temp is not None:
            label = self.unit_label(serial)
            if temp >= HOT_TEMP_C and serial not in self._hot_warned:
                self._hot_warned.add(serial)
                self.log(f"[{serial}] {label} is running hot: {temp:.1f}°C", "warn")
                self.toast.show(f"{label} overheating — {temp:.0f}°C", C.RED)
            elif temp < HOT_TEMP_C - 3:
                self._hot_warned.discard(serial)
        self._update_counts()

    def _on_selection(self):
        n = len(self.device_list.selected())
        self.sel_chip.set(f"{n} SELECTED", C.PURPLE if n else C.MUTED)

    def _require_selection(self):
        sels = self.device_list.selected()
        if not sels:
            self.toast.show("Select at least one device", C.AMBER)
        return sels

    # ───────────────────────── mirroring ─────────────────────────
    def _opts(self):
        return {k: self.vars[k].get() for k in ("stay_awake", "turn_screen_off", "audio_mode", "show_touches",
                                                "show_clicks", "max_size", "max_fps")}

    def apply_options(self):
        if not self.panels:
            self.toast.show("No open mirrors — options apply when you start one", C.AMBER)
            return
        opts = self._opts()
        for p in list(self.panels.values()):
            p.opts = dict(opts)
            p.restart()
        self.log(f"Options applied — restarting {len(self.panels)} mirror(s)", "sys")
        self.toast.show(f"Restarting {len(self.panels)} mirror(s) with the new options")

    def remember_tweak(self, serial, key, prev):
        self.tweaks_dirty.setdefault(serial, {}).setdefault(key, prev)
        self._save()

    def forget_tweak(self, serial, key):
        d = self.tweaks_dirty.get(serial, {})
        d.pop(key, None)
        if not d:
            self.tweaks_dirty.pop(serial, None)
        self._save()

    def _restore_leftover_tweaks(self, serials):
        """Settings changed in a session that ended without cleanup (crash, power loss) are put back."""
        for s in serials:
            left = dict(self.tweaks_dirty.get(s, {}))
            if not left or s in self.panels:
                continue
            for key, prev in left.items():
                try:
                    _tweak_set(s, key, prev)
                    self.ui(self.forget_tweak, s, key)
                except Exception:
                    pass
            self.log(f"[{s}] restored phone settings left over from last session: {', '.join(left)}", "sys")

    def start_mirror_selected(self):
        self.start_mirror(self._require_selection())

    def start_mirror(self, serials, quiet=False):
        if not serials:
            return
        if not shutil.which("scrcpy"):
            self.toast.show("scrcpy is not in PATH", C.RED)
            self.log("scrcpy is not in PATH.", "err")
            return
        opts = self._opts()
        new = []
        for serial in serials:
            if serial in self.panels:
                if not quiet:
                    self.log(f"[{serial}] already mirrored, skipping", "warn")
                continue
            panel = MirrorPanel(self, self.grid_scroll.inner, serial, self.device_info.get(serial, {}),
                                opts, self.vars["tile_w"].get(), self.unit(serial))
            self.panels[serial] = panel
            new.append(panel)
        if len(new) > 1:
            self.log(f"Starting {len(new)} mirrors one by one (about {len(new) * MIRROR_LAUNCH_GAP_MS // 1000 + 1} s)",
                     "sys")
        for panel in new:
            self.queue_start(panel)
        self._relayout()
        self._update_counts()

    # Starting 20 scrcpy at the same moment floods adb (server push, tunnels) and links fail or hang.
    # Mirrors are started one after another instead; reconnects go through the same line.
    def queue_start(self, panel):
        q = self.__dict__.setdefault("_launch_q", [])
        if panel in q or panel._closed:
            return
        q.append(panel)
        if panel.proc is None and panel._phase in ("boot", "queued", "lost", "fail"):
            panel._set_phase("queued")
        if not getattr(self, "_launching", False):
            self._launch_next()

    def _launch_next(self):
        q = self.__dict__.setdefault("_launch_q", [])
        while q:
            panel = q.pop(0)
            if panel._closed or self.panels.get(panel.serial) is not panel or panel.proc is not None:
                continue
            self._launching = True
            try:
                panel.start()
            except Exception as e:
                self.log(f"[{panel.serial}] start failed: {e}", "err")
            self.root.after(MIRROR_LAUNCH_GAP_MS, self._launch_next)
            return
        self._launching = False

    def alloc_port_slot(self):
        used = self.__dict__.setdefault("_port_slots", set())
        for i in range(MIRROR_PORT_SLOTS):
            if i not in used:
                used.add(i)
                return i
        return None   # 200+ mirrors: fall back to scrcpy's own range

    def release_port_slot(self, slot):
        if slot is not None:
            self.__dict__.setdefault("_port_slots", set()).discard(slot)

    def _on_panel_close(self, panel):
        self.panels.pop(panel.serial, None)
        if self.focused == panel.serial:
            self.focused = None
        self.root.after(20, self._relayout)
        self._update_counts()

    def on_panel_state(self):
        self._update_counts()

    def stop_all_mirrors(self):
        if self.panels:
            self.log(f"Stopping {len(self.panels)} mirror(s)", "sys")
        for panel in list(self.panels.values()):
            panel.close()

    # ───────────────────────── tile size / zoom ─────────────────────────
    def _set_tile_w(self, w):
        w = int(max(TILE_MIN, min(TILE_MAX, round(w / 5) * 5)))
        if w != self.vars["tile_w"].get():
            self.vars["tile_w"].set(w)
            return True
        return False

    def _manual_tile(self, w):
        self.vars["auto_fit"].set(False)          # picking a size by hand turns auto-fit off
        self._set_tile_w(w)

    def _zoom_wheel(self, delta):
        if delta:
            w = self.vars["tile_w"].get()
            step = 10 if w < 160 else TILE_STEP
            self._manual_tile(w + (step if delta > 0 else -step))

    def _sync_fit_btn(self):
        self.fit_btn.set(variant="primary" if self.vars["auto_fit"].get() else "ghost")
        if self.vars["auto_fit"].get():
            self._relayout()

    def toggle_sidebar(self):
        if self.side.winfo_ismapped():
            self.side.pack_forget()
            self.vars["sidebar_hidden"].set(True)
        else:
            self.side.pack(side=tk.LEFT, fill=tk.Y, padx=(0, px(14)), before=self.grid_right)
            self.vars["sidebar_hidden"].set(False)

    # ───────────── focus mode: one big controllable tile ─────────────
    def toggle_focus(self, panel):
        if panel.serial not in self.panels:
            return
        self.focused = None if self.focused == panel.serial else panel.serial
        self._focus_log()
        self._relayout()

    def on_popout(self, panel, popped):
        """A phone moved into (or back from) its own window: the grid closes the gap."""
        if popped and self.focused == panel.serial:
            self.focused = None
            self._focus_log()
        self._relayout()

    def unfocus(self):
        if self.focused:
            self.focused = None
            self._focus_log()
            self._relayout()

    def _focus_log(self):
        """While a tile is enlarged the system log folds away (more height for the phone), then comes back."""
        shown = self.log_holder.winfo_ismapped()
        if self.focused and shown:
            self._toggle_log()
            self._log_auto_hidden = True
        elif not self.focused and getattr(self, "_log_auto_hidden", False):
            self._log_auto_hidden = False
            if not shown:
                self._toggle_log()
        self.root.update_idletasks()

    def _on_screen_click(self, x, y):
        """Left click anywhere over our window (from the mouse hook). View-only tiles swallow clicks,
        so double-clicks on them are detected here."""
        hit = None
        for p in self.panels.values():
            if p.input_enabled or p.pop is not None:
                continue
            h = p.holder
            hx, hy = h.winfo_rootx(), h.winfo_rooty()
            if hx <= x < hx + h.winfo_width() and hy <= y < hy + h.winfo_height():
                hit = p
                break
        if hit is None:
            self._last_click = None
            return
        now = time.monotonic()
        last = self._last_click
        if (last and last[0] == hit.serial and now - last[1] <= WIN32.double_click_time()
                and abs(x - last[2]) <= px(10) and abs(y - last[3]) <= px(10)):
            self._last_click = None
            self.toggle_focus(hit)
        else:
            self._last_click = (hit.serial, now, x, y)

    def _apply_input_modes(self):
        for p in self.panels.values():
            if self.focused:
                p.set_input(p.serial == self.focused)
            else:
                p.set_input(tile_mode(p.tile_w) == "full")   # small tiles: overview only

    def _sync_tile_preset(self):
        w = self.vars["tile_w"].get()
        self.tile_preset.set(next((k for k, v in TILE_PRESETS.items() if v == w), ""))
        self.tile_chip.set(f"{w} PX")

    def _apply_tile(self):
        w = self.vars["tile_w"].get()
        self._sync_tile_preset()
        for p in self.panels.values():
            p.set_tile(w)
        if self._tile_job:
            self.root.after_cancel(self._tile_job)
        self._tile_job = self.root.after(60, self._relayout)

    def fit_tiles(self):
        """Turn auto-fit on: tiles keep resizing so every phone stays visible."""
        if self.vars["auto_fit"].get():
            self._relayout()
        else:
            self.vars["auto_fit"].set(True)
        self.toast.show("Auto-fit on — every phone stays visible" if self.panels else "Auto-fit on")

    def _relayout(self):
        for p in self.panels.values():
            try:
                p.card.grid_forget()
            except Exception:
                pass
        for w in (self.empty, self.focus_box, self.side_box):
            w.grid_forget()
        try:
            self.grid_scroll.canvas.update_idletasks()
            cw = self.grid_scroll.canvas.winfo_width() - px(4)
            ch = self.grid_scroll.canvas.winfo_height() - px(4)
        except Exception:
            cw, ch = 900, 700
        if not self.panels:
            self.focused = None
            self.empty.config(width=max(px(300), cw), height=max(px(300), ch))
            self.empty.grid(row=0, column=0)
            return
        panels = list(self.panels.values())
        aspect = max(p.aspect() for p in panels)
        auto = self.vars["auto_fit"].get()
        focus = self.panels.get(self.focused)
        if focus is None:
            self.focused = None
        if focus:
            # big tile: as tall as the grid area; the other phones fill the space beside it
            pad, _r, hdr, tbh, gap = TILE_STYLE["full"]
            ov_w = 2 * px(pad) + 6 + 2 * px(gap)
            ov_h = 2 * px(pad) + 6 + px(hdr) + px(tbh) + 2 * px(gap)
            others = [p for p in panels if p is not focus]
            fw = int((ch - ov_h) / focus.aspect())
            fw = max(px(120), min(fw, int(cw * (0.62 if others else 1.0)) - ov_w))
            card_w = max(fw + 2 * px(pad) + 6, px(FULL_TILE_MIN + 20)) + 2 * px(gap)   # header/toolbar need ~280
            for p in others:
                if p.focused:
                    p.set_focus(False)
            focus.set_focus(True, fw)
            self.focus_box.grid(row=0, column=0, sticky="nw")
            focus.card.grid(in_=self.focus_box, row=0, column=0, padx=px(gap), pady=px(gap), sticky="nw")
            if others:
                side_w = cw - card_w
                if auto:
                    w = min(best_fit_width(len(others), side_w, ch, aspect), max(TILE_MIN, int(fw / SCALE * 0.55)))
                    if self._set_tile_w(w):
                        return                    # the new size triggers another layout pass
                tile = self.vars["tile_w"].get()
                bw, _bh = tile_box(tile, aspect)
                cols = int(side_w // bw)
                if cols >= 1:
                    self.side_box.grid(row=0, column=1, sticky="nw")
                else:                             # no room beside it: put the others underneath
                    self.side_box.grid(row=1, column=0, sticky="nw")
                    cols = max(1, int(cw // bw))
                g = px(TILE_STYLE[tile_mode(tile)][4])
                for i, p in enumerate(others):
                    p.card.grid(in_=self.side_box, row=i // cols, column=i % cols, padx=g, pady=g, sticky="n")
        else:
            for p in panels:
                if p.focused:
                    p.set_focus(False)
            if auto and self._set_tile_w(best_fit_width(len(panels), cw, ch, aspect)):
                return
            tile = self.vars["tile_w"].get()
            bw, _bh = tile_box(tile, aspect)
            cols = max(1, int(cw // bw))
            g = px(TILE_STYLE[tile_mode(tile)][4])
            for i, p in enumerate(panels):
                p.card.grid(row=i // cols, column=i % cols, padx=g, pady=g, sticky="n")
        self._apply_input_modes()

    def _update_counts(self):
        live = {s for s, p in self.panels.items() if p.active}
        links = len(live)
        n_dev = self.device_list.usable_count()
        self.header.dev_chip.set(f"DEVICES {n_dev}", C.PURPLE if n_dev else C.MUTED)
        self.header.link_chip.set(f"LINKS {links}", C.GREEN if links else C.MUTED)
        self.grid_chip.set(f"{links} LINK{'S' if links != 1 else ''}", C.GREEN if links else C.MUTED)
        for s in self.device_list.rows:
            self.device_list.set_flag(s, live=s in live, net=s in self.tethered)
        temps = [self.device_info.get(s, {}).get("temp") for s in self.device_list.usable()]
        temps = [x for x in temps if x is not None]
        if temps:
            hottest = max(temps)
            col = C.RED if hottest >= HOT_TEMP_C else C.AMBER if hottest >= HOT_TEMP_C - 4 else C.GREEN
            self.header.temp_chip.set(f"MAX {hottest:.0f}°C", col)
        else:
            self.header.temp_chip.set("TEMP --", C.MUTED)
        self.header.net_chip.set(f"NET {len(self.tethered)}", C.PURPLE_HI if self.tethered else C.MUTED)

    # ───────────────────────── screen control ─────────────────────────
    def screen_off_all(self):
        if not self.panels:
            self.toast.show("No active mirrors", C.AMBER)
        for panel in list(self.panels.values()):
            panel.force_screen(True)

    def screen_on_all(self):
        if not self.panels:
            self.toast.show("No active mirrors", C.AMBER)
        for panel in list(self.panels.values()):
            panel.force_screen(False)

    def adb_screen_selected(self, off):
        state = "OFF" if off else "ON"

        def work(serial, sdk):
            ok, msg = adb_display(serial, on=not off, sdk=sdk)
            if ok:
                self.log(f"[{serial}] ADB display {state}", "ok")
            else:
                self.log(f"[{serial}] ADB display {state} failed: {msg}", "err")
                if sdk is not None and sdk < ADB_DISPLAY_MIN_SDK:
                    self.log(f"[{serial}] tip: start a mirror — its SCREEN button works on any Android version", "warn")
                    self.ui(self.toast.show, "Android < 15: start a mirror to switch the screen", C.AMBER)

        for serial in self._require_selection():
            panel = self.panels.get(serial)
            if panel and panel.active:
                panel.set_screen(off)  # scrcpy shortcut + cmd display: works on every Android version
            else:
                bg_thread(work, serial, self.sdk(serial))

    # ───────────────────────── toolkit ─────────────────────────
    def wifi_connect(self):
        addr = self.wifi_entry.get()
        if not addr:
            self.toast.show("Enter an IP address first", C.AMBER)
            return
        if ":" not in addr:
            addr += ":5555"
        self.log(f"adb connect {addr}", "sys")
        self._save()

        def work():
            try:
                r = run(["adb", "connect", addr], timeout=15)
                out = ((r.stdout or "") + (r.stderr or "")).strip()
            except Exception as e:
                out = f"error: {e}"
            low = out.lower()
            good = "connected" in low and "cannot" not in low and "failed" not in low
            self.log(out or "no output", "ok" if good else "err")
            self.ui(self.toast.show, "Wi-Fi link established" if good else "Wi-Fi connect failed",
                    C.GREEN if good else C.RED)
            self.ui(self.refresh_devices)

        bg_thread(work)

    def enable_tcpip(self):
        sels = self._require_selection()

        def work(serial):
            try:
                r = run_adb(serial, "shell", "ip route", timeout=8)
                m = re.search(r"src (\d+\.\d+\.\d+\.\d+)", r.stdout or "")
                ip = m.group(1) if m else None
                r = run_adb(serial, "tcpip", "5555", timeout=10)
                out = ((r.stdout or "") + (r.stderr or "")).strip()
                self.log(f"[{serial}] tcpip 5555 → {out}", "ok" if r.returncode == 0 else "err")
                if ip and r.returncode == 0:
                    self.log(f"[{serial}] device IP {ip} — press CONNECT to go wireless", "ok")
                    self.ui(self.wifi_entry.set, f"{ip}:5555")
            except Exception as e:
                self.log(f"[{serial}] tcpip error: {e}", "err")

        for serial in sels:
            bg_thread(work, serial)

    def run_shell(self, _event=None):
        cmd = self.shell_entry.get()
        if not cmd:
            return "break"
        sels = self._require_selection()
        if not sels:
            return "break"
        if cmd in self.shell_history:
            self.shell_history.remove(cmd)
        self.shell_history.append(cmd)
        self._hist_idx = None
        self._save()
        self.log(f"$ {cmd}   →   {len(sels)} device(s)", "sys")

        def work(serial):
            try:
                r = run_adb(serial, "shell", cmd, timeout=30)
                out = ((r.stdout or "") + (r.stderr or "")).strip()
                lines = out.splitlines() or ["(no output)"]
                lvl = "info" if r.returncode == 0 else "err"
                for ln in lines[:60]:
                    self.log(f"[{serial}] {ln}", lvl)
                if len(lines) > 60:
                    self.log(f"[{serial}] … {len(lines) - 60} more lines", "warn")
            except subprocess.TimeoutExpired:
                self.log(f"[{serial}] command timed out", "err")
            except Exception as e:
                self.log(f"[{serial}] shell error: {e}", "err")

        for serial in sels:
            bg_thread(work, serial)
        return "break"

    def _hist(self, step):
        h = self.shell_history
        if not h:
            return "break"
        if self._hist_idx is None:
            self._hist_idx = len(h)
        self._hist_idx = max(0, min(len(h), self._hist_idx + step))
        self.shell_entry.set(h[self._hist_idx] if self._hist_idx < len(h) else "")
        self.shell_entry.entry.icursor(tk.END)
        return "break"

    def open_shots(self):
        SHOT_DIR.mkdir(exist_ok=True)
        try:
            if IS_WIN:
                os.startfile(SHOT_DIR)
            else:
                subprocess.Popen(["xdg-open", str(SHOT_DIR)])
        except Exception as e:
            self.log(f"Could not open folder: {e}", "err")

    # ───────────────────────── unit numbers ─────────────────────────
    def unit(self, serial):
        """Persistent farm unit for a device: {"num": int, "name": str}. New devices get the lowest free number."""
        u = self.units.get(serial)
        if u is None:
            used = {v["num"] for v in self.units.values()}
            num = next(i for i in range(1, len(used) + 2) if i not in used)
            u = self.units[serial] = {"num": num, "name": ""}
            self.log(f"[{serial}] assigned unit #{num:02d}", "sys")
            self._save()
        return u

    def unit_label(self, serial):
        u = self.units.get(serial)
        if not u:
            return serial
        return f"#{u['num']:02d}" + (f" {u['name']}" if u.get("name") else "")

    def _units_changed(self, serials=()):
        self._save()
        self.device_list.set_devices(list(self.known.items()), self.device_info, self.units)
        for s, p in self.panels.items():
            p.set_unit(self.units.get(s, {}))
        self._update_counts()

    def ask_string(self, title, prompt, initial="", secret=False, ok_text="SAVE"):
        """Themed modal text prompt. Returns the text, or None if cancelled. secret=True hides the typing."""
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.configure(bg=C.BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        card = Card(dlg, title=title, pad=16)
        card.pack(fill=tk.BOTH, expand=True, padx=px(12), pady=px(12))
        tk.Label(card.body, text=prompt, font=F.body, fg=C.SOFT, bg=C.SURFACE, anchor="w",
                 justify="left").pack(fill=tk.X, pady=(0, px(8)))
        entry = REntry(card.body, "")
        entry.config(width=px(300))
        entry.pack(fill=tk.X)
        if secret:
            entry.entry.config(show="•")
        entry.set(initial)
        result = {"value": None}

        def ok(_e=None):
            result["value"] = entry.var.get().strip() if not entry._ph else ""
            dlg.destroy()

        row = tk.Frame(card.body, bg=C.SURFACE)
        row.pack(fill=tk.X, pady=(px(14), 0))
        RButton(row, ok_text, variant="primary", height=34, width=90, command=ok).pack(side=tk.RIGHT)
        RButton(row, "CANCEL", height=34, width=90, command=dlg.destroy).pack(side=tk.RIGHT, padx=(0, px(8)))
        entry.entry.bind("<Return>", ok)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dlg.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dlg.winfo_height()) // 3
        dlg.geometry(f"+{x}+{y}")
        style_titlebar(dlg)
        entry.entry.focus_set()
        entry.entry.select_range(0, tk.END)
        dlg.grab_set()
        self.root.wait_window(dlg)
        return result["value"]

    def set_unit_number(self, serial):
        u = self.unit(serial)
        val = self.ask_string("Unit number", f"Number for {serial} (1-999).\nIf another phone has it, they swap.",
                              str(u["num"]))
        if val is None:
            return
        if not val.isdigit() or not 1 <= int(val) <= 999:
            self.toast.show("Enter a number from 1 to 999", C.AMBER)
            return
        new = int(val)
        other = next((s for s, v in self.units.items() if v["num"] == new and s != serial), None)
        if other:
            self.units[other]["num"] = u["num"]
            self.log(f"[{other}] unit #{new:02d} → #{u['num']:02d} (swapped)", "sys")
        u["num"] = new
        self.log(f"[{serial}] unit number set to #{new:02d}", "ok")
        self._units_changed()

    def rename_unit(self, serial):
        u = self.unit(serial)
        val = self.ask_string("Unit name", f"Name for unit #{u['num']:02d} ({serial}).\nLeave empty to clear.",
                              u.get("name", ""))
        if val is None:
            return
        u["name"] = val[:28]
        self.log(f"[{serial}] name set to '{u['name']}'" if u["name"] else f"[{serial}] name cleared", "ok")
        self._units_changed()

    def renumber_units(self):
        usable = self.device_list.usable()
        if not usable:
            self.toast.show("No devices connected", C.AMBER)
            return
        if not messagebox.askyesno("Renumber units",
                                   f"Renumber the {len(usable)} connected phone(s) to 1…{len(usable)} "
                                   "in their current order?\nDisconnected phones keep no number.", parent=self.root):
            return
        self.units = {s: {"num": i + 1, "name": self.units.get(s, {}).get("name", "")} for i, s in enumerate(usable)}
        self.log(f"Renumbered {len(usable)} unit(s) to 1…{len(usable)}", "ok")
        self._units_changed()
        self.toast.show("Units renumbered — push wallpapers to update the phones")

    def _device_menu(self, row, event):
        s = row.serial
        m = tk.Menu(self.root, tearoff=0, bg=C.SURFACE2, fg=C.TEXT, activebackground=C.PURPLE,
                    activeforeground=C.INK, disabledforeground=C.PURPLE_HI, font=F.body, bd=0, relief="flat")
        m.add_command(label=f"  {self.unit_label(s)}   ·   {s}", state="disabled")
        m.add_separator()
        if row.usable:
            if s in self.panels:
                m.add_command(label="  Reconnect mirror", command=self.panels[s].restart)
                m.add_command(label="  Close mirror", command=self.panels[s].close)
            else:
                m.add_command(label="  Mirror", command=lambda: self.start_mirror([s]))
            m.add_separator()
            m.add_command(label="  Set unit number…", command=lambda: self.set_unit_number(s))
            m.add_command(label="  Rename…", command=lambda: self.rename_unit(s))
            m.add_command(label="  Push numbered wallpaper", command=lambda: self.push_wallpapers([s]))
            m.add_separator()
            if s in self.tethered:
                m.add_command(label="  Stop reverse tether", command=lambda: self.tether(False, [s]))
            else:
                m.add_command(label="  Start reverse tether", command=lambda: self.tether(True, [s]))
            m.add_command(label="  Test internet", command=lambda: self.test_internet([s]))
            m.add_command(label="  Wake screen", command=lambda: self.bulk_key(224, "WAKE", [s]))
            m.add_separator()
        m.add_command(label="  Copy serial", command=lambda: (self.root.clipboard_clear(),
                                                              self.root.clipboard_append(s),
                                                              self.toast.show("Serial copied")))
        if row.usable:
            m.add_command(label="  Reboot…", command=lambda: self.bulk_reboot([s]))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    # ───────────────────────── numbered wallpapers ─────────────────────────
    def _wall_total(self):
        return max([v["num"] for v in self.units.values()] or [0])

    def push_wallpapers(self, serials=None):
        serials = serials or self._require_selection()
        if not serials:
            return
        if not have_pil():
            self.toast.show("Install Pillow first:  pip install pillow", C.RED)
            self.log("Numbered wallpapers need Pillow:  pip install pillow", "err")
            return
        lock, total = self.vars["wall_lock"].get(), self._wall_total()
        jobs = [(s, dict(self.unit(s)), self.device_info.get(s, {}).get("res")) for s in serials]
        self.log(f"Rendering numbered wallpapers for {len(jobs)} phone(s)…", "sys")
        self.toast.show(f"Pushing wallpapers to {len(jobs)} phone(s)…")

        def one(job):
            s, u, res = job
            try:
                res = res or fetch_info(s).get("res") or (1080, 2340)
                img = render_wallpaper(u["num"], total, s, u.get("name", ""), res)
                WALL_DIR.mkdir(exist_ok=True)
                safe = re.sub(r"[^\w.-]", "_", s)
                path = WALL_DIR / f"unit_{u['num']:02d}_{safe}.png"
                img.save(path)
                ok, msg = set_device_wallpaper(s, path, lock)
                self.log(f"[{s}] wallpaper #{u['num']:02d} " + ("set" if ok else f"failed: {msg}"),
                         "ok" if ok else "err")
                return ok
            except Exception as e:
                self.log(f"[{s}] wallpaper error: {e}", "err")
                return False

        def work():
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=4) as ex:
                done = sum(ex.map(one, jobs))
            self.ui(self.toast.show, f"Wallpapers set on {done}/{len(jobs)} phone(s)",
                    C.GREEN if done == len(jobs) else C.AMBER)

        bg_thread(work)

    def preview_wallpaper(self):
        if not have_pil():
            self.toast.show("Install Pillow first:  pip install pillow", C.RED)
            return
        sels = self.device_list.selected()
        s = sels[0] if sels else "PREVIEW-SERIAL"
        u = self.unit(s) if sels else {"num": 1, "name": "Preview"}
        res = self.device_info.get(s, {}).get("res") or (1080, 2340)

        def work():
            try:
                WALL_DIR.mkdir(exist_ok=True)
                path = WALL_DIR / f"preview_unit_{u['num']:02d}.png"
                render_wallpaper(u["num"], self._wall_total() or 1, s, u.get("name", ""), res).save(path)
                self.log(f"Preview saved → {path}", "ok")
                if IS_WIN:
                    os.startfile(path)
            except Exception as e:
                self.log(f"Preview failed: {e}", "err")

        bg_thread(work)

    # ───────────────────────── reverse tethering ─────────────────────────
    def _update_tether_ui(self):
        g = self.tether_mgr
        if not g.ready():
            self.tether_chip.set("CLIENT MISSING", C.RED)
        elif g.relay_alive():
            self.tether_chip.set(f"RELAY ON · {len(self.tethered)}", C.GREEN)
        else:
            self.tether_chip.set("READY", C.PURPLE)

    def tether(self, on, serials=None):
        serials = serials or self._require_selection()
        if not serials:
            return
        if not self.tether_mgr.ready():
            self.toast.show("Tether client missing from this build", C.RED)
            return
        dns = self.dns_entry.get() or "8.8.8.8"
        self.tether_mgr.dns = dns
        self._save()
        if on:
            self.toast.show(f"Starting reverse tether on {len(serials)} phone(s)…")

        def work(s):
            try:
                ok, msg = self.tether_mgr.start(s, dns) if on else self.tether_mgr.stop(s)
            except Exception as e:
                ok, msg = False, str(e)
            label = self.unit_label(s)
            if ok:
                self.log(f"[{s}] {label} reverse tether {'ON — ' + msg if on else 'OFF'}",
                         "ok" if (not on or "online" in msg) else "warn")
            else:
                self.log(f"[{s}] reverse tether {'start' if on else 'stop'} failed: {msg}", "err")

            def apply():
                if ok:
                    (self.tethered.add if on else self.tethered.discard)(s)
                self._update_counts()
                self._update_tether_ui()
            self.ui(apply)

        for s in serials:
            bg_thread(work, s)

    # ───────────────────────── tether watchdog ─────────────────────────
    def _tether_watch(self):
        """Every 5 s: keep each tethered phone's relay + adb tunnel alive (adb drops the tunnel whenever its
        server restarts or the phone reconnects, which leaves the phone with a VPN but no internet)."""
        if self.tethered and not self._watching and self.tether_mgr.ready():
            self._watching = True
            serials = [s for s in self.tethered if self.known.get(s) == "device"]

            def work():
                try:
                    for s in serials:
                        try:
                            fixed = self.tether_mgr.repair(s)
                        except Exception as e:
                            fixed = [f"repair error: {e}"]
                        if fixed:
                            self.log(f"[{s}] reverse tether: {', '.join(fixed)}", "warn")
                            self.ui(self._update_tether_ui)
                finally:
                    self._watching = False
            bg_thread(work)
        self.root.after(5000, self._tether_watch)

    def _adopt_tethers(self, serials):
        """A phone whose tether VPN is still running (earlier session, replug) gets its relay and tunnel back
        instead of being left with a dead VPN. Phones on the old gnirehtet client are moved to ours."""
        for s in serials:
            try:
                if s in self.tethered:
                    continue
                mgr = self.tether_mgr
                if mgr.client_running(s):
                    fixed = mgr.repair(s)
                    mgr.keep_alive(s)
                    self.log(f"[{s}] found an active reverse tether — reconnected"
                             + (f" ({', '.join(fixed)})" if fixed else ""), "ok")
                elif mgr.legacy_running(s) and mgr.ready():
                    self.log(f"[{s}] old gnirehtet tether found — switching to the SCIENTIST tether", "sys")
                    ok, msg = mgr.start(s)
                    self.log(f"[{s}] reverse tether {'ON — ' + msg if ok else 'failed: ' + msg}",
                             "ok" if ok and "online" in msg else "warn")
                    if not ok:
                        continue
                else:
                    continue

                def adopt(s=s):
                    self.tethered.add(s)
                    self._update_counts()
                    self._update_tether_ui()
                self.ui(adopt)
            except Exception as e:
                self.log(f"[{s}] tether check failed: {e}", "err")

    def test_internet(self, serials=None):
        serials = serials or self._require_selection()
        if not serials:
            return

        def work(s):
            via = "PC (reverse tether)" if s in self.tethered else "the phone's own network"
            ok = Tether.internet_ok(s)
            self.log(f"[{s}] {self.unit_label(s)} internet " + (f"ONLINE via {via}" if ok else "OFFLINE"),
                     "ok" if ok else "err")
            self.ui(self.toast.show, f"{self.unit_label(s)}: {'online' if ok else 'OFFLINE'}",
                    C.GREEN if ok else C.RED)
        self.log(f"Testing internet on {len(serials)} phone(s)…", "sys")
        for s in serials:
            bg_thread(work, s)

    # ───────────────────────── farm actions (selected devices) ─────────────────────────
    def _bulk(self, serials, desc, fn):
        """Run fn(serial) -> (ok, msg) on every serial in parallel and log the results."""
        serials = serials or self._require_selection()
        if not serials:
            return
        self.log(f"{desc}  →  {len(serials)} device(s)", "sys")

        def work(s):
            try:
                ok, msg = fn(s)
            except subprocess.TimeoutExpired:
                ok, msg = False, "timed out"
            except Exception as e:
                ok, msg = False, str(e)
            self.log(f"[{s}] {desc}: {msg}" if msg else f"[{s}] {desc}" + ("" if ok else " failed"),
                     "ok" if ok else "err")

        for s in serials:
            bg_thread(work, s)

    @staticmethod
    def _result(r, ok_text=""):
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return r.returncode == 0, (out.splitlines()[-1] if out else ok_text)

    def bulk_key(self, code, name, serials=None):
        self._bulk(serials, f"key {name}",
                   lambda s: self._result(run_adb(s, "shell", "input", "keyevent", str(code), timeout=8), "sent"))

    def bulk_brightness(self, auto):
        cmd = ("settings put system screen_brightness_mode 1" if auto else
               "settings put system screen_brightness_mode 0; settings put system screen_brightness 1")
        self._bulk(None, "brightness auto" if auto else "brightness minimum",
                   lambda s: self._result(run_adb(s, "shell", cmd, timeout=8), "done"))

    def bulk_install(self):
        serials = self._require_selection()
        if not serials:
            return
        apks = filedialog.askopenfilenames(parent=self.root, title="Install APK on selected phones",
                                           filetypes=[("Android package", "*.apk"), ("All files", "*.*")])
        if not apks:
            return

        def install(s):
            results = []
            for apk in apks:
                r = run_adb(s, "install", "-r", "-g", apk, timeout=300)
                ok, msg = self._result(r)
                results.append(ok and "Success" in (r.stdout or ""))
                if not results[-1]:
                    return False, f"{Path(apk).name}: {msg}"
            return True, f"installed {len(apks)} APK(s)"
        self._bulk(serials, "install APK", install)

    def bulk_push(self):
        serials = self._require_selection()
        if not serials:
            return
        files = filedialog.askopenfilenames(parent=self.root, title="Push files to /sdcard/Download on selected phones")
        if not files:
            return

        def push(s):
            for f in files:
                ok, msg = self._result(run_adb(s, "push", f, "/sdcard/Download/", timeout=600))
                if not ok:
                    return False, f"{Path(f).name}: {msg}"
            return True, f"pushed {len(files)} file(s) to /sdcard/Download"
        self._bulk(serials, "push file", push)

    def bulk_launch(self, stop=False):
        target = self.launch_entry.get()
        if not target:
            self.toast.show("Enter a package name or URL", C.AMBER)
            return
        if re.match(r"^[a-z]+://", target, re.I):
            if stop:
                self.toast.show("Stop needs a package name", C.AMBER)
                return
            cmd = ["am", "start", "-a", "android.intent.action.VIEW", "-d", target]
            desc = f"open {target}"
        elif stop:
            cmd, desc = ["am", "force-stop", target], f"force-stop {target}"
        else:
            cmd = ["monkey", "-p", target, "-c", "android.intent.category.LAUNCHER", "1"]
            desc = f"launch {target}"

        def go(s):
            r = run_adb(s, "shell", *cmd, timeout=20)
            out = ((r.stdout or "") + (r.stderr or "")).lower()
            bad = "no activities found" in out or "error" in out or "aborted" in out
            return r.returncode == 0 and not bad, ("done" if not bad else out.strip().splitlines()[-1])
        self._bulk(None, desc, go)

    def bulk_reboot(self, serials=None):
        serials = serials or self._require_selection()
        if not serials:
            return
        names = ", ".join(self.unit_label(s) for s in serials[:6]) + (" …" if len(serials) > 6 else "")
        if not messagebox.askyesno("Reboot", f"Reboot {len(serials)} phone(s)?\n\n{names}", parent=self.root):
            return
        self._bulk(serials, "reboot", lambda s: self._result(run_adb(s, "reboot", timeout=20), "rebooting"))

    # ───────────────────────── FB Daily ─────────────────────────
    def _fb_alive(self):
        return [s for s, t in self.fb_threads.items() if t.is_alive()]

    def _fb_cfg(self, eng):
        cfg = eng.load_config()
        pre = FB_INTENSITY.get(self.vars["fb_intensity"].get(), FB_INTENSITY["Normal"])
        cfg["session_minutes"] = pre["session_minutes"]
        cfg["between_sessions_seconds"] = pre["between_sessions_seconds"]
        cfg["engage"]["add_friends_range"] = pre["add_friends_range"]
        cfg["engage"]["confirm_range"] = pre["confirm_range"]
        engage, friends = self.vars["fb_engage"].get(), self.vars["fb_friends"].get()
        cfg["weights"]["engage"] = pre["weights_engage"] if engage else 0
        cfg["engage"]["enabled"] = bool(engage)
        cfg["weights"]["add_friends"] = pre["weights_add"] if friends else 0
        cfg["weights"]["confirm_requests"] = pre["weights_conf"] if friends else 0
        cfg["chat"]["enabled"] = bool(self.vars["fb_chat"].get())
        cfg["switch_accounts"] = False           # FB Lite's account switcher isn't mapped yet
        if not self.vars["fb_pacing"].get():     # full activity from the first session
            cfg.setdefault("warmup", {})["enabled"] = False
            cfg["rest_days_per_week"] = [0, 0]
        return cfg

    def _fb_log(self, serial, msg):
        """Replacement for the engine's print-logger (called from worker threads)."""
        self.log(f"[{self.unit_label(serial)}] FB · {msg}")

    def _fb_worker(self, eng, serial, cfg):
        try:
            eng._phone_forever(serial, cfg, self.fb_totals, self.fb_lock)
        except Exception as e:
            self.log(f"[{self.unit_label(serial)}] FB Daily crashed: {e}", "err")
        finally:
            self.ui(self._fb_refresh_ui)

    def bulk_fbdaily_start(self):
        if self._fb_stopping:
            self.toast.show("FB Daily is still stopping — try again in a moment", C.AMBER)
            return
        eng = load_fbdaily()
        if eng is None:
            self.log(f"FB Daily engine unavailable: {_FBD['err']}", "err")
            self.toast.show("FB Daily engine not found", C.RED)
            return
        usb = [s for s, st in self.known.items() if st == "device" and ":" not in s]
        sel = self.device_list.selected()
        targets = [s for s in sel if s in usb] if sel else usb
        if sel and not targets:
            self.toast.show("FB Daily runs on USB phones only", C.AMBER)
            return
        if not targets:
            self.toast.show("No USB phones connected", C.AMBER)
            return
        alive = set(self._fb_alive())
        new = [s for s in targets if s not in alive]
        if not new:
            self.toast.show(f"FB Daily already running on {len(alive)} phone(s)")
            return
        if not alive:                         # fresh run: clear old stop flags + counters
            eng.STOP.clear()
            try:
                if os.path.exists(eng.STOP_FILE):
                    os.remove(eng.STOP_FILE)
            except Exception:
                pass
            with self.fb_lock:
                self.fb_totals.clear()
        eng.log = self._fb_log
        cfg = self._fb_cfg(eng)
        if getattr(eng, "fb", None) is None:
            err = getattr(eng, "_FB_IMPORT_ERR", "")
            self.log(f"FB Daily: screen helpers (fb.py) failed to load — phones will browse only ({err})", "warn")
        for s in new:
            t = threading.Thread(target=self._fb_worker, args=(eng, s, cfg), daemon=True,
                                 name=f"fbdaily-{s}")
            self.fb_threads[s] = t
            t.start()
        names = ", ".join(self.unit_label(s) for s in new[:6]) + (" …" if len(new) > 6 else "")
        self.log(f"FB Daily started on {len(new)} phone(s) [{self.vars['fb_intensity'].get()}]: {names}", "ok")
        self.toast.show(f"FB Daily running on {len(alive) + len(new)} phone(s)", C.GREEN)
        self._fb_refresh_ui()

    def bulk_fbdaily_stop(self):
        alive = self._fb_alive()
        eng = _FBD["mod"]
        if not alive or eng is None:
            self.toast.show("FB Daily is not running")
            return
        if self._fb_stopping:
            return
        self._fb_stopping = True
        eng.STOP.set()
        self.log(f"FB Daily stopping — {len(alive)} phone(s) finish their step, then FB Lite closes…", "warn")
        self._fb_refresh_ui()
        threads = [self.fb_threads[s] for s in alive]

        def work():
            deadline = time.time() + 120
            for t in threads:
                t.join(timeout=max(0.1, deadline - time.time()))
            for s in alive:  # safety sweep: FB Lite closed, phone back on its home screen
                try:
                    run_adb(s, "shell", f"am force-stop {FB_PACKAGE}", timeout=10)
                    run_adb(s, "shell", "input keyevent KEYCODE_HOME", timeout=10)
                except Exception:
                    pass
            try:
                if os.path.exists(eng.STOP_FILE):
                    os.remove(eng.STOP_FILE)
            except Exception:
                pass
            self.ui(self._fb_stopped, len(alive))
        bg_thread(work)

    def _fb_stopped(self, n):
        self._fb_stopping = False
        self.fb_threads = {s: t for s, t in self.fb_threads.items() if t.is_alive()}
        self.log(f"FB Daily stopped on {n} phone(s) — FB Lite closed", "ok")
        self.toast.show("FB Daily stopped")
        self._fb_refresh_ui()

    def _fb_refresh_ui(self):
        alive = self._fb_alive()
        if self._fb_stopping:
            self.fb_chip.set("STOPPING", C.AMBER)
        elif alive:
            self.fb_chip.set(f"RUNNING · {len(alive)}", C.GREEN)
        else:
            self.fb_chip.set("IDLE", C.MUTED)
        self.fb_start_btn.set_disabled(self._fb_stopping)
        self.fb_stop_btn.set_disabled(self._fb_stopping or not alive)
        with self.fb_lock:
            t = dict(self.fb_totals)
        self.fb_stats.config(
            text=f"sessions {t.get('sessions', 0)}  ·  reacts {t.get('reacts', 0)}  ·  "
                 f"comments {t.get('comments', 0)}\nshares {t.get('shares', 0)}  ·  "
                 f"replies {t.get('replied', 0)}  ·  friends {t.get('added', 0)}")

    def _fb_tick(self):
        try:
            self._fb_refresh_ui()
        except tk.TclError:
            pass
        self.root.after(2000, self._fb_tick)

    # ───────────────────────── time clock ─────────────────────────
    TC_STATUS = {"off": ("NOT TIMED IN", "OFF"), "working": ("ON DUTY", "ON DUTY"), "break": ("ON BREAK", "BREAK"),
                 "lunch": ("ON LUNCH", "LUNCH"), "out": ("TIMED OUT", "OUT")}

    def _tc_refresh(self):
        if not hasattr(self, "tc_info"):
            return
        now = datetime.now()
        h24 = self.vars["clock_24h"].get()
        cfg = self.tc_cfg
        w = cfg["active"]
        _day, rec = self.tc.current(w, now)
        s = TimeClock.summary(rec, now)
        st = s["state"]
        over = s["over"] > 0
        color = {"off": C.MUTED, "working": C.GREEN, "break": C.AMBER, "lunch": C.AMBER,
                 "out": C.PURPLE_HI}[st]
        status, chip = self.TC_STATUS[st]
        if over and st in ("break", "lunch"):
            status, chip, color = "OVERBREAK", "OVERBREAK", C.RED
        self.header.set_worker(w, status + (f"  ·  IN {fmt_time(s['in'], h24)}" if s["in"] and st != "out" else ""),
                               color)
        key = (w, st, chip, color)
        if self._tc_shown.get("key") != key:
            self._tc_shown["key"] = key
            self.tc_chip.set(chip, color)
            self.tc_worker_btn.set(text=elide(w, F.btn, px(240)))
            for act, enabled in (("in", st == "off"), ("out", st in ("working", "break", "lunch")),
                                 ("break", st in ("working", "break")), ("lunch", st in ("working", "lunch"))):
                b = self.tc_btns[act]
                if act in ("break", "lunch"):
                    name = TC_ACTIONS[act]
                    b.set(text=f"END {name}" if st == act else name, variant="primary" if st == act else "ghost")
                b.set_disabled(not enabled)
        req = (rec or {}).get("req") if st in ("working", "break", "lunch") else None
        sh = ({"time_in": req["in"], "time_out": req["out"], "break_min": req["break"]}
              if req and req.get("in") and req.get("out") else self.tc_shift(w))   # running shift keeps its rules
        self.tc_shift_lbl.config(text=f"SHIFT {fmt_hhmm(sh['time_in'], h24)} – {fmt_hhmm(sh['time_out'], h24)}"
                                      f"  ·  BREAK {fmt_mins(sh['break_min'])}")
        if st == "off":
            info = f"Press TIME IN to start\n{w}'s shift."
        else:
            late = f"   LATE {fmt_mins(math.ceil(s['late']))}" if s["late"] >= 1 else "   on time"
            left = s["allow"] - s["used"]
            used = (f"{fmt_mins(s['used'])} of {fmt_mins(s['allow'])}"
                    + (f"  ·  {fmt_mins(left)} left" if left >= 0 else ""))
            out = fmt_time(s["out"], h24) if s["out"] else "--"
            if s["out"] and s["under"] >= 1:
                out += f"   UNDERTIME {fmt_mins(s['under'])}"
            elif s["out"] and s["ot"] >= 1:
                out += f"   OVERTIME {fmt_mins(s['ot'])}"
            info = (f"IN      {fmt_time(s['in'], h24)}{late}\n"
                    f"BREAK   {fmt_mins(s['break'])}  ({s['n_break']}×)\n"
                    f"LUNCH   {fmt_mins(s['lunch'])}  ({s['n_lunch']}×)\n"
                    f"USED    {used}\n"
                    f"OUT     {out}\n"
                    f"WORKED  {fmt_mins(s['worked'])}")
        self.tc_info.config(text=info)
        alert, acol = "", C.RED
        if s["open"]:
            kind, start = s["open"]
            run = (now - start).total_seconds()
            clock = f"{int(run // 3600)}:{int(run % 3600 // 60):02d}:{int(run % 60):02d}"
            if over:
                alert = f"OVERBREAK  +{fmt_mins(max(1, round(s['over'])))}  ·  {kind} since {fmt_time(start, h24)}"
            else:
                alert, acol = (f"ON {kind.upper()}  {clock}  ·  {fmt_mins(s['allow'] - s['used'])} left", C.AMBER)
        elif over:
            alert = f"OVERBREAK by {fmt_mins(max(1, round(s['over'])))}  ({fmt_mins(s['used'])} of {fmt_mins(s['allow'])})"
        elif s["late"] >= 1 and st != "out":
            alert, acol = f"LATE by {fmt_mins(math.ceil(s['late']))}", C.AMBER
        if self.tc_alert.cget("text") != alert or self.tc_alert.cget("fg") != acol:
            self.tc_alert.config(text=alert, fg=acol)
            if alert:
                self.tc_alert.pack(fill=tk.X, pady=(px(4), 0), after=self.tc_info)
            else:
                self.tc_alert.pack_forget()
        proofs = (rec or {}).get("proofs") or []
        if proofs:
            p = proofs[-1]
            ptxt = f"✓ proof saved: {p.get('action', '')} · {fmt_time(_dt(p.get('saved')), h24)}"
        elif st != "off":
            ptxt = "no proof saved yet for this shift"
        else:
            ptxt = f"proofs → Documents\\{TIMECLOCK_DIR.name}"
        self.tc_proof_lbl.config(text=ptxt)

    def _tc_worker_menu(self, widget=None):
        widget = widget if isinstance(widget, tk.Widget) else self.tc_worker_btn
        m = tk.Menu(self.root, tearoff=0, bg=C.SURFACE2, fg=C.TEXT, activebackground=C.PURPLE,
                    activeforeground=C.INK, bd=0, font=F.body)
        tags = {"off": "", "working": "   · on duty", "break": "   · break", "lunch": "   · lunch", "out": "   · out"}
        for w in self.tc_cfg["workers"]:
            st = TimeClock.state(self.tc.current(w)[1])
            m.add_command(label=("●  " if w == self.tc_cfg["active"] else "     ") + w + tags[st],
                          command=lambda w=w: self.tc_set_worker(w))
        m.add_separator()
        m.add_command(label="     Add / remove workers…  (admin)", command=self.open_admin)
        try:
            m.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())
        finally:
            m.grab_release()

    def tc_set_worker(self, w):
        if w not in self.tc_cfg["workers"] or w == self.tc_cfg["active"]:
            return
        self.tc_cfg["active"] = w
        self._save()
        self._tc_shown.clear()
        self._tc_refresh()
        self.log(f"Time clock: {w} is now on this PC", "sys")

    def tc_punch(self, action):
        w = self.tc_cfg["active"]
        if action == "out":
            st = TimeClock.state(self.tc.current(w)[1])
            if st in ("working", "break", "lunch") and not messagebox.askyesno(
                    "Time out", f"Time out {w} now?\n\nOnly the admin can undo a time out.", parent=self.root):
                return
        try:
            ok, msg, label = self.tc.punch(w, action, self.tc_shift(w))
        except Exception as e:
            self.log(f"Time clock: could not save the punch ({e})", "err")
            self.toast.show("Could not save — see the system log", C.RED)
            return
        if not ok:
            self.toast.show(msg, C.AMBER)
            return
        self.log(f"Time clock — {msg}", "ok")
        s = TimeClock.summary(self.tc.current(w)[1])
        if label in ("TIME IN",) and s["late"] >= 1:
            self.log(f"Time clock — {w} is LATE by {fmt_mins(math.ceil(s['late']))}", "warn")
        if label.endswith(" END") or label == "TIME OUT":
            if s["over"] > 0:
                self.log(f"Time clock — {w} OVERBREAK by {fmt_mins(max(1, round(s['over'])))} "
                         f"({fmt_mins(s['used'])} of {fmt_mins(s['allow'])})", "err")
        if self.tc.csv_locked:
            self.log("Time clock: timesheet.csv is open in another program — it updates once it is closed", "warn")
        self.toast.show(f"{msg}  ·  press SAVE PROOF for the screenshot", C.GREEN)
        self._tc_shown.clear()
        self._tc_refresh()

    def tc_open_folder(self):
        try:
            TIMECLOCK_DIR.mkdir(parents=True, exist_ok=True)
            if not self.tc.csv_path.exists():
                self.tc.write_csv()
            if IS_WIN:
                os.startfile(TIMECLOCK_DIR)
            else:
                subprocess.Popen(["xdg-open", str(TIMECLOCK_DIR)])
        except Exception as e:
            self.log(f"Could not open folder: {e}", "err")

    def tc_save_proof(self):
        if not have_pil():
            self.toast.show("Pillow is missing — cannot take screenshots", C.RED)
            return
        w = self.tc_cfg["active"]
        day, rec = self.tc.current(w)
        if not rec or not rec.get("in"):
            self.toast.show(f"Nothing to save yet — press TIME IN for {w} first", C.AMBER)
            return
        label, at = TimeClock.events(rec)[-1]
        try:  # show the window and the time clock card before the screenshot
            if self.root.state() == "iconic":
                self.root.deiconify()
            self.root.lift()
            if self.side.winfo_ismapped():
                self.side.canvas.yview_moveto(0)
        except tk.TclError:
            pass
        self._tc_shown.clear()
        self._tc_refresh()
        self.root.update()
        self.root.after(250, lambda: self._tc_grab(w, day, rec, label, at))

    def _tc_grab(self, worker, day, rec, label, at):
        from PIL import ImageGrab
        r = self.root
        x, y, wd, ht = r.winfo_rootx(), r.winfo_rooty(), r.winfo_width(), r.winfo_height()
        try:
            shot = ImageGrab.grab(bbox=(x, y, x + wd, y + ht), all_screens=True)
        except Exception as e:
            self.log(f"Time clock: screenshot failed ({e})", "err")
            self.toast.show("Screenshot failed — see the system log", C.RED)
            return
        saved = datetime.now().replace(microsecond=0)
        s = TimeClock.summary(rec, saved)
        req = rec.get("req") or {}
        st_txt = self.TC_STATUS[s["state"]][0]
        lines = [
            f"{label}  ·  {worker}",
            f"{label} at {fmt_time(at, sec=True)}  ·  {at.strftime('%A, %d %B %Y')}",
            "  ·  ".join(x for x in (
                f"Shift {fmt_hhmm(req['in'])} – {fmt_hhmm(req['out'])}" if req.get("in") and req.get("out") else "",
                f"In {fmt_time(s['in'])}" + (f" (LATE {fmt_mins(math.ceil(s['late']))})" if s["late"] >= 1 else ""),
                f"Break {fmt_mins(s['break'])}", f"Lunch {fmt_mins(s['lunch'])}",
                f"Used {fmt_mins(s['used'])} of {fmt_mins(s['allow'])}",
                f"OVERBREAK {fmt_mins(max(1, round(s['over'])))}" if s["over"] > 0 else "",
                f"Out {fmt_time(s['out'])}" if s["out"] else "", st_txt) if x),
            f"Proof saved {saved.strftime('%Y-%m-%d')} {fmt_time(saved, sec=True)}  ·  "
            f"PC {os.environ.get('COMPUTERNAME', '')}  ·  SCIENTIST time clock",
        ]
        hot = s["over"] > 0 or s["late"] >= 1
        safe = re.sub(r"[^\w\- ]+", "_", worker).strip() or "worker"
        folder = TIMECLOCK_DIR / "proofs" / day
        name = f"{safe}_{at.strftime('%H%M%S')}_{label.replace(' ', '-')}"
        accent, bg, fg, muted, red = C.PURPLE, C.BG2, C.TEXT, C.MUTED, C.RED

        def work():
            try:
                from PIL import Image, ImageDraw
                fbig = _wfont([r"C:\Windows\Fonts\bahnschrift.ttf", r"C:\Windows\Fonts\segoeuib.ttf",
                               r"C:\Windows\Fonts\arialbd.ttf"], 34, "Bold")
                fsm = _wfont([r"C:\Windows\Fonts\consola.ttf", r"C:\Windows\Fonts\cour.ttf"], 20)
                pad, fh = 22, 34 + 3 * 30 + 44
                img = Image.new("RGB", (shot.width, shot.height + fh), _rgb(bg))
                img.paste(shot, (0, 0))
                d = ImageDraw.Draw(img)
                y0 = shot.height
                d.rectangle([0, y0, img.width, y0 + 4], fill=_rgb(red if hot else accent))
                d.text((pad, y0 + 16), lines[0], font=fbig, fill=_rgb(accent))
                for i, t in enumerate(lines[1:]):
                    col = red if (i == 1 and hot) else (fg if i == 0 else muted)
                    d.text((pad, y0 + 16 + 44 + i * 30), t, font=fsm, fill=_rgb(col))
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / f"{name}.png"
                k = 2
                while path.exists():
                    path = folder / f"{name}_{k}.png"
                    k += 1
                img.save(path, optimize=True)
                self.ui(self._tc_proof_done, worker, rec, label, saved, path)
            except Exception as e:
                self.log(f"Time clock: could not save the proof ({e})", "err")
                self.ui(self.toast.show, "Could not save the proof — see the system log", C.RED)
        bg_thread(work)

    def _tc_proof_done(self, worker, rec, label, saved, path):
        rec.setdefault("proofs", []).append({"action": label, "saved": saved.isoformat(),
                                             "file": str(path.relative_to(TIMECLOCK_DIR))})
        try:
            self.tc.save()
        except Exception as e:
            self.log(f"Time clock: could not update records.json ({e})", "err")
        self.log(f"Time clock — proof saved: {worker} {label} → {path}", "ok")
        self.toast.show(f"Proof saved · {worker} · {label}", C.GREEN)
        self._tc_refresh()

    # ── admin (password protected) ──
    def _admin_ok(self, pw):
        return tc_hash(pw) == (self.tc_cfg["pw"] or tc_hash(TC_DEFAULT_PASSWORD))

    def open_admin(self):
        if self.admin_win is not None and self.admin_win.winfo_exists():
            self.admin_win.deiconify()
            self.admin_win.lift()
            self.admin_win.focus_set()
            return
        pw = self.ask_string("Admin", "Admin password", secret=True, ok_text="OPEN")
        if pw is None:
            return
        if not self._admin_ok(pw):
            self.log("Time clock: wrong admin password", "warn")
            self.toast.show("Wrong admin password", C.RED)
            return
        self.toast.hide()
        dlg = tk.Toplevel(self.root)
        self.admin_win = dlg
        dlg.title("Admin options")
        dlg.configure(bg=C.BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        card = Card(dlg, title="Admin options", pad=16)
        card.pack(fill=tk.BOTH, expand=True, padx=px(12), pady=px(12))
        Chip(card.header_right, "TIME CLOCK", C.PURPLE_HI, dot=False).pack(side=tk.RIGHT)
        b = card.body
        self._admin_pw_warn = tk.Label(b, text="", font=F.small, fg=C.AMBER, bg=C.SURFACE, anchor="w")
        if not self.tc_cfg["pw"]:
            self._admin_pw_warn.config(text="⚠  The default password is still in use — change it below.")
            self._admin_pw_warn.pack(fill=tk.X, pady=(0, px(6)))

        # workers, each with their own required shift
        self._label(b, "WORKERS  ·  EACH HAS THEIR OWN SHIFT", top=0)
        self._admin_wlist = tk.Frame(b, bg=C.SURFACE)
        self._admin_wlist.pack(fill=tk.X)
        self._admin_draw_workers()
        row = tk.Frame(b, bg=C.SURFACE)
        row.pack(fill=tk.X, pady=(px(6), 0))
        new_e = REntry(row, "New worker name")

        def add():
            name = new_e.get().strip()[:40]
            if not name:
                self.toast.show("Type the worker's name first", C.AMBER)
                return
            if name.lower() in (x.lower() for x in self.tc_cfg["workers"]):
                self.toast.show(f"{name} is already on the list", C.AMBER)
                return
            self.tc_cfg["workers"].append(name)
            self.tc_cfg["shifts"][name] = self._tc_default_shift()
            self._save()
            new_e.set("")
            self._admin_draw_workers()
            self.log(f"Time clock: worker added — {name} (admin)", "sys")
            self._tc_shift_dialog(name, first=True)
        RButton(row, "ADD", icon="plus", variant="primary", height=34, command=add).pack(side=tk.RIGHT,
                                                                                          padx=(px(6), 0))
        new_e.pack(side=tk.LEFT, fill=tk.X, expand=True)
        new_e.entry.bind("<Return>", lambda e: add())
        tk.Label(b, text="✎ rename  ·  ◷ shift (time in / out, break)  ·  ▦ today's times  ·  ✕ delete",
                 font=F.mono_s, fg=C.FAINT, bg=C.SURFACE, anchor="w").pack(fill=tk.X, pady=(px(6), 0))

        # default shift: what a newly added worker starts with
        self._label(b, "DEFAULT SHIFT  ·  FOR NEW WORKERS")
        read_default = self._shift_form(b, self._tc_default_shift())

        def save_default(apply_all=False):
            sh = read_default()
            if not sh:
                return
            self.tc_cfg.update(sh)
            if apply_all:
                if not messagebox.askyesno("Apply to all", "Give EVERY worker this shift?\n\n"
                                           "Their own shifts are replaced.", parent=dlg):
                    return
                for w in self.tc_cfg["workers"]:
                    self.tc_cfg["shifts"][w] = dict(sh)
            self._save()
            self._tc_shown.clear()
            self._tc_refresh()
            self._admin_draw_workers()
            self.log(f"Time clock: default shift {self._shift_text(sh)}"
                     + (" — applied to every worker" if apply_all else "") + " (admin)", "sys")
            self.toast.show("Applied to every worker" if apply_all else "Default shift saved", C.GREEN)
        self._pair(b, ("SAVE DEFAULT", "clock", "ghost", save_default, "Used for workers added from now on"),
                   ("APPLY TO ALL", "person", "ghost", lambda: save_default(True),
                    "Give every worker this shift")).pack_configure(pady=(px(8), 0))

        # password + records
        self._label(b, "SECURITY & RECORDS")
        self._pair(b, ("CHANGE PASSWORD", "lock", "ghost", self._admin_change_pw, "New admin password"),
                   ("OPEN RECORDS", "folder", "ghost", self.tc_open_folder,
                    "timesheet.csv (Excel) + proof screenshots"))
        tk.Label(b, text=f"records: {TIMECLOCK_DIR}", font=F.mono_s, fg=C.FAINT, bg=C.SURFACE, anchor="w",
                 wraplength=px(420), justify="left").pack(fill=tk.X, pady=(px(6), 0))
        RButton(b, "CLOSE", height=34, command=dlg.destroy).pack(fill=tk.X, pady=(px(14), 0))
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.update_idletasks()
        dlg.geometry(f"{max(dlg.winfo_reqwidth(), px(460))}x{dlg.winfo_reqheight()}")
        dlg.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dlg.winfo_width()) // 2
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - dlg.winfo_height()) // 3)
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        style_titlebar(dlg)
        dlg.focus_set()

    def _tc_default_shift(self):
        return {k: self.tc_cfg[k] for k in ("time_in", "time_out", "break_min")}

    def tc_shift(self, w):
        """The worker's own required shift: {"time_in", "time_out", "break_min"}."""
        sh = self.tc_cfg.setdefault("shifts", {})
        if w not in sh:
            sh[w] = self._tc_default_shift()
        return sh[w]

    def _shift_text(self, sh):
        h24 = self.vars["clock_24h"].get()
        return (f"{fmt_hhmm(sh['time_in'], h24)} – {fmt_hhmm(sh['time_out'], h24)}"
                f"  ·  break {fmt_mins(sh['break_min'])}")

    def _shift_form(self, parent, sh):
        """TIME IN / TIME OUT / BREAK entries. Returns read() → validated shift dict, or None (+ toast)."""
        grid = tk.Frame(parent, bg=parent.cget("bg"))
        grid.pack(fill=tk.X)
        h24 = self.vars["clock_24h"].get()
        ents = {}
        for i, (key, text, val) in enumerate((
                ("time_in", "TIME IN", fmt_hhmm(sh["time_in"], h24)),
                ("time_out", "TIME OUT", fmt_hhmm(sh["time_out"], h24)),
                ("break_min", "BREAK (MIN)", str(sh["break_min"])))):
            tk.Label(grid, text=text, font=F.tiny, fg=C.MUTED, bg=grid.cget("bg"), anchor="w").grid(
                row=0, column=i, sticky="w", padx=(0 if i == 0 else px(8), 0))
            e = REntry(grid, "")
            e.set(val)
            e.grid(row=1, column=i, sticky="ew", padx=(0 if i == 0 else px(8), 0), pady=(px(4), 0))
            grid.columnconfigure(i, weight=1)
            ents[key] = e
        tk.Label(parent, text="break minutes = break + lunch together (default 60)  ·  e.g. 8:00 AM, 17:30",
                 font=F.mono_s, fg=C.FAINT, bg=parent.cget("bg"), anchor="w").pack(fill=tk.X, pady=(px(6), 0))

        def read():
            t_in, t_out = parse_hhmm(ents["time_in"].get()), parse_hhmm(ents["time_out"].get())
            if not t_in or not t_out:
                self.toast.show("Times look wrong — use e.g. 8:00 AM or 17:30", C.AMBER)
                return None
            if t_in == t_out:
                self.toast.show("Time in and time out cannot be the same", C.AMBER)
                return None
            try:
                brk = int(ents["break_min"].get())
                assert 1 <= brk <= 600
            except (ValueError, AssertionError):
                self.toast.show("Break must be 1–600 minutes", C.AMBER)
                return None
            return {"time_in": t_in, "time_out": t_out, "break_min": brk}
        read.ents = ents
        return read

    def _tc_shift_dialog(self, worker, first=False):
        """Admin: the worker's own required time in / time out / break minutes."""
        parent = self.admin_win if (self.admin_win and self.admin_win.winfo_exists()) else self.root
        dlg = tk.Toplevel(parent)
        dlg.title(f"Shift — {worker}")
        dlg.configure(bg=C.BG)
        dlg.transient(parent)
        dlg.resizable(False, False)
        card = Card(dlg, title=f"{worker} · shift", pad=16)
        card.pack(fill=tk.BOTH, expand=True, padx=px(12), pady=px(12))
        b = card.body
        if first:
            tk.Label(b, text="New worker — set their required shift (or keep the default).", font=F.small,
                     fg=C.SOFT, bg=C.SURFACE, anchor="w").pack(fill=tk.X, pady=(0, px(8)))
        read = self._shift_form(b, self.tc_shift(worker))

        def save(_e=None):
            sh = read()
            if not sh:
                return
            self.tc_cfg["shifts"][worker] = sh
            self._save()
            self._tc_shown.clear()
            self._tc_refresh()
            self._admin_draw_workers()
            self.log(f"Time clock: {worker}'s shift set to {self._shift_text(sh)} (admin)", "sys")
            st = TimeClock.state(self.tc.current(worker)[1])
            self.toast.show(f"{worker}'s shift saved" + (" — applies from the next TIME IN"
                                                         if st in ("working", "break", "lunch") else ""), C.GREEN)
            dlg.destroy()
        row = tk.Frame(b, bg=C.SURFACE)
        row.pack(fill=tk.X, pady=(px(14), 0))
        RButton(row, "SAVE", variant="primary", height=34, width=90, command=save).pack(side=tk.RIGHT)
        RButton(row, "CANCEL", height=34, width=90, command=dlg.destroy).pack(side=tk.RIGHT, padx=(0, px(8)))
        for e in read.ents.values():
            e.entry.bind("<Return>", save)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.update_idletasks()
        dlg.geometry(f"{max(dlg.winfo_reqwidth(), px(420))}x{dlg.winfo_reqheight()}")
        dlg.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - dlg.winfo_width()) // 2
        y = parent.winfo_rooty() + px(60)
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        style_titlebar(dlg)
        read.ents["time_in"].entry.focus_set()
        dlg.grab_set()

    def _admin_draw_workers(self):
        box = getattr(self, "_admin_wlist", None)
        if box is None or not box.winfo_exists():
            return
        for ch in box.winfo_children():
            ch.destroy()
        for w in self.tc_cfg["workers"]:
            row = tk.Frame(box, bg=C.SURFACE2, highlightthickness=1, highlightbackground=C.BORDER)
            row.pack(fill=tk.X, pady=(0, px(4)))
            st = TimeClock.state(self.tc.current(w)[1])
            txt = tk.Frame(row, bg=C.SURFACE2)
            txt.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(px(10), 0), pady=px(4))
            tk.Label(txt, text=w, font=F.body, fg=C.TEXT, bg=C.SURFACE2, anchor="w").pack(fill=tk.X)
            tk.Label(txt, text=self._shift_text(self.tc_shift(w)), font=F.mono_s, fg=C.PURPLE_HI,
                     bg=C.SURFACE2, anchor="w").pack(fill=tk.X)
            tk.Label(row, text=self.TC_STATUS[st][1], font=F.tiny, fg=C.MUTED, bg=C.SURFACE2).pack(
                side=tk.LEFT, padx=px(6))
            for icon, tip, cmd, var in (
                    ("trash", "Delete worker (records are kept)", lambda w=w: self._admin_delete(w), "danger"),
                    ("calendar", "Edit today's times (in, out, breaks)", lambda w=w: self.tc_edit_dialog(w),
                     "ghost"),
                    ("clock", "Shift: required time in / out, break minutes", lambda w=w: self._tc_shift_dialog(w),
                     "ghost"),
                    ("edit", "Rename", lambda w=w: self._admin_rename(w), "ghost")):
                RButton(row, icon=icon, variant=var, height=28, radius=8, command=cmd, tooltip=tip).pack(
                    side=tk.RIGHT, padx=(0, px(4)), pady=px(4))
        try:
            dlg = self.admin_win
            dlg.update_idletasks()
            dlg.geometry(f"{max(dlg.winfo_reqwidth(), px(460))}x{dlg.winfo_reqheight()}")
        except (tk.TclError, AttributeError):
            pass

    def _admin_delete(self, w):
        if len(self.tc_cfg["workers"]) <= 1:
            self.toast.show("Keep at least one worker", C.AMBER)
            return
        if not messagebox.askyesno("Delete worker", f"Remove {w} from the time clock?\n\n"
                                   "Their saved times and proofs are kept.", parent=self.admin_win):
            return
        self.tc_cfg["workers"].remove(w)
        self.tc_cfg.get("shifts", {}).pop(w, None)
        if self.tc_cfg["active"] == w:
            self.tc_cfg["active"] = self.tc_cfg["workers"][0]
        self._save()
        self._tc_shown.clear()
        self._tc_refresh()
        self._admin_draw_workers()
        self.log(f"Time clock: worker removed — {w} (admin)", "sys")

    def _admin_rename(self, w):
        new = self.ask_string("Rename worker", f"New name for {w}", w)
        if new is None:
            return
        new = new.strip()[:40]
        if not new or new == w:
            return
        if new.lower() in (x.lower() for x in self.tc_cfg["workers"] if x != w):
            self.toast.show(f"{new} is already on the list", C.AMBER)
            return
        ws = self.tc_cfg["workers"]
        ws[ws.index(w)] = new
        self.tc_cfg["shifts"][new] = self.tc_cfg["shifts"].pop(w, None) or self._tc_default_shift()
        if self.tc_cfg["active"] == w:
            self.tc_cfg["active"] = new
        self.tc.rename(w, new)
        self._save()
        self._tc_shown.clear()
        self._tc_refresh()
        self._admin_draw_workers()
        self.log(f"Time clock: {w} renamed to {new} (admin)", "sys")

    def _admin_change_pw(self):
        p1 = self.ask_string("Change password", "New admin password (at least 4 characters)", secret=True)
        if p1 is None:
            return
        if len(p1) < 4:
            self.toast.show("Password must be at least 4 characters", C.AMBER)
            return
        p2 = self.ask_string("Change password", "Type the new password again", secret=True)
        if p2 is None:
            return
        if p1 != p2:
            self.toast.show("Passwords do not match — nothing changed", C.AMBER)
            return
        self.tc_cfg["pw"] = tc_hash(p1)
        self._save()
        try:
            self._admin_pw_warn.pack_forget()
        except (tk.TclError, AttributeError):
            pass
        self.log("Time clock: admin password changed", "sys")
        self.toast.show("Admin password changed", C.GREEN)

    def tc_edit_dialog(self, worker):
        """Admin: change every time of the worker's current / today's shift."""
        day, rec = self.tc.current(worker)
        rec = json.loads(json.dumps(rec)) if rec else None
        h24 = self.vars["clock_24h"].get()
        parent = self.admin_win if (self.admin_win and self.admin_win.winfo_exists()) else self.root
        dlg = tk.Toplevel(parent)
        dlg.title(f"Edit times — {worker}")
        dlg.configure(bg=C.BG)
        dlg.transient(parent)
        dlg.resizable(False, False)
        card = Card(dlg, title=f"{worker} · {day}", pad=16)
        card.pack(fill=tk.BOTH, expand=True, padx=px(12), pady=px(12))
        b = card.body
        tk.Label(b, text="times like 8:05 AM or 17:30  ·  leave TIME OUT empty = still on duty",
                 font=F.mono_s, fg=C.FAINT, bg=C.SURFACE, anchor="w").pack(fill=tk.X, pady=(0, px(8)))

        def show(iso):
            d = _dt(iso)
            return fmt_time(d, h24) if d else ""

        top = tk.Frame(b, bg=C.SURFACE)
        top.pack(fill=tk.X)
        e_in, e_out = REntry(top, ""), REntry(top, "")
        for i, (lbl, e, iso) in enumerate((("TIME IN", e_in, (rec or {}).get("in")),
                                           ("TIME OUT", e_out, (rec or {}).get("out")))):
            tk.Label(top, text=lbl, font=F.tiny, fg=C.MUTED, bg=C.SURFACE, anchor="w").grid(
                row=0, column=i, sticky="w", padx=(0 if i == 0 else px(8), 0))
            e.grid(row=1, column=i, sticky="ew", padx=(0 if i == 0 else px(8), 0), pady=(px(4), 0))
            top.columnconfigure(i, weight=1)
            e.set(show(iso))
            e.orig = (show(iso), iso)
        self._label(b, "BREAKS & LUNCH")
        blist = tk.Frame(b, bg=C.SURFACE)
        blist.pack(fill=tk.X)
        rows = []

        def add_row(kind="break", s_iso=None, e_iso=None):
            r = tk.Frame(blist, bg=C.SURFACE)
            r.pack(fill=tk.X, pady=(0, px(4)))
            kv = tk.StringVar(value=kind)
            Segmented(r, [("break", "BREAK"), ("lunch", "LUNCH")], kv, height=32, width=120).pack(side=tk.LEFT)
            es, ee = REntry(r, "start"), REntry(r, "end")
            es.set(show(s_iso))
            ee.set(show(e_iso))
            es.orig, ee.orig = (show(s_iso), s_iso), (show(e_iso), e_iso)
            item = {"frame": r, "kind": kv, "s": es, "e": ee}
            RButton(r, icon="trash", variant="danger", height=32, radius=8, tooltip="Remove",
                    command=lambda: (rows.remove(item), r.destroy(), fit())).pack(side=tk.RIGHT, padx=(px(6), 0))
            ee.pack(side=tk.RIGHT, padx=(px(6), 0))
            es.pack(side=tk.RIGHT, padx=(px(6), 0))
            es.config(width=px(110))
            ee.config(width=px(110))
            rows.append(item)
            fit()

        def fit():
            try:
                dlg.update_idletasks()
                dlg.geometry(f"{max(dlg.winfo_reqwidth(), px(480))}x{dlg.winfo_reqheight()}")
            except tk.TclError:
                pass

        RButton(b, "ADD BREAK / LUNCH", icon="plus", height=30, command=lambda: add_row()).pack(
            fill=tk.X, pady=(px(2), 0))

        def to_dt(e, base, required=False):
            """Entry → datetime on the shift (times before `base` roll over to the next day)."""
            txt = e.get().strip()
            if not txt:
                if required:
                    raise ValueError("a time is missing")
                return None
            if e.orig[1] and txt == e.orig[0]:
                return _dt(e.orig[1])  # unchanged: keep the exact original (seconds included)
            hhmm = parse_hhmm(txt)
            if not hhmm:
                raise ValueError(f"“{txt}” is not a time (use e.g. 8:05 AM or 17:30)")
            h, m = map(int, hhmm.split(":"))
            d = base.replace(hour=h, minute=m, second=0, microsecond=0)
            if d < base - _td(minutes=1) and base.hour * 60 + base.minute > 0:
                d += _td(days=1)
            return d

        def save():
            try:
                day0 = datetime.strptime(day, "%Y-%m-%d")
                if not e_in.get().strip():
                    if rows or e_out.get().strip():
                        raise ValueError("TIME IN is needed")
                    self.tc.set_record(day, worker, None)
                    done(f"{worker}'s times for {day} cleared")
                    return
                t_in = to_dt(e_in, day0, True)
                if t_in.date() != day0.date():
                    t_in = t_in.replace(year=day0.year, month=day0.month, day=day0.day)
                t_out = to_dt(e_out, t_in)
                brs = []
                for i, it in enumerate(rows):
                    s = to_dt(it["s"], t_in, True)
                    en = to_dt(it["e"], s)
                    if en is None and (t_out or i != len(rows) - 1):
                        raise ValueError("only the last break can be left open (no end), and only while on duty")
                    if t_out and (s > t_out or (en and en > t_out)):
                        raise ValueError("a break is after the time out")
                    brs.append({"kind": it["kind"].get(), "start": s.isoformat(),
                                "end": en.isoformat() if en else None})
                if t_out and t_out <= t_in:
                    raise ValueError("TIME OUT must be after TIME IN")
                brs.sort(key=lambda x: x["start"])
                ws = self.tc_shift(worker)
                new = rec or {"proofs": [], "req": {"in": ws["time_in"], "out": ws["time_out"],
                                                    "break": ws["break_min"]}}
                new.update({"in": t_in.isoformat(), "out": t_out.isoformat() if t_out else None, "breaks": brs})
                new.setdefault("edits", []).append(datetime.now().replace(microsecond=0).isoformat())
                self.tc.set_record(day, worker, new)
                done(f"{worker}'s times for {day} updated")
            except ValueError as ex:
                self.toast.show(f"Check the times: {ex}", C.AMBER)
            except Exception as ex:
                self.log(f"Time clock: edit failed ({ex})", "err")
                self.toast.show("Could not save — see the system log", C.RED)

        def done(msg):
            self.log(f"Time clock: {msg} (admin)", "sys")
            self.toast.show(msg, C.GREEN)
            self._tc_shown.clear()
            self._tc_refresh()
            self._admin_draw_workers()
            dlg.destroy()

        def undo():
            label = self.tc.undo_last(worker)
            if label:
                done(f"{worker}: last punch ({label}) undone")
            else:
                self.toast.show(f"{worker} has no punch to undo", C.AMBER)

        for br in (rec or {}).get("breaks", []):
            add_row(br.get("kind", "break"), br.get("start"), br.get("end"))
        btns = tk.Frame(b, bg=C.SURFACE)
        btns.pack(fill=tk.X, pady=(px(14), 0))
        RButton(btns, "SAVE", variant="primary", height=34, width=80, command=save).pack(side=tk.RIGHT)
        RButton(btns, "CANCEL", height=34, width=80, command=dlg.destroy).pack(side=tk.RIGHT, padx=(0, px(6)))
        RButton(btns, "UNDO LAST PUNCH", icon="back", height=34, command=undo,
                tooltip="Removes the most recent TIME IN / BREAK / LUNCH / TIME OUT").pack(side=tk.LEFT)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        fit()
        dlg.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - dlg.winfo_width()) // 2
        y = parent.winfo_rooty() + px(40)
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        style_titlebar(dlg)
        dlg.grab_set()

    # ───────────────────────── settings ─────────────────────────
    def open_settings(self):
        if self.settings_win is not None and self.settings_win.winfo_exists():
            self.settings_win.deiconify()
            self.settings_win.lift()
            self.settings_win.focus_set()
            return
        dlg = tk.Toplevel(self.root)
        self.settings_win = dlg
        dlg.title("Settings")
        dlg.configure(bg=C.BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        card = Card(dlg, title="Settings", pad=16)
        card.pack(fill=tk.BOTH, expand=True, padx=px(12), pady=px(12))
        b = card.body

        # accent colour
        self._label(b, "SOFTWARE COLOUR", top=0)
        sw = tk.Frame(b, bg=C.SURFACE)
        sw.pack(fill=tk.X)
        self._swatches = []
        size = px(30)
        for i, (name, hexc) in enumerate(THEME_PRESETS):
            cv = tk.Canvas(sw, width=size, height=size, bg=C.SURFACE, highlightthickness=0, cursor="hand2")
            cv.grid(row=i // 4, column=i % 4, padx=(0, px(10)), pady=(0, px(8)), sticky="w")
            cv.hexc = hexc
            cv.bind("<Button-1>", lambda e, h=hexc: self.set_accent(h))
            Tooltip(cv, name)
            self._swatches.append(cv)
        row = tk.Frame(b, bg=C.SURFACE)
        row.pack(fill=tk.X, pady=(px(2), 0))
        self._accent_lbl = tk.Label(row, text="", font=F.mono_s, fg=C.MUTED, bg=C.SURFACE, anchor="w")
        self._accent_lbl.pack(side=tk.LEFT)
        RButton(row, "RESET", height=28, command=lambda: self.set_accent(DEFAULT_ACCENT),
                tooltip="Back to the default violet").pack(side=tk.RIGHT)
        RButton(row, "CUSTOM…", icon="picture", height=28, command=self._pick_accent,
                tooltip="Pick any colour").pack(side=tk.RIGHT, padx=(0, px(6)))
        self._draw_swatches()

        # system log
        self._label(b, "SYSTEM LOG")
        Toggle(b, "Show system log", self.vars["log_visible"], sub="log panel at the bottom of the window").pack(
            fill=tk.X)

        # clock
        self._label(b, "CLOCK")
        Toggle(b, "24-hour clock", self.vars["clock_24h"], sub="off = 12-hour with AM / PM").pack(fill=tk.X)

        # time clock admin
        self._label(b, "ADMIN")
        RButton(b, "ADMIN OPTIONS", icon="lock", height=34, command=self.open_admin,
                tooltip="Time clock: required time in / out, break minutes, workers, password").pack(fill=tk.X)
        tk.Label(b, text="password protected · shift, break, workers", font=F.mono_s, fg=C.FAINT,
                 bg=C.SURFACE, anchor="w").pack(fill=tk.X, pady=(px(4), 0))

        RButton(b, "DONE", variant="primary", height=34, command=dlg.destroy).pack(fill=tk.X, pady=(px(16), 0))
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.update_idletasks()
        dlg.geometry(f"{max(dlg.winfo_reqwidth(), px(360))}x{dlg.winfo_reqheight()}")
        dlg.update_idletasks()
        x = self.root.winfo_rootx() + self.root.winfo_width() - dlg.winfo_width() - px(24)
        y = self.root.winfo_rooty() + self.header.winfo_height() + px(4)
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        style_titlebar(dlg)
        dlg.focus_set()

    def _draw_swatches(self):
        cur = self.vars["accent"].get().lower()
        for cv in getattr(self, "_swatches", []):
            try:
                cv.delete("all")
                n = int(cv["width"])
                sel = cv.hexc.lower() == cur
                if sel:
                    cv.create_oval(1, 1, n - 2, n - 2, outline=C.TEXT, width=2)
                m = px(5) if sel else px(3)
                cv.create_oval(m, m, n - m - 1, n - m - 1, fill=cv.hexc, outline="")
            except tk.TclError:
                pass
        if getattr(self, "_accent_lbl", None) is not None:
            try:
                name = next((nm for nm, h in THEME_PRESETS if h == cur), "Custom")
                self._accent_lbl.config(text=f"{name.upper()}  {cur}")
            except tk.TclError:
                pass

    def _pick_accent(self):
        res = colorchooser.askcolor(color=self.vars["accent"].get(), title="Software colour",
                                    parent=self.settings_win or self.root)
        if res and res[1]:
            self.set_accent(res[1])

    def set_accent(self, hexc):
        if not valid_hex(hexc):
            return
        hexc = hexc.lower()
        old = {k: getattr(C, k) for k in _BASE_THEME}
        new = palette_for(hexc)
        apply_palette(new)
        cmap = {}
        for k, v in old.items():
            if v.lower() != new[k].lower():
                cmap.setdefault(v.lower(), new[k])
        self.vars["accent"].set(hexc)
        self._recolor(self.root, cmap)
        self._retag_log()
        for w in [self.root] + [t for t in self.root.winfo_children() if isinstance(t, tk.Toplevel)]:
            style_titlebar(w)
        try:
            self._icon = make_icon(self.root)
            self.root.iconphoto(True, self._icon)
        except tk.TclError:
            pass
        self._draw_swatches()
        self.header.set_time(datetime.now(), self.vars["clock_24h"].get())

    COLOR_OPTS = ("bg", "fg", "highlightbackground", "highlightcolor", "insertbackground",
                  "selectbackground", "selectforeground", "activebackground", "activeforeground",
                  "disabledforeground", "troughcolor")

    def _recolor(self, w, cmap):
        """Swap old palette colours for new ones on every widget, canvas item and stored colour."""
        for opt in self.COLOR_OPTS:
            try:
                v = str(w.cget(opt)).lower()
            except (tk.TclError, ValueError):
                continue
            if v in cmap:
                try:
                    w.config(**{opt: cmap[v]})
                except tk.TclError:
                    pass
        for attr in ("bgc", "fill", "border", "color"):
            v = getattr(w, attr, None)
            if isinstance(v, str) and v.lower() in cmap:
                setattr(w, attr, cmap[v.lower()])
        if isinstance(w, tk.Canvas):
            for item in w.find_all():
                for opt in ("fill", "outline"):
                    try:
                        v = str(w.itemcget(item, opt)).lower()
                    except tk.TclError:
                        continue
                    if v in cmap:
                        w.itemconfig(item, **{opt: cmap[v]})
        for child in w.winfo_children():
            self._recolor(child, cmap)
        if isinstance(w, Header):
            w.logo.delete("all")
            w._draw_logo()
        elif hasattr(w, "_draw") and callable(w._draw):
            try:
                w._draw()
            except Exception:
                pass

    def _retag_log(self):
        t = self.log_text
        for tag, col in (("time", C.FAINT), ("dev", C.PURPLE_HI), ("info", C.SOFT), ("sys", C.PURPLE),
                         ("ok", C.GREEN), ("warn", C.AMBER), ("err", C.RED)):
            t.tag_config(tag, foreground=col)

    def _apply_log_visibility(self):
        show = self.vars["log_visible"].get()
        if show and not self.log_card.winfo_manager():
            self.log_card.pack(side=tk.BOTTOM, fill=tk.X, padx=px(16), pady=(0, px(12)), before=self.main)
        elif not show and self.log_card.winfo_manager():
            self.log_card.pack_forget()

    # ───────────────────────── auto-reconnect ─────────────────────────
    def on_link_lost(self, panel):
        if not self.vars["auto_reconnect"].get():
            return
        panel.retries = getattr(panel, "retries", 0) + 1
        if panel.retries > 30:
            self.log(f"[{panel.serial}] auto-reconnect gave up", "warn")
            return

        def attempt():
            if panel._closed or panel.active or self.panels.get(panel.serial) is not panel:
                return
            if self.known.get(panel.serial) == "device":
                self.log(f"[{panel.serial}] auto-reconnect (try {panel.retries})", "sys")
                panel.restart()
            else:
                self.on_link_lost(panel)  # device not back yet: keep waiting
        self.root.after(4000, attempt)

    # ───────────────────────── shutdown ─────────────────────────
    def on_close(self):
        self._save()
        eng, alive = _FBD["mod"], self._fb_alive()
        if eng is not None and alive:
            eng.STOP.set()
            for s in alive:  # close FB Lite so no phone is left running the activity
                try:
                    run_adb(s, "shell", f"am force-stop {FB_PACKAGE}", timeout=5)
                except Exception:
                    pass
        for panel in list(self.panels.values()):
            panel.close(sync=True)
        if self.tether_mgr.ready():
            for s in list(self.tethered):
                try:
                    self.tether_mgr.stop(s)
                except Exception:
                    pass
        self.tether_mgr.shutdown()
        self.root.destroy()


def main():
    global SCALE
    if "--fb-selftest" in sys.argv:
        fb_selftest()
        return
    if IS_WIN:
        try:  # own taskbar identity, so the installed exe shows its icon instead of a generic Python one
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Scientist.ADBMultiMirror")
        except Exception:
            pass
    root = tk.Tk()
    SCALE = max(1.0, root.winfo_fpixels("1i") / 96.0)
    apply_palette(palette_for(load_settings().get("accent", DEFAULT_ACCENT)))
    root.configure(bg=C.BG)
    init_fonts(root)
    AdbScrcpyApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
