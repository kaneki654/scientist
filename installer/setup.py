#!/usr/bin/env python3
"""
Scientist-SETUP  —  installer / updater / uninstaller for SCIENTIST.

Built into a single Scientist-SETUP.exe by installer/build.py. The exe carries payload.zip:
    Scientist.exe + _internal/      the app (Python, Tk and Pillow built in)
    tools/scrcpy/                   scrcpy + adb (mirroring engine)
    licenses/                       third-party licenses

Per-user install (no admin rights): %LOCALAPPDATA%\\Programs\\Scientist by default.
    Scientist-SETUP.exe                       install or update (wizard)
    Scientist-SETUP.exe --silent [--dir D] [--no-shortcuts]
                                              install without UI (log: %TEMP%\\Scientist-setup.log)
    uninstall.exe --uninstall                 remove (registered in Windows "Installed apps")
"""

import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import winreg
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import Scientist as S  # noqa: E402  (theme + widgets: the installer looks like the app)
from Scientist import C, F, px, Card, Chip, REntry, RButton, Toggle, NeonLine, rrect, mix  # noqa: E402

APP_NAME = "Scientist"
APP_EXE = "Scientist.exe"
APP_ID = "Scientist"
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}"
DEFAULT_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Programs" / "Scientist"
NO_WINDOW = 0x08000000
KEEP_ON_UNINSTALL = ("screenshots", "wallpapers")
KEEP_ON_UPDATE = {"fbdaily/config.json"}      # user-edited files an update must not overwrite


def resource(name):
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / name
    return Path(__file__).resolve().parent / "build" / name


PAYLOAD = resource("payload.zip")


def payload_manifest():
    try:
        with zipfile.ZipFile(PAYLOAD) as z:
            return json.loads(z.read("manifest.json"))
    except Exception:
        return {"version": S.APP_VERSION, "size": 0}


def component_of(member):
    parts = member.replace("\\", "/").split("/")
    if parts[0] == "tools" and len(parts) > 1:
        return parts[1]          # "scrcpy" / "helper"
    return "core"


# ════════════════════════════════════════════════════════════════════
#  Windows helpers
# ════════════════════════════════════════════════════════════════════
def shell_folder(csidl):
    buf = ctypes.create_unicode_buffer(520)
    ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    return Path(buf.value)


def desktop_dir():
    return shell_folder(0x10)          # CSIDL_DESKTOPDIRECTORY (follows OneDrive redirection)


def start_menu_dir():
    return shell_folder(0x02)          # CSIDL_PROGRAMS (per-user Start Menu\Programs)


def powershell(script, env=None, timeout=60):
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-Command", script], capture_output=True, text=True, timeout=timeout,
                          env=dict(os.environ, **(env or {})), creationflags=NO_WINDOW)


def make_shortcut(lnk, target, icon=None, desc=""):
    lnk.parent.mkdir(parents=True, exist_ok=True)
    r = powershell("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:SCI_LNK);"
                   "$s.TargetPath=$env:SCI_TARGET;$s.WorkingDirectory=$env:SCI_WD;"
                   "$s.IconLocation=$env:SCI_ICON;$s.Description=$env:SCI_DESC;$s.Save()",
                   env={"SCI_LNK": str(lnk), "SCI_TARGET": str(target), "SCI_WD": str(Path(target).parent),
                        "SCI_ICON": f"{icon or target},0", "SCI_DESC": desc})
    if r.returncode != 0:
        raise RuntimeError(f"shortcut {lnk.name}: {r.stderr.strip()}")


def running_from(folder):
    """Processes (Scientist / scrcpy / adb / old gnirehtet) whose exe lives inside `folder`."""
    r = powershell("Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and "
                   "$_.Path.StartsWith($env:SCI_DIR, [StringComparison]::OrdinalIgnoreCase) } | "
                   "ForEach-Object { \"$($_.Id)|$($_.ProcessName)\" }",
                   env={"SCI_DIR": str(folder).rstrip("\\") + "\\"})
    out = []
    for line in r.stdout.splitlines():
        pid, _, name = line.strip().partition("|")
        if pid.isdigit() and int(pid) != os.getpid():
            out.append((int(pid), name))
    return out


def stop_processes(procs):
    for pid, _ in procs:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, creationflags=NO_WINDOW)
    time.sleep(0.8)


