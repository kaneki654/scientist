#!/usr/bin/env python3
"""
FB Daily — desktop app (no terminal needed).

A friendly one-window control panel for the fbdaily engine: connect your phones,
pick how you want it to behave, press START. It shows what every phone is doing
live and stops cleanly (closing FB Lite on all phones) when you press STOP.

    python gui.py

Drop your icon at  fbdaily/icon.png  to brand the splash + window (a themed
Facebook-blue badge is drawn as a fallback).
"""
import os
import sys
import math
import time
import queue
import random
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fbdaily as fb                                   # the engine

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QGridLayout, QLabel, QPushButton, QComboBox, QFrame,
    QScrollArea, QSizePolicy, QButtonGroup, QGraphicsDropShadowEffect)
from PyQt6.QtCore import (Qt, QTimer, QPropertyAnimation, QEasingCurve, QRectF,
    QPointF, QSize, pyqtProperty)
from PyQt6.QtGui import (QPainter, QColor, QBrush, QPen, QFont, QPixmap, QIcon,
    QLinearGradient, QRadialGradient, QPainterPath, QPolygonF)

# ── palette (from the icon) ─────────────────────────────────────────────────
FB_BLUE   = "#1877F2"
FB_BRIGHT = "#2D88FF"
FB_DEEP   = "#0B57D0"
NAVY0     = "#0A1730"
NAVY1     = "#0E2350"
PANEL     = "#14274F"
PANEL2    = "#1B346A"
TEXT      = "#EAF1FF"
MUTE      = "#8FA9D6"
GREEN     = "#2ED47A"
RED       = "#F0506E"
GOLD      = "#FFC64B"
Q = queue.Queue()                                       # engine -> GUI events


# ── icon (icon.png / icon.ico, else a drawn Facebook badge) ─────────────────
def _asset_dirs():
    dirs = [HERE]
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", "")
        dirs = [os.path.dirname(sys.executable)] + ([base] if base else []) + dirs
    return dirs


def app_pixmap(size=256):
    for d in _asset_dirs():
        for name in ("icon.png", "icon.ico", "icon.jpg"):
            p = os.path.join(d, name)
            if os.path.exists(p):
                pm = QPixmap(p)
                if not pm.isNull():
                    return pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
    # fallback: glossy blue coin with a white "f"
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QRadialGradient(size * 0.4, size * 0.35, size * 0.75)
    g.setColorAt(0, QColor(FB_BRIGHT))
    g.setColorAt(1, QColor(FB_DEEP))
    p.setPen(QPen(QColor("#0A47A8"), size * 0.05))
    p.setBrush(QBrush(g))
    p.drawEllipse(int(size * 0.04), int(size * 0.04), int(size * 0.92), int(size * 0.92))
    f = QFont("Arial Black", int(size * 0.55), QFont.Weight.Black)
    p.setFont(f)
    p.setPen(QColor("#FFFFFF"))
    p.drawText(pm.rect().adjusted(int(size*0.06), 0, 0, int(-size*0.02)),
               Qt.AlignmentFlag.AlignCenter, "f")
    p.end()
    return pm


# ── animated background: floating blue/white particles ──────────────────────
class Particles(QWidget):
    def __init__(self, parent=None, n=55):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.p = []
        for _ in range(n):
            self.p.append({"x": random.random(), "y": random.random(),
                           "dx": random.uniform(-.0025, .0025),
                           "dy": random.uniform(-.0025, .0025),
                           "s": random.uniform(2, 8),
                           "w": random.random() < .35})
        self.t = QTimer(self); self.t.timeout.connect(self._tick); self.t.start(33)

    def _tick(self):
        for d in self.p:
            d["x"] += d["dx"]; d["y"] += d["dy"]
            if d["x"] < 0 or d["x"] > 1: d["dx"] *= -1
            if d["y"] < 0 or d["y"] > 1: d["dy"] *= -1
        self.update()

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        for d in self.p:
            c = QColor(255, 255, 255, 42) if d["w"] else QColor(45, 136, 255, 55)
            p.setBrush(c); p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(d["x"]*w, d["y"]*h), d["s"], d["s"])


