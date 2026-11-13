#!/usr/bin/env python3
"""
ADB Multi-Mirror + Per-Device Screen OFF
- Select multiple devices, mirror all of them at once
- Each mirror has its own "Screen OFF/ON" button (turns off physical panel while keeping control)
- Batch screen off/on for all mirrored devices
- Auto stop of everything on close
"""

import sys
import time
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import win32gui
    import win32con
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


class ScrollableFrame(ttk.Frame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self.canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0, bg="#1e1e1e")
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas)
        self.window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)
        self.on_resize_callback = None
    
    def _on_canvas_resize(self, event):
        self.canvas.itemconfig(self.window_id, width=event.width)
        if self.on_resize_callback:
            self.on_resize_callback()
    
    def _on_wheel(self, event):
        try:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except Exception:
            pass


class MirrorPanel:
    def __init__(self, parent, serial, log_fn, on_close_fn, stay_awake=True):
        self.serial = serial
        self.log = log_fn
        self.on_close_fn = on_close_fn
        self.scrcpy_proc = None
        self.embedded_hwnd = None
        self.screen_off = False
        self.window_title = f"Scrcpy_{serial.replace(':','_')}_{int(time.time()*1000)}"
        self._stay_awake = stay_awake
        
        self.frame = ttk.LabelFrame(parent, text=f"📱 {serial}", padding=4)
        
        hdr = ttk.Frame(self.frame)
        hdr.pack(fill=tk.X, pady=(0, 4))
        
        self.screen_btn = ttk.Button(hdr, text="Screen OFF", width=12, command=self.toggle_screen)
        self.screen_btn.pack(side=tk.LEFT)
        
        self.status_label = ttk.Label(hdr, text="starting…", foreground="#888")
        self.status_label.pack(side=tk.LEFT, padx=6)
        
        ttk.Button(hdr, text="✕", width=3, command=self.close).pack(side=tk.RIGHT)
        
        self.mirror_holder = tk.Frame(self.frame, bg="#000", width=400, height=700)
        self.mirror_holder.pack(fill=tk.BOTH, expand=True)
        self.mirror_holder.pack_propagate(False)
        self.mirror_holder.bind("<Configure>", self._resize)
        
        self._closed = False
    
    def start(self):
        cmd = ["scrcpy", "-s", self.serial, "--window-title", self.window_title]
        if self._stay_awake:
            cmd.append("--stay-awake")
        self.log(f"[{self.serial}] start: {' '.join(cmd)}")
        try:
            self.scrcpy_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, creationflags=CREATE_NO_WINDOW,
            )
        except Exception as e:
            self.log(f"[{self.serial}] launch failed: {e}")
            self.status_label.config(text="launch failed", foreground="#c00")
            return
        threading.Thread(target=self._read_output, daemon=True).start()
        self._try_embed(0)
    
    def _read_output(self):
        try:
            for line in self.scrcpy_proc.stdout:
                self.log(f"[{self.serial}] {line.rstrip()}")
        except Exception:
            pass
    
    def _try_embed(self, attempt):
        if self._closed:
            return
        if not (self.scrcpy_proc and self.scrcpy_proc.poll() is None):
            return
        if not HAS_WIN32:
            if attempt == 0:
                self.status_label.config(text="standalone", foreground="#c80")
            return
        hwnd = win32gui.FindWindow("SDL_app", self.window_title)
        if not hwnd:
            hwnd = win32gui.FindWindow(None, self.window_title)
        if hwnd:
            self._embed(hwnd)
            return
        if attempt >= 100:
            self.status_label.config(text="standalone", foreground="#c80")
            return
        self.frame.after(100, lambda: self._try_embed(attempt + 1))
    
    def _embed(self, hwnd):
        try:
            self.mirror_holder.update_idletasks()
            parent = int(self.mirror_holder.winfo_id())
            win32gui.SetParent(hwnd, parent)
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            style &= ~(win32con.WS_CAPTION | win32con.WS_THICKFRAME)
            win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            self.embedded_hwnd = hwnd
            self.status_label.config(text="live", foreground="#080")
            self._resize()
            self.log(f"[{self.serial}] embedded")
        except Exception as e:
            self.log(f"[{self.serial}] embed failed: {e}")
            self.status_label.config(text="embed failed", foreground="#c00")
    
    def _resize(self, event=None):
        if not self.embedded_hwnd:
            return
        try:
            w = max(1, self.mirror_holder.winfo_width())
            h = max(1, self.mirror_holder.winfo_height())
            win32gui.MoveWindow(self.embedded_hwnd, 0, 0, w, h, True)
        except Exception:
            pass
    
    def _adb_display(self, on):
        sub = "power-on" if on else "power-off"
        try:
            r = subprocess.run(
                ["adb", "-s", self.serial, "shell", "cmd", "display", sub, "0"],
                capture_output=True, text=True, timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
            if r.returncode == 0:
                return True, (r.stdout or "").strip()
            return False, ((r.stderr or "") + " " + (r.stdout or "")).strip()
        except Exception as e:
            return False, str(e)
    
    def toggle_screen(self):
        if self.screen_off:
            ok, msg = self._adb_display(on=True)
            if ok:
                self.screen_off = False
                self.screen_btn.config(text="Screen OFF")
                self.log(f"[{self.serial}] display ON")
            else:
                self.log(f"[{self.serial}] display ON failed: {msg}")
        else:
            ok, msg = self._adb_display(on=False)
            if ok:
                self.screen_off = True
                self.screen_btn.config(text="Screen ON")
                self.log(f"[{self.serial}] display OFF")
            else:
                self.log(f"[{self.serial}] display OFF failed: {msg}")
    
    def force_screen(self, off):
        if off != self.screen_off:
            self.toggle_screen()
    
    def stop(self):
        proc = self.scrcpy_proc
        self.scrcpy_proc = None
        self.embedded_hwnd = None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            self.status_label.config(text="stopped", foreground="#888")
        except Exception:
            pass
    
    def close(self):
        if self._closed:
            return
        self._closed = True
        self.stop()
        try:
            self.on_close_fn(self)
        except Exception:
            pass
        try:
            self.frame.destroy()
        except Exception:
            pass


class AdbScrcpyApp:
    def __init__(self, root):
        self.root = root
        self.root.title("ADB Multi-Mirror + Screen OFF")
        self.root.geometry("1500x950")
        
        self.panels = {}
        
        self._build_ui()
        self.refresh_devices()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
    
    def _build_ui(self):
        main_paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main_paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        
        left = ttk.Frame(main_paned)
        right = ttk.Frame(main_paned)
        main_paned.add(left, weight=0)
        main_paned.add(right, weight=1)
        
        # Device list
        dev_box = ttk.LabelFrame(left, text="Devices (Ctrl/Shift-click to select many)", padding=6)
        dev_box.pack(fill=tk.X, pady=(0, 6))
        
        list_holder = ttk.Frame(dev_box)
        list_holder.pack(fill=tk.BOTH, expand=True)
        self.device_list = tk.Listbox(list_holder, selectmode=tk.EXTENDED, height=8, exportselection=False)
        self.device_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(list_holder, orient=tk.VERTICAL, command=self.device_list.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.device_list.config(yscrollcommand=sb.set)
        
        btn_row = ttk.Frame(dev_box)
        btn_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(btn_row, text="Refresh", command=self.refresh_devices).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="All", command=self._select_all).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="None", command=self._clear_selection).pack(side=tk.LEFT)
        
        # Mirror box
        mbox = ttk.LabelFrame(left, text="Mirror", padding=6)
        mbox.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(mbox, text="Start mirror for selected", command=self.start_mirror_selected).pack(fill=tk.X, pady=2)
        ttk.Button(mbox, text="Stop all mirrors", command=self.stop_all_mirrors).pack(fill=tk.X, pady=2)
        
        self.stay_awake_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            mbox, text="Keep devices awake (--stay-awake)",
            variable=self.stay_awake_var,
        ).pack(anchor="w", pady=(6, 0))
        
        # Batch screen control
        sbox = ttk.LabelFrame(left, text="Batch screen (all mirrored)", padding=6)
        sbox.pack(fill=tk.X, pady=(0, 6))
        row = ttk.Frame(sbox)
        row.pack(fill=tk.X)
        ttk.Button(row, text="Screen OFF all", command=self.screen_off_all).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 2))
        ttk.Button(row, text="Screen ON all", command=self.screen_on_all).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(2, 0))
        
        # Manual ADB
        mbox2 = ttk.LabelFrame(left, text="Manual ADB (selected devices)", padding=6)
        mbox2.pack(fill=tk.X, pady=(0, 6))
        row = ttk.Frame(mbox2)
        row.pack(fill=tk.X)
        ttk.Button(row, text="ADB screen OFF", command=self.adb_screen_off_selected).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 2))
        ttk.Button(row, text="ADB screen ON", command=self.adb_screen_on_selected).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(2, 0))
        
        # Mirror area
        self.scroll = ScrollableFrame(right)
        self.scroll.pack(fill=tk.BOTH, expand=True)
        self.scroll.on_resize_callback = self._relayout_panels
        
        # Log
        log_box = ttk.LabelFrame(self.root, text="Log", padding=4)
        log_box.pack(fill=tk.X, padx=6, pady=(0, 6))
        self.log_text = tk.Text(log_box, height=6)
        self.log_text.pack(fill=tk.X)
    
    # ---------- logging ----------
    def log(self, message):
        try:
            self.root.after(0, self._append_log, message)
        except tk.TclError:
            pass
    
    def _append_log(self, message):
        self.log_text.insert(tk.END, str(message) + "\n")
        self.log_text.see(tk.END)
    
    # ---------- device list ----------
    def _select_all(self):
        self.device_list.selection_set(0, tk.END)
    
    def _clear_selection(self):
        self.device_list.selection_clear(0, tk.END)
    
    def selected_devices(self):
        return [self.device_list.get(i) for i in self.device_list.curselection()]
    
    def refresh_devices(self):
        try:
            subprocess.run(["adb", "start-server"], capture_output=True, text=True, timeout=15, creationflags=CREATE_NO_WINDOW)
            r = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=15, creationflags=CREATE_NO_WINDOW)
        except FileNotFoundError:
            messagebox.showerror("ADB not found", "adb is not in PATH.")
            return
        except Exception as e:
            messagebox.showerror("ADB error", str(e))
            return
        
        prev = set(self.selected_devices())
        devices = []
        for line in r.stdout.splitlines()[1:]:
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])
        
        self.device_list.delete(0, tk.END)
        for d in devices:
            self.device_list.insert(tk.END, d)
        
        for i, d in enumerate(devices):
            if d in prev:
                self.device_list.selection_set(i)
        if not self.selected_devices() and devices:
            self.device_list.selection_set(0)
        
        if devices:
            self.log(f"Devices: {', '.join(devices)}")
        else:
            self.log("No devices. Enable USB debugging and authorize the PC.")
    
    # ---------- mirroring ----------
    def _relayout_panels(self):
        for p in self.panels.values():
            try:
                p.frame.grid_forget()
            except Exception:
                pass
        if not self.panels:
            return
        try:
            self.scroll.canvas.update_idletasks()
            w = self.scroll.canvas.winfo_width()
        except Exception:
            w = 900
        cols = max(1, w // 440)
        for i, p in enumerate(self.panels.values()):
            p.frame.grid(row=i // cols, column=i % cols, padx=4, pady=4, sticky="n")
    
    def start_mirror_selected(self):
        sels = self.selected_devices()
        if not sels:
            messagebox.showwarning("No device", "Select at least one device.")
            return
        if not shutil.which("scrcpy"):
            messagebox.showerror("scrcpy not found", "scrcpy is not in PATH.")
            return
        
        stay_awake = self.stay_awake_var.get()
        for serial in sels:
            if serial in self.panels:
                self.log(f"[{serial}] already mirrored, skipping")
                continue
            panel = MirrorPanel(
                self.scroll.inner, serial, self.log,
                self._on_panel_close, stay_awake=stay_awake,
            )
            self.panels[serial] = panel
            panel.start()
        self._relayout_panels()
    
    def _on_panel_close(self, panel):
        self.panels.pop(panel.serial, None)
        self.root.after(20, self._relayout_panels)
    
    def stop_all_mirrors(self):
        for serial in list(self.panels.keys()):
            panel = self.panels.get(serial)
            if panel:
                panel.close()
    
    # ---------- batch screen ----------
    def screen_off_all(self):
        for panel in list(self.panels.values()):
            panel.force_screen(True)
    
    def screen_on_all(self):
        for panel in list(self.panels.values()):
            panel.force_screen(False)
    
    # ---------- manual ADB on selected devices ----------
    def adb_screen_off_selected(self):
        sels = self.selected_devices()
        if not sels:
            messagebox.showwarning("No device", "Select at least one device.")
            return
        for serial in sels:
            try:
                r = subprocess.run(
                    ["adb", "-s", serial, "shell", "cmd", "display", "power-off", "0"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=CREATE_NO_WINDOW,
                )
                if r.returncode == 0:
                    self.log(f"[{serial}] ADB display OFF")
                else:
                    self.log(f"[{serial}] ADB display OFF failed: {(r.stderr or r.stdout).strip()}")
            except Exception as e:
                self.log(f"[{serial}] ADB display OFF error: {e}")
    
    def adb_screen_on_selected(self):
        sels = self.selected_devices()
        if not sels:
            messagebox.showwarning("No device", "Select at least one device.")
            return
        for serial in sels:
            try:
                r = subprocess.run(
                    ["adb", "-s", serial, "shell", "cmd", "display", "power-on", "0"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=CREATE_NO_WINDOW,
                )
                if r.returncode == 0:
                    self.log(f"[{serial}] ADB display ON")
                else:
                    self.log(f"[{serial}] ADB display ON failed: {(r.stderr or r.stdout).strip()}")
            except Exception as e:
                self.log(f"[{serial}] ADB display ON error: {e}")
    
    # ---------- shutdown ----------
    def on_close(self):
        for serial in list(self.panels.keys()):
            panel = self.panels.get(serial)
            if panel:
                panel.close()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = AdbScrcpyApp(root)
    root.mainloop()