def registry_entry():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            loc, _ = winreg.QueryValueEx(k, "InstallLocation")
            ver, _ = winreg.QueryValueEx(k, "DisplayVersion")
            return {"dir": loc, "version": ver}
    except OSError:
        return None


def write_registry(target, version, size_kb):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
        exe = str(target / APP_EXE)
        for name, val in (("DisplayName", "Scientist — ADB Multi-Mirror"), ("DisplayVersion", version),
                          ("Publisher", "Scientist"), ("DisplayIcon", f"{exe},0"),
                          ("InstallLocation", str(target)),
                          ("UninstallString", f'"{target / "uninstall.exe"}" --uninstall'),
                          ("QuietUninstallString", f'"{target / "uninstall.exe"}" --uninstall --silent')):
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
        for name, val in (("NoModify", 1), ("NoRepair", 1), ("EstimatedSize", int(size_kb))):
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, val)


def delete_registry():
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass


def rmtree_retry(path, keep=()):
    path = Path(path)
    if not path.exists():
        return []
    left = []
    for child in path.iterdir():
        if child.name.lower() in keep:
            continue
        for attempt in range(5):
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                break
            except OSError:
                time.sleep(0.5)
        else:
            left.append(child)
    try:
        if not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass
    return left


# ════════════════════════════════════════════════════════════════════
#  Install / uninstall jobs (run in a worker thread, report through `report`)
# ════════════════════════════════════════════════════════════════════
def do_install(target, opts, report):
    target = Path(target)
    manifest = payload_manifest()
    version = manifest.get("version", S.APP_VERSION)
    report(0.02, f"Installing SCIENTIST {version} → {target}")

    procs = running_from(target)
    if procs:
        report(0.04, "Stopping running copies: " + ", ".join(sorted({n for _, n in procs})))
        stop_processes(procs)

    target.mkdir(parents=True, exist_ok=True)
    stale = target / "_internal"
    if stale.exists():
        report(0.06, "Removing the previous version's runtime")
        shutil.rmtree(stale, ignore_errors=True)
    for comp in ("scrcpy", "gnirehtet"):   # gnirehtet: left over from older versions, no longer used
        if (opts.get(comp) or comp == "gnirehtet") and (target / "tools" / comp).exists():
            shutil.rmtree(target / "tools" / comp, ignore_errors=True)

    with zipfile.ZipFile(PAYLOAD) as z:
        members = [m for m in z.infolist() if m.filename != "manifest.json"
                   and opts.get(component_of(m.filename), True)]
        total = sum(m.file_size for m in members) or 1
        done = 0
        last = None
        for m in members:
            comp = component_of(m.filename)
            if comp != last:
                label = {"core": "SCIENTIST app (Python, Tk, Pillow, FB Daily, reverse tethering built in)",
                         "scrcpy": "scrcpy + ADB", "helper": "wallpaper helper",
                         "tether": "reverse-tether client"}.get(comp, comp)
                report(None, f"Copying {label}")
                last = comp
            dest = (target / m.filename).resolve()
            if not str(dest).startswith(str(target.resolve())):
                continue  # never write outside the install folder
            if m.filename.replace("\\", "/") in KEEP_ON_UPDATE and dest.exists():
                done += m.file_size   # the user's tuning (e.g. an AI key) survives updates;
                continue              # new options still apply via the engine's defaults
            if m.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(m) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
            done += m.file_size
            report(0.08 + 0.77 * done / total, None)

    report(0.87, "Registering the uninstaller")
    me = Path(sys.executable)
    # the payload ships a small uninstall.exe; fall back to copying this setup exe if it's missing
    if not (target / "uninstall.exe").exists() and getattr(sys, "frozen", False):
        shutil.copy2(me, target / "uninstall.exe")
    exe = target / APP_EXE
    if opts.get("desktop"):
        report(0.9, "Creating the desktop shortcut")
        make_shortcut(desktop_dir() / "Scientist.lnk", exe, desc="ADB multi-mirror & phone farm control")
    if opts.get("startmenu"):
        report(0.93, "Creating the Start menu shortcuts")
        folder = start_menu_dir() / "Scientist"
        make_shortcut(folder / "Scientist.lnk", exe, desc="ADB multi-mirror & phone farm control")
        if (target / "uninstall.exe").exists():
            make_shortcut(folder / "Uninstall Scientist.lnk", target / "uninstall.exe", icon=exe,
                          desc="Remove Scientist")
    size_kb = sum(f.stat().st_size for f in target.rglob("*") if f.is_file()) // 1024
    write_registry(target, version, size_kb)
    report(1.0, f"Done — {size_kb / 1024:.0f} MB installed")
    return target