# ── Tom-chases-Jerry orbiting spinner ───────────────────────────────────────
class ChaseSpinner(QWidget):
    def __init__(self, parent=None, r=46):
        super().__init__(parent)
        self.setFixedSize(r*2 + 24, r*2 + 24)
        self.r = r; self.a = 0.0
        self.t = QTimer(self); self.t.timeout.connect(self._tick); self.t.start(16)

    def _tick(self):
        self.a += 0.06; self.update()

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width()/2, self.height()/2
        p.setPen(QPen(QColor(45, 136, 255, 60), 3)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), self.r, self.r)
        # Jerry (white) leads, Tom (blue) chases
        for lead, col, sz in ((0.0, QColor("#FFFFFF"), 9),
                              (-0.9, QColor(FB_BRIGHT), 13)):
            ang = self.a + lead
            x = cx + self.r*math.cos(ang); y = cy + self.r*math.sin(ang)
            p.setBrush(col); p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(x, y), sz, sz)


class Pulse(QWidget):
    """small pulsing status dot."""
    def __init__(self, parent=None):
        super().__init__(parent); self.setFixedSize(16, 16)
        self.on = False; self.t = 0
        self.k = QTimer(self); self.k.timeout.connect(self._tick); self.k.start(40)

    def _tick(self):
        self.t += 1; self.update()

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.on:
            a = 0.4 + 0.6*abs(math.sin(self.t*0.12))
            p.setBrush(QColor(46, 212, 122, int(255*a)))
        else:
            p.setBrush(QColor(120, 140, 170, 140))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(3, 3, 10, 10)