def do_uninstall(target, opts, report):
    target = Path(target)
    report(0.05, f"Removing SCIENTIST from {target}")
    procs = running_from(target)
    if procs:
        report(0.1, "Stopping running copies: " + ", ".join(sorted({n for _, n in procs})))
        stop_processes(procs)
    report(0.3, "Removing shortcuts")
    for lnk in (desktop_dir() / "Scientist.lnk",):
        lnk.unlink(missing_ok=True)
    shutil.rmtree(start_menu_dir() / "Scientist", ignore_errors=True)
    report(0.45, "Removing program files")
    keep = KEEP_ON_UNINSTALL if opts.get("keep_media") else ()
    left = rmtree_retry(target, keep=keep)
    if opts.get("remove_settings"):
        report(0.85, "Removing settings and unit numbers")
        (Path.home() / ".scientist_mirror.json").unlink(missing_ok=True)
    delete_registry()
    if left:
        report(1.0, f"Done — {len(left)} item(s) were in use and could not be removed")
    else:
        report(1.0, "Done — SCIENTIST was removed" + (" (screenshots & wallpapers kept)" if keep else ""))


# ════════════════════════════════════════════════════════════════════
#  Wizard UI
# ════════════════════════════════════════════════════════════════════
class ProgressBar(tk.Canvas):
    def __init__(self, parent, height=14):
        super().__init__(parent, height=px(height), bg=parent.cget("bg"), highlightthickness=0)
        self.value = 0.0
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, v):
        self.value = max(0.0, min(1.0, v))
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10:
            return
        rrect(self, 1, 1, w - 2, h - 2, (h - 2) / 2, fill=C.BG, outline=C.BORDER)
        fw = max(h, (w - 4) * self.value)
        if self.value > 0:
            rrect(self, 2, 2, 2 + fw, h - 3, (h - 4) / 2, fill=C.PURPLE, outline="")
            self.create_rectangle(2 + fw * 0.15, 3, 2 + fw * 0.85, 4, fill=C.PURPLE_HI, outline="")


class SetupWizard:
    def __init__(self, root, mode="install", target=None):
        self.root = root
        self.mode = mode
        self.q = queue.Queue()
        self.manifest = payload_manifest()
        self.existing = registry_entry()
        self.target_dir = Path(target) if target else (
            Path(self.existing["dir"]) if self.existing else DEFAULT_DIR)
        self.result_dir = None

        root.title("Scientist Setup" if mode == "install" else "Uninstall Scientist")
        root.configure(bg=C.BG)
        root.resizable(False, False)
        self._icon = S.make_icon(root)
        root.iconphoto(True, self._icon)
        w, h = px(760), px(640)
        root.geometry(f"{w}x{h}+{(root.winfo_screenwidth() - w) // 2}+{max(0, (root.winfo_screenheight() - h) // 2 - 20)}")
        S.style_titlebar(root)

        self._header()
        self.footer = tk.Frame(root, bg=C.BG2)
        self.footer.pack(side=tk.BOTTOM, fill=tk.X)
        tk.Frame(self.footer, bg=C.BORDER, height=1).pack(fill=tk.X)
        self.foot_row = tk.Frame(self.footer, bg=C.BG2)
        self.foot_row.pack(fill=tk.X, padx=px(20), pady=px(12))
        self.body = tk.Frame(root, bg=C.BG)
        self.body.pack(fill=tk.BOTH, expand=True, padx=px(20), pady=px(16))
        root.protocol("WM_DELETE_WINDOW", self._close)
        self._busy = False
        (self._page_options if mode == "install" else self._page_uninstall)()
        self._pump()

    # ----- chrome -----
    def _header(self):
        top = tk.Frame(self.root, bg=C.BG)
        top.pack(fill=tk.X, padx=px(20), pady=(px(14), px(10)))
        logo = tk.Canvas(top, width=px(520), height=px(56), bg=C.BG, highlightthickness=0)
        logo.pack(side=tk.LEFT)
        import math
        cx, cy, r = px(26), px(28), px(19)
        outer, inner = [], []
        for i in range(6):
            a = math.radians(60 * i - 90)
            outer += [cx + r * math.cos(a), cy + r * math.sin(a)]
            inner += [cx + r * .42 * math.cos(a), cy + r * .42 * math.sin(a)]
        logo.create_polygon(outer, outline=C.PURPLE, fill=C.BG2, width=2)
        logo.create_polygon(inner, outline="", fill=C.PURPLE)
        x = px(60)
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
            logo.create_text(x + dx, px(21) + dy, text="SCIENTIST", font=F.title, fill=C.PURPLE_DIM, anchor="w")
        logo.create_text(x, px(21), text="SCIENTIST", font=F.title, fill=C.PURPLE_HI, anchor="w")
        tw = F.title.measure("SCIENTIST")
        word = "SETUP" if self.mode == "install" else "UNINSTALL"
        cw = F.h.measure(word) + px(22)
        rrect(logo, x + tw + px(12), px(10), x + tw + px(12) + cw, px(32), px(11), fill=C.PURPLE, outline="")
        logo.create_text(x + tw + px(12) + cw / 2, px(21), text=word, font=F.h, fill=C.INK)
        logo.create_text(x + 1, px(45), text=f"ADB MULTI-MIRROR  //  v{self.manifest.get('version', S.APP_VERSION)}",
                         font=F.mono, fill=C.MUTED, anchor="w")
        NeonLine(self.root, height=2).pack(fill=tk.X)

    def _clear(self):
        for w in self.body.winfo_children():
            w.destroy()
        for w in self.foot_row.winfo_children():
            w.destroy()

    def _foot_note(self, text):
        tk.Label(self.foot_row, text=text, font=F.mono_s, fg=C.FAINT, bg=C.BG2).pack(side=tk.LEFT)

    # ----- page: options -----
    def _page_options(self):
        self._clear()
        size_mb = self.manifest.get("size", 0) / 1024 / 1024
        loc = Card(self.body, title="Install location", pad=14)
        loc.pack(fill=tk.X, pady=(0, px(12)))
        if size_mb:
            Chip(loc.header_right, f"{size_mb:.0f} MB", C.PURPLE_HI, dot=False).pack(side=tk.RIGHT)
        if self.existing:
            Chip(loc.header_right, f"UPDATE {self.existing['version']} → {self.manifest.get('version')}",
                 C.AMBER, dot=False).pack(side=tk.RIGHT, padx=(0, px(6)))
        row = tk.Frame(loc.body, bg=C.SURFACE)
        row.pack(fill=tk.X)
        self.dir_entry = REntry(row, str(DEFAULT_DIR))
        self.dir_entry.set(str(self.target_dir))
        RButton(row, "BROWSE", icon="folder", height=34, command=self._browse).pack(side=tk.RIGHT, padx=(px(8), 0))
        self.dir_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        note = ("updating keeps your settings and unit numbers" if self.existing else
                "installs for your user only — no administrator rights needed")
        tk.Label(loc.body, text=note, font=F.mono_s, fg=C.FAINT, bg=C.SURFACE, anchor="w").pack(
            fill=tk.X, pady=(px(8), 0))

        comp = Card(self.body, title="Components", pad=14)
        comp.pack(fill=tk.X, pady=(0, px(12)))
        req = tk.Frame(comp.body, bg=C.SURFACE)
        req.pack(fill=tk.X, pady=(0, px(8)))
        Chip(req, "REQUIRED", C.GREEN, dot=False).pack(side=tk.RIGHT)
        tk.Label(req, text="SCIENTIST app", font=F.body, fg=C.TEXT, bg=C.SURFACE, anchor="w").pack(fill=tk.X)
        tk.Label(req, text="Python, Tk, Pillow, wallpaper helper and reverse tethering built in", font=F.mono_s,
                 fg=C.MUTED, bg=C.SURFACE, anchor="w").pack(fill=tk.X)
        self.v_scrcpy = tk.BooleanVar(value=True)
        Toggle(comp.body, "scrcpy 4.1 + ADB", self.v_scrcpy,
               sub="mirroring engine — untick only if both are already in your PATH").pack(fill=tk.X)

        sc = Card(self.body, title="Shortcuts", pad=14)
        sc.pack(fill=tk.X)
        self.v_desktop = tk.BooleanVar(value=True)
        self.v_start = tk.BooleanVar(value=True)
        self.v_launch = tk.BooleanVar(value=True)
        grid = tk.Frame(sc.body, bg=C.SURFACE)
        grid.pack(fill=tk.X)
        for i, (text, var) in enumerate((("Desktop", self.v_desktop), ("Start menu", self.v_start),
                                         ("Launch when done", self.v_launch))):
            Toggle(grid, text, var).grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else px(18), 0))
            grid.grid_columnconfigure(i, weight=1)

        self._foot_note("phones need USB debugging enabled  ·  drivers: Windows Update / OEM")
        RButton(self.foot_row, "UPDATE" if self.existing else "INSTALL", icon="download", variant="primary",
                height=38, radius=12, command=self._start_install).pack(side=tk.RIGHT)
        RButton(self.foot_row, "CANCEL", height=38, radius=12, command=self._close).pack(side=tk.RIGHT,
                                                                                        padx=(0, px(10)))

    def _browse(self):
        d = filedialog.askdirectory(parent=self.root, title="Install SCIENTIST into…",
                                    initialdir=str(Path(self.dir_entry.get() or DEFAULT_DIR).parent))
        if d:
            d = Path(d)
            if d.name.lower() != "scientist":
                d = d / "Scientist"
            self.dir_entry.set(str(d))

    # ----- page: progress -----
    def _page_progress(self, title):
        self._clear()
        card = Card(self.body, title=title, pad=16)
        card.pack(fill=tk.BOTH, expand=True)
        self.status = tk.Label(card.body, text="Preparing…", font=F.h2, fg=C.TEXT, bg=C.SURFACE, anchor="w")
        self.status.pack(fill=tk.X)
        self.bar = ProgressBar(card.body)
        self.bar.pack(fill=tk.X, pady=(px(12), px(6)))
        self.pct = tk.Label(card.body, text="0 %", font=F.mono_b, fg=C.PURPLE_HI, bg=C.SURFACE, anchor="e")
        self.pct.pack(fill=tk.X)
        holder = tk.Frame(card.body, bg=C.BG)
        holder.pack(fill=tk.BOTH, expand=True, pady=(px(10), 0))
        self.logbox = tk.Text(holder, height=10, bg=C.BG, fg=C.SOFT, font=F.mono, bd=0, relief="flat",
                              highlightthickness=0, padx=px(10), pady=px(8), state="disabled", wrap="word")
        self.logbox.pack(fill=tk.BOTH, expand=True)
        self.logbox.tag_config("ok", foreground=C.GREEN)
        self.logbox.tag_config("err", foreground=C.RED)
        self._foot_note("please keep this window open")

    def _log(self, text, tag=None):
        self.logbox.config(state="normal")
        self.logbox.insert(tk.END, "▸ " + text + "\n", tag)
        self.logbox.see(tk.END)
        self.logbox.config(state="disabled")

    def _report(self, frac, text):
        self.q.put((frac, text))

    def _pump(self):
        try:
            while True:
                item = self.q.get_nowait()
                if callable(item):
                    item()
                    continue
                frac, text = item
                if frac is not None:
                    self.bar.set(frac)
                    self.pct.config(text=f"{frac * 100:.0f} %")
                if text:
                    self.status.config(text=text)
                    self._log(text, "ok" if text.startswith("Done") else None)
        except queue.Empty:
            pass
        self.root.after(40, self._pump)

    # ----- install flow -----
    def _start_install(self):
        target = Path(self.dir_entry.get() or DEFAULT_DIR)
        if not PAYLOAD.exists():
            messagebox.showerror("Setup", "The installer payload is missing.", parent=self.root)
            return
        if target.exists() and any(target.iterdir()) and not (target / APP_EXE).exists() \
                and target != DEFAULT_DIR and target.name.lower() != "scientist":
            if not messagebox.askyesno("Setup", f"{target} is not empty. Install into it anyway?", parent=self.root):
                return
        procs = running_from(target) if target.exists() else []
        if procs and not messagebox.askyesno(
                "Setup", "SCIENTIST (or its mirrors) is running from this folder.\n"
                         "Close it now and continue? Open mirrors will stop.", parent=self.root):
            return
        opts = {"core": True, "helper": True, "scrcpy": self.v_scrcpy.get(),
                "desktop": self.v_desktop.get(), "startmenu": self.v_start.get()}
        launch = self.v_launch.get()
        self._busy = True
        self._page_progress("Installing")

        def work():
            try:
                self.result_dir = do_install(target, opts, self._report)
                self.q.put(lambda: self._page_done(True, launch))
            except Exception as e:
                self._report(None, f"Install failed: {e}")
                self.q.put(lambda: self._page_done(False, False, str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _page_done(self, ok, launch, error=""):
        self._busy = False
        self._clear()
        card = Card(self.body, pad=24)
        card.pack(fill=tk.BOTH, expand=True)
        c = tk.Canvas(card.body, width=px(120), height=px(120), bg=C.SURFACE, highlightthickness=0)
        c.pack(pady=(px(20), px(10)))
        import math
        pts = []
        for i in range(6):
            a = math.radians(60 * i - 90)
            pts += [px(60) + px(52) * math.cos(a), px(60) + px(52) * math.sin(a)]
        col = C.PURPLE if ok else C.RED
        c.create_polygon(pts, outline=col, fill=mix(C.SURFACE, col, 0.12), width=3)
        if ok:
            c.create_line(px(38), px(62), px(54), px(78), px(84), px(46), fill=C.PURPLE_HI, width=px(6),
                          capstyle="round", joinstyle="round")
        else:
            c.create_text(px(60), px(60), text="!", font=F.title, fill=C.RED)
        title = ("INSTALLED" if self.mode == "install" else "UNINSTALLED") if ok else "SOMETHING WENT WRONG"
        tk.Label(card.body, text=title, font=F.big, fg=C.TEXT, bg=C.SURFACE).pack()
        if ok and self.mode == "install":
            sub = f"SCIENTIST {self.manifest.get('version')} is ready in\n{self.result_dir}"
        elif ok:
            sub = "SCIENTIST has been removed from this PC."
        else:
            sub = error or "See the details above."
        tk.Label(card.body, text=sub, font=F.body, fg=C.MUTED, bg=C.SURFACE, justify="center",
                 wraplength=px(620)).pack(pady=(px(8), 0))
        if ok and self.mode == "install":
            tips = ("Next: enable USB debugging on each phone (Settings → Developer options),\n"
                    "plug them in and accept the RSA prompt. Then hit START MIRROR.")
            tk.Label(card.body, text=tips, font=F.mono_s, fg=C.FAINT, bg=C.SURFACE,
                     justify="center").pack(pady=(px(18), 0))
            RButton(self.foot_row, "LAUNCH SCIENTIST", icon="play", variant="primary", height=38, radius=12,
                    command=self._launch_and_close).pack(side=tk.RIGHT)
            RButton(self.foot_row, "OPEN FOLDER", icon="folder", height=38, radius=12,
                    command=lambda: os.startfile(self.result_dir)).pack(side=tk.RIGHT, padx=(0, px(10)))
            RButton(self.foot_row, "CLOSE", height=38, radius=12, command=self._close).pack(side=tk.RIGHT,
                                                                                           padx=(0, px(10)))
            if launch:
                self.root.after(600, self._launch_and_close)
        else:
            RButton(self.foot_row, "CLOSE", variant="primary", height=38, radius=12,
                    command=self._close).pack(side=tk.RIGHT)

    def _launch_and_close(self):
        exe = Path(self.result_dir) / APP_EXE
        if exe.exists():
            subprocess.Popen([str(exe)], cwd=str(exe.parent), close_fds=True)
        self._close(force=True)

    # ----- uninstall flow -----
    def _page_uninstall(self):
        self._clear()
        card = Card(self.body, title="Uninstall", pad=16)
        card.pack(fill=tk.X)
        ver = self.existing["version"] if self.existing else "?"
        tk.Label(card.body, text=f"Remove SCIENTIST {ver} from:", font=F.body, fg=C.SOFT, bg=C.SURFACE,
                 anchor="w").pack(fill=tk.X)
        tk.Label(card.body, text=str(self.target_dir), font=F.mono_b, fg=C.PURPLE_HI, bg=C.SURFACE,
                 anchor="w").pack(fill=tk.X, pady=(px(4), px(14)))
        self.v_keep = tk.BooleanVar(value=True)
        self.v_rmset = tk.BooleanVar(value=False)
        Toggle(card.body, "Keep screenshots & wallpapers", self.v_keep,
               sub="the screenshots/ and wallpapers/ folders stay").pack(fill=tk.X, pady=(0, px(10)))
        Toggle(card.body, "Also remove my settings", self.v_rmset,
               sub="options and unit numbers (~/.scientist_mirror.json)").pack(fill=tk.X)
        tk.Label(card.body, text="Open mirrors are closed. Phones are not changed.", font=F.mono_s, fg=C.FAINT,
                 bg=C.SURFACE, anchor="w").pack(fill=tk.X, pady=(px(14), 0))
        RButton(self.foot_row, "UNINSTALL", icon="trash", variant="danger", height=38, radius=12,
                command=self._start_uninstall).pack(side=tk.RIGHT)
        RButton(self.foot_row, "CANCEL", height=38, radius=12, command=self._close).pack(side=tk.RIGHT,
                                                                                        padx=(0, px(10)))

    def _start_uninstall(self):
        opts = {"keep_media": self.v_keep.get(), "remove_settings": self.v_rmset.get()}
        self._busy = True
        self._page_progress("Uninstalling")

        def work():
            try:
                do_uninstall(self.target_dir, opts, self._report)
                self.q.put(lambda: self._page_done(True, False))
            except Exception as e:
                self._report(None, f"Uninstall failed: {e}")
                self.q.put(lambda: self._page_done(False, False, str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _close(self, force=False):
        if self._busy and not force:
            return
        self.root.destroy()


# ════════════════════════════════════════════════════════════════════
#  Entry point
# ════════════════════════════════════════════════════════════════════
def relaunch_uninstaller_from_temp(target):
    """uninstall.exe lives inside the folder it must delete: run a temp copy instead."""
    tmp = Path(tempfile.gettempdir()) / f"Scientist-uninstall-{os.getpid()}.exe"
    shutil.copy2(sys.executable, tmp)
    args = [str(tmp), "--uninstall", "--dir", str(target), "--from-temp"] + \
           (["--silent"] if "--silent" in sys.argv else [])
    subprocess.Popen(args, close_fds=True)


def schedule_self_delete():
    me = sys.executable
    subprocess.Popen(f'cmd /c ping -n 3 127.0.0.1 >nul & del /f /q "{me}"', shell=True,
                     creationflags=NO_WINDOW, close_fds=True)


LOG_FILE = Path(tempfile.gettempdir()) / "Scientist-setup.log"


def silent_report(_frac, text):
    """Progress for --silent runs: appended to %TEMP%\\Scientist-setup.log and echoed if a console exists."""
    if not text:
        return
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")
    except OSError:
        pass
    try:
        if sys.stdout:
            enc = sys.stdout.encoding or "utf-8"
            sys.stdout.write(text.encode(enc, "replace").decode(enc) + "\n")
            sys.stdout.flush()
    except Exception:
        pass


def run_silent(job, *args):
    """Never show a crash dialog in silent mode: log the error and exit with code 1."""
    try:
        job(*args, silent_report)
    except Exception as e:
        silent_report(None, f"FAILED: {e!r}")
        sys.exit(1)


def arg_value(flag):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def main():
    uninstall = "--uninstall" in sys.argv
    silent = "--silent" in sys.argv
    target = arg_value("--dir")
    if uninstall:
        existing = registry_entry()
        target = target or (existing["dir"] if existing else str(Path(sys.executable).parent))
        frozen = getattr(sys, "frozen", False)
        if frozen and "--from-temp" not in sys.argv and \
                Path(sys.executable).resolve().parent == Path(target).resolve():
            relaunch_uninstaller_from_temp(target)
            return
        if silent:
            run_silent(do_uninstall, target, {"keep_media": True, "remove_settings": False})
        else:
            root = tk.Tk()
            S.SCALE = max(1.0, root.winfo_fpixels("1i") / 96.0)
            S.init_fonts(root)
            SetupWizard(root, mode="uninstall", target=target)
            root.mainloop()
        if frozen and "--from-temp" in sys.argv:
            schedule_self_delete()
        return
    if silent:
        no_shortcuts = "--no-shortcuts" in sys.argv
        run_silent(do_install, target or DEFAULT_DIR,
                   {"core": True, "helper": True, "scrcpy": True,
                    "desktop": not no_shortcuts, "startmenu": not no_shortcuts})
        return
    root = tk.Tk()
    S.SCALE = max(1.0, root.winfo_fpixels("1i") / 96.0)
    S.init_fonts(root)
    SetupWizard(root, mode="install", target=target)
    root.mainloop()


if __name__ == "__main__":
    main()