# ── splash ──────────────────────────────────────────────────────────────────
class Splash(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint)
        self.resize(520, 420); self._center()
        self.bg = Particles(self); self.bg.setGeometry(0, 0, 520, 420)
        self.icon = app_pixmap(150)
        self.sp = ChaseSpinner(self, 78); self.sp.move(260 - self.sp.width()//2, 250)
        self.t = 0
        self.k = QTimer(self); self.k.timeout.connect(self._tick); self.k.start(40)

    def _center(self):
        s = QApplication.primaryScreen().geometry()
        self.move((s.width()-520)//2, (s.height()-420)//2)

    def _tick(self): self.t += 1; self.update()

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0, QColor(FB_BLUE)); g.setColorAt(1, QColor(FB_DEEP))
        path = QPainterPath(); path.addRoundedRect(QRectF(self.rect()), 22, 22)
        p.fillPath(path, QBrush(g))
        bob = int(6*math.sin(self.t*0.08))
        p.drawPixmap(260 - self.icon.width()//2, 70 + bob, self.icon)
        p.setPen(QColor("#FFFFFF"))
        p.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))
        p.drawText(QRectF(0, 230, 520, 40), Qt.AlignmentFlag.AlignCenter, "FB Daily")
        p.setPen(QColor(230, 240, 255, 210))
        p.setFont(QFont("Segoe UI", 10))
        p.drawText(QRectF(0, 360, 520, 30), Qt.AlignmentFlag.AlignCenter,
                   "Starting up" + "." * (1 + (self.t//8) % 3))


# ── little building blocks ──────────────────────────────────────────────────
def shadow(widget, blur=24, dy=6, a=120):
    e = QGraphicsDropShadowEffect(widget)
    e.setBlurRadius(blur); e.setOffset(0, dy); e.setColor(QColor(0, 0, 0, a))
    widget.setGraphicsEffect(e); return widget


class Toggle(QPushButton):
    """A pill on/off switch."""
    def __init__(self, on=True):
        super().__init__(); self.setCheckable(True); self.setChecked(on)
        self.setFixedSize(52, 28); self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isChecked()
        p.setBrush(QColor(FB_BRIGHT) if on else QColor(70, 90, 120))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(0, 0, 52, 28, 14, 14)
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(26 if on else 3, 3, 22, 22)


class StatCard(QFrame):
    def __init__(self, label, color=FB_BRIGHT):
        super().__init__()
        self.setObjectName("card"); self.setMinimumWidth(150)
        self._val = 0.0; self.target = 0
        lo = QVBoxLayout(self); lo.setContentsMargins(14, 12, 14, 12); lo.setSpacing(2)
        self.num = QLabel("0"); self.num.setStyleSheet(
            f"color:{color};font-size:30px;font-weight:800;background:transparent")
        cap = QLabel(label); cap.setStyleSheet(
            f"color:{MUTE};font-size:11px;font-weight:600;background:transparent")
        lo.addWidget(self.num); lo.addWidget(cap)
        t = QTimer(self); t.timeout.connect(self._anim); t.start(40)

    def set(self, v):
        self.target = v

    def _anim(self):
        if abs(self._val - self.target) > 0.5:
            self._val += (self.target - self._val) * 0.2
            self.num.setText(str(int(round(self._val))))
        elif int(self._val) != self.target:
            self._val = self.target; self.num.setText(str(self.target))


class DeviceRow(QFrame):
    def __init__(self, serial):
        super().__init__(); self.setObjectName("devrow")
        lo = QHBoxLayout(self); lo.setContentsMargins(12, 7, 12, 7)
        self.dot = Pulse(); lo.addWidget(self.dot)
        name = QLabel(serial); name.setStyleSheet(
            f"color:{TEXT};font-size:11px;font-family:Consolas;background:transparent")
        lo.addWidget(name)
        lo.addStretch()
        self.act = QLabel("idle"); self.act.setStyleSheet(
            f"color:{MUTE};font-size:10px;background:transparent")
        lo.addWidget(self.act)

    def set_activity(self, text, active=True):
        self.dot.on = active; self.act.setText(text[:34])


# ── main window ─────────────────────────────────────────────────────────────
INTENSITY = {
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
TONES = ["friendly and warm", "nice and polite", "playful and fun", "sweet",
         "a little flirty", "chill and casual"]


class App(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FB Daily")
        self.setWindowIcon(QIcon(app_pixmap(64)))
        self.setMinimumSize(1000, 680)
        self.running = False
        self.threads = []
        self.rows = {}
        self.totals = {"reacts": 0, "comments": 0, "shares": 0, "replied": 0,
                       "added": 0, "confirmed": 0}
        self._build()
        # tee the engine's logger into the activity feed
        fb.log = self._tee_log
        self.timer = QTimer(self); self.timer.timeout.connect(self._drain); self.timer.start(120)
        self._refresh_devices()

    # ---- engine log hook (runs on worker threads) -> queue ----
    def _tee_log(self, serial, msg):
        with fb._print_lock:
            print(f"  [{serial}] {msg}", flush=True)
        Q.put(("log", serial, msg))

    # ---- UI ----
    def _build(self):
        root = QWidget(); self.setCentralWidget(root)
        self.bg = Particles(root); self.bg.lower()
        main = QVBoxLayout(root); main.setContentsMargins(22, 16, 22, 16); main.setSpacing(14)

        # header
        head = QHBoxLayout()
        ic = QLabel(); ic.setPixmap(app_pixmap(52)); head.addWidget(ic)
        tt = QVBoxLayout(); tt.setSpacing(0)
        title = QLabel("FB Daily"); title.setStyleSheet(
            f"color:{TEXT};font-size:24px;font-weight:800")
        sub = QLabel("Automatic Facebook daily activity"); sub.setStyleSheet(
            f"color:{MUTE};font-size:11px")
        tt.addWidget(title); tt.addWidget(sub); head.addLayout(tt)
        head.addStretch()
        self.status_pulse = Pulse(); head.addWidget(self.status_pulse)
        self.status_lbl = QLabel("Stopped"); self.status_lbl.setStyleSheet(
            f"color:{MUTE};font-size:12px;font-weight:700")
        head.addWidget(self.status_lbl)
        main.addLayout(head)

        body = QHBoxLayout(); body.setSpacing(14); main.addLayout(body, 1)

        # left column: devices + settings
        left = QVBoxLayout(); left.setSpacing(14)
        left.addWidget(self._devices_panel(), 1)
        left.addWidget(self._settings_panel())
        body.addLayout(left, 0)

        # right column: dashboard + feed + start/stop
        right = QVBoxLayout(); right.setSpacing(14)
        right.addWidget(self._stats_panel())
        right.addWidget(self._feed_panel(), 1)
        right.addLayout(self._controls())
        body.addLayout(right, 1)

        root.setStyleSheet(f"""
            QWidget{{color:{TEXT};font-family:'Segoe UI'}}
            #panel{{background:{PANEL};border:1px solid #24406f;border-radius:14px}}
            #card{{background:{PANEL2};border:1px solid #2a4c86;border-radius:12px}}
            #devrow{{background:#0f2145;border:1px solid #24406f;border-radius:9px}}
            QComboBox{{background:#0f2145;border:1px solid #2a4c86;border-radius:8px;
                       padding:6px 10px;color:{TEXT};font-size:12px}}
            QComboBox QAbstractItemView{{background:{PANEL};color:{TEXT};
                       selection-background-color:{FB_BLUE}}}
            QScrollArea{{border:none;background:transparent}}
        """)

    def _panel(self, title):
        f = QFrame(); f.setObjectName("panel"); shadow(f)
        lo = QVBoxLayout(f); lo.setContentsMargins(14, 12, 14, 12); lo.setSpacing(8)
        h = QLabel(title); h.setStyleSheet(
            f"color:{FB_BRIGHT};font-size:12px;font-weight:800;background:transparent")
        lo.addWidget(h)
        return f, lo

    def _devices_panel(self):
        f, lo = self._panel("PHONES (USB)")
        row = QHBoxLayout()
        self.dev_count = QLabel("0 connected"); self.dev_count.setStyleSheet(
            f"color:{MUTE};font-size:11px;background:transparent")
        row.addWidget(self.dev_count); row.addStretch()
        rb = QPushButton("Refresh"); rb.setCursor(Qt.CursorShape.PointingHandCursor)
        rb.setStyleSheet(f"QPushButton{{background:#0f2145;border:1px solid #2a4c86;"
                         f"border-radius:8px;padding:4px 12px;color:{TEXT};font-size:10px}}"
                         f"QPushButton:hover{{background:{PANEL2}}}")
        rb.clicked.connect(self._refresh_devices); row.addWidget(rb)
        lo.addLayout(row)
        sa = QScrollArea(); sa.setWidgetResizable(True); sa.setMinimumWidth(280)
        self.dev_holder = QWidget(); self.dev_lo = QVBoxLayout(self.dev_holder)
        self.dev_lo.setContentsMargins(0, 0, 0, 0); self.dev_lo.setSpacing(6)
        self.dev_lo.addStretch()
        sa.setWidget(self.dev_holder); lo.addWidget(sa, 1)
        return f

    def _settings_panel(self):
        f, lo = self._panel("SETTINGS")
        def row(text, widget):
            r = QHBoxLayout(); l = QLabel(text)
            l.setStyleSheet(f"color:{TEXT};font-size:12px;background:transparent")
            r.addWidget(l); r.addStretch(); r.addWidget(widget); lo.addLayout(r)
        self.tg_chat = Toggle(True);   row("Reply to messages", self.tg_chat)
        self.tg_friends = Toggle(True); row("Add / confirm friends", self.tg_friends)
        self.tg_engage = Toggle(True);  row("React / comment / share", self.tg_engage)
        self.cmb_tone = QComboBox(); self.cmb_tone.addItems(
            ["Mixed (all tones)"] + [t.title() for t in TONES])
        row("Chat tone", self.cmb_tone)
        self.cmb_int = QComboBox(); self.cmb_int.addItems(list(INTENSITY.keys()))
        self.cmb_int.setCurrentText("Normal"); row("Intensity", self.cmb_int)
        return f

    def _stats_panel(self):
        f, lo = self._panel("TODAY")
        grid = QGridLayout(); grid.setSpacing(10)
        self.cards = {}
        specs = [("reacts", "Reactions", FB_BRIGHT), ("comments", "Comments", "#7FD1FF"),
                 ("shares", "Shares", GOLD), ("replied", "Chat replies", GREEN),
                 ("added", "Friend requests", "#FF9BB3"), ("confirmed", "Confirmed", "#B9A6FF")]
        for i, (k, lbl, col) in enumerate(specs):
            c = StatCard(lbl, col); self.cards[k] = c
            grid.addWidget(c, i // 3, i % 3)
        lo.addLayout(grid)
        return f

    def _feed_panel(self):
        f, lo = self._panel("LIVE ACTIVITY")
        sa = QScrollArea(); sa.setWidgetResizable(True)
        self.feed_holder = QWidget(); self.feed_lo = QVBoxLayout(self.feed_holder)
        self.feed_lo.setContentsMargins(0, 0, 0, 0); self.feed_lo.setSpacing(3)
        self.feed_lo.addStretch()
        sa.setWidget(self.feed_holder); self.feed_area = sa
        lo.addWidget(sa, 1)
        return f

    def _controls(self):
        row = QHBoxLayout()
        self.btn = QPushButton("▶   START"); self.btn.setFixedHeight(56)
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._style_btn(False); self.btn.clicked.connect(self._toggle_run)
        shadow(self.btn, 26, 8, 150)
        row.addWidget(self.btn)
        return row

    def _style_btn(self, running):
        if running:
            self.btn.setText("■   STOP")
            self.btn.setStyleSheet(
                f"QPushButton{{background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
                f"stop:0 #ff6b85,stop:1 {RED});color:white;border:none;"
                f"border-radius:14px;font-size:17px;font-weight:800}}"
                f"QPushButton:hover{{background:{RED}}}")
        else:
            self.btn.setText("▶   START")
            self.btn.setStyleSheet(
                f"QPushButton{{background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
                f"stop:0 #56e39a,stop:1 {GREEN});color:#06331d;border:none;"
                f"border-radius:14px;font-size:17px;font-weight:800}}"
                f"QPushButton:hover{{background:#3ddc8a}}")

    # ---- devices ----
    def _refresh_devices(self):
        devices = fb.usb_devices()
        for i in reversed(range(self.dev_lo.count() - 1)):
            w = self.dev_lo.itemAt(i).widget()
            if w:
                w.deleteLater()
        self.rows = {}
        for s in devices:
            r = DeviceRow(s); self.rows[s] = r
            self.dev_lo.insertWidget(self.dev_lo.count() - 1, r)
        self.dev_count.setText(f"{len(devices)} connected")

    def _feed(self, serial, msg):
        lbl = QLabel(f"[{serial[-4:]}]  {msg}")
        lbl.setStyleSheet(f"color:{MUTE};font-size:10px;font-family:Consolas;"
                          f"background:transparent")
        lbl.setWordWrap(True)
        self.feed_lo.insertWidget(self.feed_lo.count() - 1, lbl)
        if self.feed_lo.count() > 220:
            w = self.feed_lo.itemAt(0).widget()
            if w:
                w.deleteLater()
        QTimer.singleShot(20, lambda: self.feed_area.verticalScrollBar().setValue(
            self.feed_area.verticalScrollBar().maximum()))

    # ---- config from the UI ----
    def _build_cfg(self):
        cfg = fb.load_config()
        pre = INTENSITY[self.cmb_int.currentText()]
        cfg["session_minutes"] = pre["session_minutes"]
        cfg["between_sessions_seconds"] = pre["between_sessions_seconds"]
        cfg["engage"]["add_friends_range"] = pre["add_friends_range"]
        cfg["engage"]["confirm_range"] = pre["confirm_range"]
        cfg["weights"]["engage"] = pre["weights_engage"] if self.tg_engage.isChecked() else 0
        cfg["weights"]["add_friends"] = pre["weights_add"] if self.tg_friends.isChecked() else 0
        cfg["weights"]["confirm_requests"] = pre["weights_conf"] if self.tg_friends.isChecked() else 0
        cfg["chat"]["enabled"] = self.tg_chat.isChecked()
        tone = self.cmb_tone.currentText()
        cfg["chat"]["styles"] = TONES if tone.startswith("Mixed") else [tone.lower()]
        return cfg

    # ---- start / stop ----
    def _toggle_run(self):
        if self.running:
            self._stop()
        else:
            self._start()

    def _start(self):
        devices = fb.usb_devices()
        if not devices:
            self.status_lbl.setText("No USB phones found"); return
        self.running = True
        fb.STOP.clear()
        try:
            if os.path.exists(fb.STOP_FILE):
                os.remove(fb.STOP_FILE)
        except Exception:
            pass
        cfg = self._build_cfg()
        self._style_btn(True)
        self.status_pulse.on = True
        self.status_lbl.setStyleSheet(f"color:{GREEN};font-size:12px;font-weight:700")
        self.status_lbl.setText("Running")
        for k in self.totals:
            self.totals[k] = 0
        self.threads = []
        for s in devices:
            t = threading.Thread(target=self._phone_loop, args=(s, cfg), daemon=True)
            t.start(); self.threads.append(t)

    def _stop(self):
        self.status_lbl.setStyleSheet(f"color:{GOLD};font-size:12px;font-weight:700")
        self.status_lbl.setText("Stopping — closing FB Lite…")
        fb.STOP.set()
        self.running = False
        self._style_btn(False)
        self.btn.setEnabled(False)
        threading.Thread(target=self._await_stop, daemon=True).start()

    def _await_stop(self):
        for t in self.threads:
            t.join(timeout=60)
        for s in list(self.rows):
            try:
                fb.sh(s, "am", "force-stop", "com.facebook.lite")
                fb.sh(s, "input", "keyevent", "KEYCODE_HOME")
            except Exception:
                pass
        Q.put(("stopped", "", ""))

    def _phone_loop(self, serial, cfg):
        Q.put(("act", serial, "starting"))
        fb.interruptible_sleep(random.uniform(*cfg["stagger_seconds"]))
        while not fb.stop_requested():
            try:
                st = fb.full_session(serial, cfg)
                Q.put(("stats", serial, st))
            except Exception as e:
                Q.put(("log", serial, f"error: {e}"))
                fb.interruptible_sleep(5)
            if fb.stop_requested():
                break
            fb.interruptible_sleep(random.uniform(*cfg.get("between_sessions_seconds", [30, 120])))
        try:
            fb.sh(serial, "am", "force-stop", cfg.get("package", "com.facebook.lite"))
            fb.sh(serial, "input", "keyevent", "KEYCODE_HOME")
        except Exception:
            pass
        Q.put(("act", serial, "stopped"))

    # ---- drain engine events into the UI (main thread) ----
    def _drain(self):
        for _ in range(200):
            try:
                kind, serial, payload = Q.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._feed(serial, payload)
                r = self.rows.get(serial)
                if r:
                    low = payload.lower()
                    if "chat ->" in low: r.set_activity("chatting")
                    elif "reacted" in low or "react" in low: r.set_activity("reacting")
                    elif "add_friends" in low: r.set_activity("adding friends")
                    elif "confirm" in low: r.set_activity("confirming")
                    elif "done" in low: r.set_activity("browsing")
            elif kind == "act":
                r = self.rows.get(serial)
                if r:
                    r.set_activity(payload, active=(payload != "stopped"))
            elif kind == "stats":
                for k in self.totals:
                    self.totals[k] += payload.get(k, 0)
                for k, c in self.cards.items():
                    c.set(self.totals[k])
                r = self.rows.get(serial)
                if r:
                    r.set_activity("session done")
            elif kind == "stopped":
                self.status_pulse.on = False
                self.status_lbl.setStyleSheet(f"color:{MUTE};font-size:12px;font-weight:700")
                self.status_lbl.setText("Stopped")
                self.btn.setEnabled(True)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.bg.setGeometry(0, 0, self.width(), self.height())


def _selftest():
    """Headless build check: verify engine import, adb path, and construction."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else HERE
    out = os.path.join(base, "selftest.log")
    lines, code = [], 0
    try:
        lines.append(f"frozen={getattr(sys,'frozen',False)}")
        lines.append(f"fbdaily.ADB={fb.ADB} exists={os.path.exists(fb.ADB)}")
        radb = getattr(fb.fb, "ADB", None) if fb.fb else None
        lines.append(f"reactor.ADB={radb} exists={os.path.exists(radb) if radb else 'n/a'}")
        app = QApplication(sys.argv); app.setStyle("Fusion")
        _ = Splash(); _ = App()
        lines.append(f"devices={len(fb.usb_devices())}")
        lines.append("SELFTEST OK")
    except Exception:
        import traceback
        lines.append("SELFTEST FAIL:\n" + traceback.format_exc()); code = 1
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass
    print("\n".join(lines))
    sys.exit(code)


def _install_excepthook():
    # Without this, an unhandled exception inside a Qt virtual (e.g. paintEvent)
    # makes PyQt abort the whole process (0xC0000409). Log it and keep running.
    import traceback
    base = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
    def hook(t, v, tb):
        try:
            msg = "".join(traceback.format_exception(t, v, tb))
            try:
                sys.stderr.write(msg)
            except Exception:
                pass
            with open(os.path.join(base, "gui_error.log"), "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass
    sys.excepthook = hook


def main():
    _install_excepthook()
    if "--selftest" in sys.argv:
        _selftest()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    splash = Splash(); splash.show()
    win = App()

    def go():
        splash.close()
        win.showMaximized()
    QTimer.singleShot(2200, go)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
