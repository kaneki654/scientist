#!/usr/bin/env python3
"""
FB Daily — human-like daily activity on Facebook Lite across all USB phones.

Two kinds of activity, mixed randomly and staggered per phone so no two phones
do the same thing at the same instant:

  * BROWSE  — scroll feed, watch reels, check notifications, open posts.
  * ENGAGE  — open a post and REACT (randomized reaction), sometimes SHARE,
              sometimes COMMENT.

Engagement reuses the proven interaction code from the sibling reactor
(`../fb.py`).  Feed taps in FB Lite are unpredictable (a tap may open a photo,
a profile, or a link), so every engagement is self-correcting: it detects what
opened and only reacts/shares/comments on a real post surface, otherwise it
backs out — it never fires an action blindly.

USB ONLY: wireless ADB targets (serials like ip:port) are ignored.

Run with NO arguments -- every USB phone automatically does its daily activity,
staggered and randomized per phone:
  * confirm incoming friend requests, then add some friends
  * browse its own feed (scroll, reels, notifications) and, on TIMELINE posts,
    react (randomized) + sometimes comment + sometimes share

There is no fixed target post -- engagement happens on whatever is in each
account's feed.

    python fbdaily.py

It runs FOREVER — session after session on every phone — until you stop it:
press Ctrl+C, or create a file named STOP in this folder (works even when it's
running in the background). It also survives errors (a failed step just moves on
to the next session), so it won't quit on its own.

Behaviour is tuned only through config.json (session_minutes, stagger, activity
weights, reaction mix, friend ranges, between_sessions_seconds). No CLI flags.
"""
import os
import re
import sys
import json
import time
import signal
import random
import hashlib
import subprocess
import threading


def _silence_subprocess_windows():
    """On Windows, a windowed app (no console) pops a brand-new console window
    for EVERY child process (adb.exe). With ~20 phones firing many adb calls a
    second that floods the screen with flashing terminals. Force CREATE_NO_WINDOW
    on every subprocess (ours + uiautomator2/adbutils internals) so none appear."""
    if sys.platform != "win32" or getattr(subprocess, "_no_window_patched", False):
        return
    _orig_init = subprocess.Popen.__init__
    def _init(self, *args, **kwargs):
        if "creationflags" not in kwargs:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        _orig_init(self, *args, **kwargs)
    subprocess.Popen.__init__ = _init
    subprocess._no_window_patched = True


_silence_subprocess_windows()

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)

# Reuse the reactor's device/interaction helpers (react, comment, u2 connect).
sys.path.insert(0, PARENT)
try:
    import fb  # noqa: E402
except Exception as _e:                       # engagement disabled if unavailable
    fb = None
    _FB_IMPORT_ERR = _e

def _find_adb():
    import shutil
    # On Linux/macOS the bundled Windows adb.exe won't run — prefer a real adb
    # from PATH (the installer adds Android platform-tools there).
    if sys.platform != "win32":
        onpath = shutil.which("adb")
        if onpath:
            return onpath
    cands = []
    if getattr(sys, "frozen", False):            # packaged .exe
        base = getattr(sys, "_MEIPASS", "")
        if base:
            cands.append(os.path.join(base, "platform-tools", "adb.exe"))
        cands.append(os.path.join(os.path.dirname(sys.executable),
                                  "platform-tools", "adb.exe"))
    cands.append(os.path.join(PARENT, "platform-tools", "adb.exe"))
    for c in cands:
        if c and os.path.exists(c):
            return c
    return shutil.which("adb") or "adb"


ADB = _find_adb()
if fb is not None:
    fb.ADB = ADB                                   # one adb for everything

# per-phone uiautomator2 device of the running session (set in full_session)
_DEV = {}


def _d(serial):
    return _DEV.get(serial)


def _nav(serial, idx, w, h):
    """Tap FB Lite top tab `idx` (0 home, 1 friends, 2 messages, 3 video, 4 notifications, 5 market).
    The tab row moves (it sits under the 'facebook' header on the feed, at the top in Messenger), so
    it is located on screen; falls back to the feed layout."""
    d = _d(serial)
    if d is None or fb is None:
        tap(serial, int(w * (idx + 0.5) / 6), int(h * 0.12))      # no screen reader: feed layout
        return True

    def find():
        try:
            return fb.nav_points(d)
        except Exception:
            return None
    pts = find()
    if not pts:                                      # screen may still be changing: look again
        time.sleep(1.2)
        pts = find()
    for _ in range(3):                               # inside a thread / sheet: back out to the tabs
        if pts:
            break
        fb.hide_keyboard(serial)
        press_back(serial)
        time.sleep(1.3)
        pts = find()
    if not pts:                                      # left the app? bring FB Lite back
        sh(serial, "monkey", "-p", "com.facebook.lite", "-c", "android.intent.category.LAUNCHER", "1",
           timeout=20)
        time.sleep(4)
        pts = find()
    if not pts:
        log(serial, "tab bar not found — skipping tap")
        return False
    x, y = pts[idx]
    tap(serial, x, y)
    return True

DEFAULT_CONFIG = {
    "package": "com.facebook.lite",
    "session_minutes": [5, 12],
    # Rotate through EVERY saved account on the phone: each account gets this
    # many minutes of daily activity, then it switches to the next one (cycling).
    "switch_accounts": True,
    "account_switch_minutes": 15,
    "stagger_seconds": [0, 45],
    "read_pause_seconds": [2, 9],
    "watch_pause_seconds": [8, 22],
    "weights": {
        "scroll": 34,
        "scroll_back": 6,
        "long_watch": 8,
        "open_post": 5,
        "notifications": 5,
        "reels": 6,
        "engage": 28,
        "add_friends": 5,
        "confirm_requests": 3
    },
    "engage": {
        "enabled": True,
        "react_prob": 0.9,
        "share_prob": 0.3,
        "comment_prob": 0.3,
        "comment_example": "",
        "reaction_weights": {
            "like": 40, "love": 25, "haha": 15, "wow": 8,
            "care": 6, "sad": 3, "angry": 3
        },
        "add_friends_range": [2, 5],
        "confirm_range": [1, 5]
    },
    "chat": {
        "enabled": True,
        "persona": "a normal Filipino person",
        "styles": ["friendly and warm", "nice and polite", "playful and fun",
                   "sweet", "a little flirty", "chill and casual"],
        "max_per_session": 5,
        "check_interval_seconds": 60
    },
    "between_sessions_seconds": [30, 120],
    # Optional Pollinations key for AI comments / chat replies. Without one the free tier allows
    # one reply every ~45 s per internet connection (shared by all phones).
    "ai_key": "",
    # ── Anti-detection layer (per-phone, persistent in state.json) ──────────
    # Warm-up: brand-new phones do very little for the first days and ramp up
    # to full activity over ~2 weeks. `start_activity_pct` is the fraction of
    # normal activity on day 0 (e.g. 15 -> ~15%). Reaches 100% at `warmup_days`.
    "warmup": {"enabled": True, "warmup_days": 14, "start_activity_pct": 15},
    # 1-2 days a week per phone are 'rest days': just a bit of scrolling, no
    # engagement / friend requests / share / comment. Same weekday all week.
    "rest_days_per_week": [1, 2],
    # Cap the number of sessions per calendar day (further scaled down by
    # warm-up and rest days). Real people don't open FB 30 times a day.
    "max_sessions_per_day": [3, 8],
    # No new sessions during these local hours (people sleep). Wraps midnight.
    "quiet_hours": [1, 7],
    # If FB shows a verification / suspicious-activity / checkpoint screen,
    # this phone is paused for this many hours before we try again.
    "flag_cooldown_hours": 48,
    # Small chance during browsing of a scroll-back / stray back button /
    # mis-tap. Real users are not perfectly forward-only.
    "random_mistakes": {"enabled": True,
                        "scroll_back_prob": 0.05,
                        "back_button_prob": 0.03,
                        "mistap_prob": 0.02}
}

_print_lock = threading.Lock()

# Per-phone set of accounts already used in the current rotation, so we cycle
# through EVERY saved account (reset once we've been through them all).
_ACCOUNT_USED = {}
_ACCOUNT_LOCK = threading.Lock()


def _open_switcher(serial, d, pkg):
    """Open FB Lite's saved-account switcher and return True if the account list
    is showing. Uses fbdaily's robust `open_feed` (monkey LAUNCHER) instead of
    the reactor's `am start .MainActivity`, which fails to launch on some phones
    (they were left on the launcher)."""
    w, h = screen_size(serial)
    for _ in range(3):
        if stop_requested():
            return False
        open_feed(serial, pkg)                        # robust launch
        if fb.wait_for_account_block(d, timeout=5):   # login / suspended screen
            return False
        if fb.account_rows(d):
            return True
        tap(serial, int(w * 0.914), int(h * 0.074))   # hamburger / top-right menu
        time.sleep(random.uniform(1.8, 4.2) + random.random() * 0.5)
        if fb.account_rows(d):
            return True
        arrow = fb.find_switch_account_arrow(d)
        if arrow is not None:
            d.click(*arrow)
            time.sleep(random.uniform(1.8, 4.2) + random.random() * 0.5)
            if fb.account_rows(d):
                return True
        press_back(serial)
        time.sleep(random.uniform(0.7, 2.2) + random.random() * 0.5)
    return False


def rotate_account(serial, d, cfg):
    """Switch this phone to its NEXT saved FB account, cycling through ALL of
    them (every account gets a turn). Returns the new account name, or None."""
    if fb is None or d is None or not cfg.get("switch_accounts", True):
        return None
    if not getattr(fb, "SUPPORTS_ACCOUNT_SWITCH", True):
        return None
    pkg = cfg.get("package", "com.facebook.lite")
    with _ACCOUNT_LOCK:
        used = _ACCOUNT_USED.setdefault(serial, set())
    try:
        if not _open_switcher(serial, d, pkg):
            log(serial, "account switcher not reachable (single account or "
                        "login screen)")
            return None
        rows = fb.account_rows(d)
        if not rows or len(rows) < 2:
            return None                               # only one account -> stay put
        current = rows[0]["name"].lower()             # rows[0] = current account
        used.add(current)
        # Next account not yet used this cycle...
        target = next((r for r in rows[1:] if r["name"].lower() not in used), None)
        if target is None:
            # ...went through them all -> loop back and keep switching.
            used.clear()
            used.add(current)
            log(serial, "cycled all accounts; looping rotation")
            target = next((r for r in rows[1:]
                           if r["name"].lower() != current), None)
        if target is None:
            return None
        name = target["name"]
        log(serial, f"switching account -> {name}")
        d.click(*target["point"])
        time.sleep(6)
        if fb.chooser_present(serial, d):
            fb.dismiss_chooser(serial, d)
            time.sleep(2)
        used.add(name.lower())
        block = fb.wait_for_account_block(d, timeout=8)
        if block:
            log(serial, f"account {name}: {block}")
            return None
        return name
    except Exception as e:
        log(serial, f"account switch error: {e}")
        return None

# ── stop mechanism ─────────────────────────────────────────────────────────
# The tool runs FOREVER (session after session) until you stop it. Stop with
# Ctrl+C, or by creating a file named STOP in this folder (works even when it's
# running in the background / detached).
STOP = threading.Event()
STOP_FILE = os.path.join(HERE, "STOP")


def stop_requested():
    return STOP.is_set() or os.path.exists(STOP_FILE)


def interruptible_sleep(seconds):
    """Sleep, but wake immediately if a stop was requested."""
    end = time.time() + seconds
    while not stop_requested():
        left = end - time.time()
        if left <= 0:
            break
        time.sleep(min(0.5, left))


def log(serial, msg):
    with _print_lock:
        print(f"  [{serial}] {msg}", flush=True)


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))   # deep copy
    path = os.path.join(HERE, "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user = json.load(f)
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            print(f"  [config] ignoring config.json ({e})")
    return cfg


# ── Human-behavior state (warm-up, rest days, verification cooldown) ───────
# Everything the anti-detection layer needs sits on disk so it survives
# restarts: how long we've known this phone (drives the warm-up curve), when
# it has run sessions (drives the daily-cap + time-varying jitter) and any
# 'flagged_until' cooldown from a verification / suspicious-activity screen.
STATE_FILE = os.path.join(HERE, "state.json")
_STATE_LOCK = threading.Lock()


def _load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def _device_state(state, serial):
    """Read-side accessor: fill in defaults but don't save. Callers save only
    when they've actually mutated something (session recorded, flag set)."""
    dev = state.setdefault(serial, {})
    dev.setdefault("first_seen", time.time())
    dev.setdefault("sessions", [])
    dev.setdefault("flagged_until", 0.0)
    dev.setdefault("flag_log", [])
    return dev


def _prune_old_sessions(dev, days=30):
    cutoff = time.time() - days * 86400
    dev["sessions"] = [t for t in dev["sessions"] if t >= cutoff]


def account_age_days(serial):
    """Days since this phone first ran the tool. Persists `first_seen` on the
    very first observation so the warm-up count doesn't reset on every run."""
    with _STATE_LOCK:
        state = _load_state()
        existed = serial in state
        dev = _device_state(state, serial)
        if not existed:
            _save_state(state)
    return max(0.0, (time.time() - dev["first_seen"]) / 86400.0)


def warmup_multiplier(serial, cfg):
    """0.15..1.0 — new phones move very slowly the first days, ramping up over
    ~2 weeks. Applied to session length, engage/add/confirm weights, react/
    comment/share probabilities and friend batch sizes."""
    wu = cfg.get("warmup", {}) or {}
    if not wu.get("enabled", True):
        return 1.0
    days = account_age_days(serial)
    warmup_days = max(1.0, float(wu.get("warmup_days", 14)))
    start_pct = max(0.05, float(wu.get("start_activity_pct", 15)) / 100.0)
    if days >= warmup_days:
        return 1.0
    t = days / warmup_days
    return start_pct + (1.0 - start_pct) * (t ** 1.4)   # slow start, fast finish


def is_rest_day(serial, cfg):
    """Whether TODAY is a rest day for this phone. Deterministic per phone per
    calendar week so the same weekday is 'rest' all day (not flipping on/off)
    and different phones rest on different days."""
    lo, hi = cfg.get("rest_days_per_week", [1, 2])
    cseed = int(hashlib.md5(f"count-{serial}".encode()).hexdigest(), 16) & 0xffffffff
    target = random.Random(cseed).randint(int(lo), int(hi))
    week_key = f"{serial}-{time.strftime('%G-%V')}"
    seed = int(hashlib.md5(week_key.encode()).hexdigest(), 16) & 0xffffffff
    r = random.Random(seed)
    rest_days = set(r.sample(range(7), min(target, 7)))
    return time.localtime().tm_wday in rest_days


def in_quiet_hours(cfg):
    """Local hour is inside a 'sleep' window (default 1am-7am). Handles a
    wrap-around window like [23, 6]."""
    q = cfg.get("quiet_hours", [1, 7])
    if not q or len(q) != 2:
        return False
    hour = time.localtime().tm_hour
    start, end = int(q[0]), int(q[1])
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def sessions_today(serial):
    today = time.strftime("%Y-%m-%d")
    with _STATE_LOCK:
        state = _load_state()
        dev = _device_state(state, serial)
    return sum(1 for t in dev["sessions"]
               if time.strftime("%Y-%m-%d", time.localtime(t)) == today)


def daily_session_cap(serial, cfg):
    """Max sessions today. Warm-up and rest-day scale this DOWN, so brand-new
    phones do 1-2 sessions/day and rest days cap at 1-2 regardless."""
    lo, hi = cfg.get("max_sessions_per_day", [3, 8])
    key = f"{serial}-{time.strftime('%Y-%m-%d')}"
    seed = int(hashlib.md5(key.encode()).hexdigest(), 16) & 0xffffffff
    base = random.Random(seed).randint(int(lo), int(hi))
    if is_rest_day(serial, cfg):
        return max(1, min(2, base))
    m = warmup_multiplier(serial, cfg)
    return max(1, int(round(base * m)))


def record_session_start(serial):
    with _STATE_LOCK:
        state = _load_state()
        dev = _device_state(state, serial)
        dev["sessions"].append(time.time())
        _prune_old_sessions(dev)
        _save_state(state)


def flag_cooldown_remaining(serial):
    with _STATE_LOCK:
        state = _load_state()
        dev = _device_state(state, serial)
    return max(0.0, dev.get("flagged_until", 0.0) - time.time())


def mark_flagged(serial, cfg, reason=""):
    """FB showed us a verification / suspicious-activity / checkpoint screen.
    Stop this phone for `flag_cooldown_hours` — anything we do while flagged
    digs the account in deeper."""
    hours = float(cfg.get("flag_cooldown_hours", 48))
    with _STATE_LOCK:
        state = _load_state()
        dev = _device_state(state, serial)
        dev["flagged_until"] = time.time() + hours * 3600
        dev["flag_log"].append({"time": time.time(), "reason": str(reason)[:200]})
        _save_state(state)
    log(serial, f"FLAGGED for {hours:.0f}h — {reason}")


# ── Verification / suspicious-activity detection ──────────────────────────
# FB screens that mean "we noticed something; verify or wait". If any of these
# appear we abort the session and put the phone into a cooldown.
_VERIFICATION_SIGNALS = (
    "we've detected suspicious", "suspicious activity",
    "please confirm your identity", "confirm your identity",
    "verify your account", "verify your identity", "verify it's you",
    "let's confirm it's you",
    "your account has been temporarily", "your account is temporarily",
    "temporarily blocked", "you're temporarily blocked", "you are temporarily blocked",
    "we suspended", "account restricted", "account disabled",
    "checkpoint required", "unusual login", "protect your account",
    "enter the code we sent", "two-factor authentication",
    "we limit how often", "you can't use this feature",
)


def detect_verification(d):
    """Return the matched warning phrase if a verification screen is showing,
    otherwise ''."""
    if d is None or fb is None:
        return ""
    try:
        if fb.has_nav(d):          # normal FB pages (feed / friends / chats) are never a checkpoint
            return ""
        vis = (fb.visible_text(d) or "").lower()
    except Exception:
        return ""
    for s in _VERIFICATION_SIGNALS:
        if s in vis:
            return s
    return ""


# ── Session-level scaling + human-imperfection helpers ────────────────────
def _scale_cfg_for_session(cfg, mult, rest):
    """Return a copy of cfg with all the 'risky' knobs scaled down by `mult`
    (and, on a rest day, outreach zeroed entirely). Session length shrinks,
    engagement probabilities drop, friend batches shrink, etc."""
    scaled = json.loads(json.dumps(cfg))
    sm = scaled.get("session_minutes", [5, 12])
    lo = max(1.0, sm[0] * mult)
    hi = max(lo + 0.5, sm[1] * mult)
    scaled["session_minutes"] = [lo, hi]
    asm = scaled.get("account_switch_minutes")
    if asm:
        scaled["account_switch_minutes"] = max(2, int(asm * mult))
    w = scaled.setdefault("weights", {})
    for key in ("engage", "add_friends", "confirm_requests"):
        if key in w:
            # an activity switched off (weight 0) stays off; others never drop below 1
            w[key] = 0 if (rest or w[key] <= 0) else max(1, int(w[key] * mult))
    ec = scaled.setdefault("engage", {})
    for key in ("react_prob", "share_prob", "comment_prob"):
        if key in ec:
            ec[key] = max(0.0, float(ec[key]) * mult)
    for key in ("add_friends_range", "confirm_range"):
        if key in ec and isinstance(ec[key], list) and len(ec[key]) == 2:
            r_lo, r_hi = ec[key]
            ec[key] = [max(1, int(round(r_lo * mult))),
                       max(1, int(round(r_hi * mult)))]
    if rest:
        # Rest day: essentially just scroll a little. Disable outreach entirely
        # so this phone looks like a person who just barely opened FB today.
        for key in ("engage", "add_friends", "confirm_requests"):
            w[key] = 0
        w["scroll"] = int(w.get("scroll", 30) * 1.6)
    return scaled


def _human_mistake(serial, w, h, cfg):
    """One rare human-imperfection nudge — a scroll-back to re-read, a stray
    back-button, or a fat-fingered mis-tap we immediately back out of. Called
    inside the browsing loop so activity isn't perfectly forward-only (a common
    bot tell)."""
    rm = cfg.get("random_mistakes", {}) or {}
    if not rm.get("enabled", True):
        return
    r = random.random()
    sb = float(rm.get("scroll_back_prob", 0.05))
    bb = float(rm.get("back_button_prob", 0.03))
    mt = float(rm.get("mistap_prob", 0.02))
    if r < sb:
        scroll_back(serial, w, h)
        time.sleep(random.uniform(0.6, 1.8))
    elif r < sb + bb:
        press_back(serial)
        time.sleep(random.uniform(0.4, 1.2))
    elif r < sb + bb + mt:
        x = int(w * random.uniform(0.3, 0.7))
        y = int(h * random.uniform(0.45, 0.65))
        swipe(serial, x, y, x + random.randint(-15, 15), y - random.randint(20, 60), random.randint(150, 300))
        time.sleep(random.uniform(0.5, 1.5))


# ── ADB helpers ────────────────────────────────────────────────────────────
def adb(serial, *args, timeout=30):
    try:
        return subprocess.run([ADB, "-s", serial, *args],
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        class _R:
            returncode, stdout, stderr = 1, "", "timeout"
        return _R()


def sh(serial, *cmd, timeout=30):
    return adb(serial, "shell", *cmd, timeout=timeout)


def usb_devices():
    """Serials of USB-connected, authorized devices (wireless ip:port skipped)."""
    try:
        out = subprocess.run([ADB, "devices"], capture_output=True,
                             text=True, timeout=20).stdout
    except Exception:
        return []
    devs = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device" and ":" not in parts[0]:
            devs.append(parts[0])
    return devs


def screen_size(serial):
    m = re.search(r"(\d+)x(\d+)", sh(serial, "wm", "size").stdout or "")
    return (int(m.group(1)), int(m.group(2))) if m else (720, 1600)


def wake(serial):
    sh(serial, "input", "keyevent", "KEYCODE_WAKEUP")
    time.sleep(1.0)
    w, h = screen_size(serial)
    sh(serial, "input", "swipe", str(w // 2), str(int(h * 0.82)),
       str(w // 2), str(int(h * 0.20)), "300")
    time.sleep(0.6)
    sh(serial, "input", "keyevent", "82")
    sh(serial, "svc", "power", "stayon", "true", "usb")
    time.sleep(0.5)


def tap(serial, x, y):
    x += random.randint(-8, 8)
    y += random.randint(-8, 8)
    if random.random() < 0.1:            # small pause before ~1-in-10 taps
        time.sleep(random.uniform(0.3, 1.2))
    # Emulate a held tap with tiny finger drift via a near-zero swipe — Android's
    # `input tap` has no duration, but `input swipe` does.
    hold_ms = int(random.uniform(0.05, 0.25) * 1000)
    sh(serial, "input", "swipe",
       str(int(x)), str(int(y)),
       str(int(x + random.randint(-3, 3))), str(int(y + random.randint(-3, 3))),
       str(hold_ms))


def swipe(serial, x1, y1, x2, y2, dur=None):
    """Two-segment curved swipe — real fingers don't move in a perfect straight
    line, so bend the path through a jittered midpoint and split the duration."""
    if dur is None:
        dur = random.randint(300, 800)
    mid_x = (x1 + x2) // 2 + random.randint(-30, 30)
    mid_y = (y1 + y2) // 2 + random.randint(-40, 40)
    d1 = max(80, dur // 2 + random.randint(-30, 30))
    d2 = max(80, dur - d1 // 2)
    segments = [
        (x1, y1, mid_x, mid_y, d1),
        (mid_x, mid_y, x2, y2, d2),
    ]
    for sx, sy, ex, ey, seg_dur in segments:
        sh(serial, "input", "swipe",
           str(int(sx)), str(int(sy)),
           str(int(ex)), str(int(ey)),
           str(int(seg_dur)))
        time.sleep(random.uniform(0.05, 0.15))


def press_back(serial):
    sh(serial, "input", "keyevent", "KEYCODE_BACK")


def pause(min_sec, max_sec, extra_skew=0.3):
    """Human pause: actually waits (interruptible by STOP) and returns the seconds waited."""
    base = random.uniform(min_sec, max_sec)
    if random.random() < 0.15:
        base *= random.uniform(1.5, 4.0)
    base += random.uniform(-extra_skew, extra_skew)
    base = max(0.5, base)
    interruptible_sleep(base)
    return base

# ── Browsing ───────────────────────────────────────────────────────────────
def open_feed(serial, pkg):
    sh(serial, "am", "force-stop", pkg)
    time.sleep(1.5)
    sh(serial, "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1",
       timeout=25)
    time.sleep(random.uniform(5, 8))


def foreground_pkg(serial):
    """Package of the app currently in the foreground (best-effort)."""
    out = (sh(serial, "dumpsys", "window").stdout or "")
    m = re.search(r"mCurrentFocus=[^\n]*\bu0\s+([a-zA-Z][\w.]+)/", out)
    if not m:
        m = re.search(r"mCurrentFocus=[^\n]*\s([a-zA-Z][\w.]+)/[\w.$]*\}", out)
    if not m:
        m = re.search(r"mFocusedApp=[^\n]*\s([a-zA-Z][\w.]+)/", out)
    return m.group(1) if m else ""


def ensure_fb(serial, pkg):
    """If the phone has drifted out of FB Lite (a tapped link opened a browser,
    another app came up, or it went to the home screen), bring FB Lite back.
    Returns True if it had to reopen."""
    cur = foreground_pkg(serial)
    if not cur or cur == pkg:
        return False
    log(serial, f"left FB Lite (foreground={cur}) -> reopening")
    # if we're in an in-app / external browser opened from a link, back out first
    for _ in range(2):
        press_back(serial)
        time.sleep(0.7)
        if foreground_pkg(serial) == pkg:
            return True
    sh(serial, "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1",
       timeout=20)
    time.sleep(random.uniform(3, 5))
    return True


def scroll_feed(serial, w, h):
    if random.random() < 0.15:
        distance = random.randint(30, 120)
    else:
        distance = random.randint(150, 400)
    duration = int(200 + (distance / 500) * 300 + random.randint(-50, 100))
    x = int(w * random.uniform(0.35, 0.65))
    y_start = int(h * random.uniform(0.65, 0.80))
    y_end = y_start - distance
    swipe(serial, x, y_start, x + random.randint(-20, 20), y_end, duration)
    if random.random() < 0.2:
        time.sleep(random.uniform(0.5, 3.0))


def scroll_back(serial, w, h):
    x = int(w * random.uniform(0.40, 0.60))
    swipe(serial, x, int(h * random.uniform(0.30, 0.42)),
          x, int(h * random.uniform(0.62, 0.74)), random.randint(300, 600))


def open_post(serial, w, h, cfg):
    """Open the comments of a post on screen, read a little, come back. (Never a blind tap — on
    FB Lite a blind feed tap can hit Confirm / Add friend in a carousel.)"""
    d = _d(serial)
    rows = fb.action_rows(d) if (d is not None and fb is not None) else []
    rows = [r for r in rows if h * 0.2 < r["y1"] < h * 0.85]
    if not rows:
        scroll_feed(serial, w, h)
        return
    cx, cy = fb.center(rows[0]["cells"][1])
    tap(serial, cx, cy)
    pause(*cfg["read_pause_seconds"])
    if random.random() < 0.5:
        swipe(serial, w // 2, int(h * 0.62), w // 2, int(h * 0.42), random.randint(300, 500))
        pause(1.5, 4.0)
    press_back(serial)
    time.sleep(1.0)
    if fb.find_phrase(fb.ocr(d)[0], "write a comment"):
        press_back(serial)
        time.sleep(1.0)


def check_notifications(serial, w, h, cfg):
    _nav(serial, 4, w, h)
    pause(*cfg["read_pause_seconds"])
    if random.random() < 0.5:
        scroll_feed(serial, w, h)
        pause(1.5, 4.0)
    _go_home(serial, w, h)


def watch_reels(serial, w, h, cfg):
    _nav(serial, 3, w, h)
    time.sleep(random.uniform(3, 6))
    for _ in range(random.randint(2, 5)):
        if stop_requested():
            break
        swipe(serial, w // 2, int(h * 0.75), w // 2, int(h * 0.25),
              random.randint(180, 320))
        pause(*cfg["watch_pause_seconds"])
    _go_home(serial, w, h)


# ── Friends (add / confirm) ──────────────────────────────────────────────
# The Friends UI is custom-rendered (no a11y labels), so this is coordinate
# based: the blue action button (Add friend / Confirm) is the LEFT button of
# each row; "Remove" / "Delete" is on the right. Tuned for 720-wide screens.
def _open_friends_tab(serial, w, h):
    _nav(serial, 1, w, h)                          # Friends tab
    time.sleep(random.uniform(2.5, 4))


def confirm_requests(serial, w, h, cfg):
    """Confirm pending friend requests on the Friends tab. Each 'Confirm' is found by its text on
    a solid-blue button (never 'Delete'), so nothing else gets tapped."""
    d = _d(serial)
    if d is None or fb is None:
        log(serial, "confirm_requests: screen reader unavailable — skipped")
        return 0
    ec = cfg["engage"]
    target = random.randint(*ec.get("confirm_range", [1, 5]))
    _open_friends_tab(serial, w, h)
    n = 0
    for _ in range(3):                              # a couple of screens at most
        hits = [p for p in fb.blue_button_hits(d, "Confirm") if h * 0.15 < p[1] < h * 0.93]
        if not hits:
            break
        for x, y in hits:
            if n >= target or stop_requested():
                break
            tap(serial, x, y)
            n += 1
            time.sleep(random.uniform(1.8, 3.2))
        if n >= target:
            break
        swipe(serial, w // 2, int(h * 0.78), w // 2, int(h * 0.42), random.randint(350, 550))
        time.sleep(random.uniform(1.5, 2.5))
    log(serial, f"confirm_requests: confirmed {n} request(s)")
    _go_home(serial, w, h)
    return n


def add_friends(serial, w, h, cfg):
    """Send a few friend requests from 'People You May Know' (Friends tab, else the feed's
    PYMK cards). Only taps buttons whose text reads exactly 'Add friend'."""
    d = _d(serial)
    if d is None or fb is None:
        log(serial, "add_friends: screen reader unavailable — skipped")
        return 0
    ec = cfg["engage"]
    target = random.randint(*ec.get("add_friends_range", [2, 5]))
    sent = 0

    def tap_visible():
        nonlocal sent
        hits = [p for p in fb.text_hits(d, "Add friend") if h * 0.15 < p[1] < h * 0.93]
        for x, y in hits:
            if sent >= target or stop_requested():
                return bool(hits)
            tap(serial, x, y)
            sent += 1
            time.sleep(random.uniform(1.8, 3.2))
        return bool(hits)

    _open_friends_tab(serial, w, h)
    for _ in range(4):
        if sent >= target or stop_requested():
            break
        tap_visible()
        swipe(serial, w // 2, int(h * 0.80), w // 2, int(h * 0.34), random.randint(350, 550))
        time.sleep(random.uniform(1.5, 2.5))
    if sent < target and not stop_requested():     # the feed's 'People You May Know' carousel
        _go_home(serial, w, h)
        for _ in range(8):
            if sent >= target or stop_requested():
                break
            hits = [p for p in fb.text_hits(d, "Add friend") if h * 0.15 < p[1] < h * 0.93]
            if hits:
                tap_visible()
                if sent < target:                  # more cards: swipe the carousel left
                    y = hits[0][1] - int(h * 0.15)
                    swipe(serial, int(w * 0.85), y, int(w * 0.15), y, random.randint(300, 450))
                    time.sleep(random.uniform(1.2, 2.0))
                    tap_visible()
                break
            swipe(serial, w // 2, int(h * 0.80), w // 2, int(h * 0.38), random.randint(350, 550))
            time.sleep(random.uniform(1.2, 2.2))
    log(serial, f"add_friends: sent {sent} request(s)")
    _go_home(serial, w, h)
    return sent


# ── Messenger auto-reply (free AI, real conversations) ──────────────────────
# FB Lite's MESSENGER (unlike its news feed) is accessible — the chat list and
# open threads are in the a11y tree. So this reads the list for UNREAD
# conversations, opens each, reads the thread, and asks the free Pollinations
# AI for a natural next reply (with a per-person tone: friendly/flirty/etc).
# Because the other person's next reply arrives as a new unread, the
# conversation keeps building across sessions — on every phone.
_CHAT_UI = (
    "messages", "create pin", "create your pin", "your note", "share a note",
    "active now", "view profile", "end-to-end", "learn more", "message",
    "you're now friends", "messages and calls", "seen", "delivered", "typing",
    "sent an attachment", "sent a photo", "sent a sticker", "you can now message",
    "add to story", "tap to", "search", "reply", "forwarded",
    "restore", "enter your pin", "end-to-end", "view profile", "lives in", "new facebook profile",
    "active", "learn more", "previews", "message seen", "you're friends on facebook", "works at",
    "studied at", "went to",
)


def _txt_nodes(d):
    """[(text, x1, cy)] of on-screen text nodes from the a11y tree."""
    try:
        root = fb.hierarchy_root(d)
    except Exception:
        root = None
    out = []
    if root is None:
        return out
    for n in root.iter("node"):
        t = (n.attrib.get("text") or "").strip()
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", n.attrib.get("bounds", ""))
        if t and m:
            x1, y1, x2, y2 = map(int, m.groups())
            out.append((t, x1, (y1 + y2) // 2))
    return out


def read_unread_convs(serial, d, w, h):
    """Unread conversations in the Messenger list: [(name, tap_y)] (FB Lite labels each row
    'Unread, <name>, <preview>' in the accessibility tree)."""
    try:
        return [(name, y) for name, _x, y in fb.unread_chats(d)]
    except Exception:
        return []


def read_thread(serial, d, w, h, name=""):
    """Recent messages in the open thread: [(mine, text)] oldest->newest.
    Theirs = a text bubble on the left next to the sender's profile photo (FB Lite labels it
    'Profile photo of <name>'); mine = a text bubble on the right. Header cards ('Request sent',
    'View profile'), encryption notices, timestamps and 'Couldn't send.' are not messages."""
    try:
        allnodes = fb.nodes(d)
    except Exception:
        return []
    photos = [(y1, y2) for x1, y1, x2, y2, ck, t, ds, c in allnodes
              if ds.lower().startswith("profile photo of") and x1 < w * 0.2]
    composer_top = min((y1 for x1, y1, x2, y2, ck, t, ds, c in allnodes if "EditText" in c), default=int(h * 0.88))
    nm = name.lower().strip()
    msgs = []
    for x1, y1, x2, y2, ck, t, ds, c in allnodes:
        t = (t or "").strip()
        if not t or y1 < h * 0.08 or y2 > composer_top or len(t) < 2:
            continue
        low = t.lower()
        if any(k in low for k in _CHAT_UI) or (nm and low == nm):
            continue
        if low in ("request sent", "couldn't send.", "couldn't send", "sending...", "sent", "add friend",
                   "say hi", "wave"):
            continue
        if (re.fullmatch(r"[\d:\sapm.,APM]+", t) or re.fullmatch(r"\d+\s?(s|m|h|d|w|y|min|hr|hrs)", low)
                or re.match(r"(mon|tue|wed|thu|fri|sat|sun)\b", low)):
            continue
        mine = x1 > w * 0.42
        if not mine:
            # theirs only if a profile photo sits beside / just below this bubble group
            if not any(py1 <= y2 + 400 and py2 >= y1 - 40 for py1, py2 in photos):
                continue
        msgs.append((y1, mine, t))
    msgs.sort()
    return [(mine, t) for _y, mine, t in msgs][-8:]


# ── Philippine-language awareness (fbdaily only): Tagalog, Bisaya, Kapampangan ──
# Distinctive marker words only (avoid tokens that also occur in English/Tagalog).
_CEB_MARKERS = {
    "kaayo", "nindot", "nindota", "kanindot", "gwapa", "gwapo", "gwapaha", "lami",
    "gyud", "jud", "unsa", "bitaw", "palihug", "gihapon", "diay", "murag",
    "ganahan", "kadlaw", "lingaw", "makalingaw", "ngano", "kinsa", "walay",
    "naay", "maau", "kuan", "pagkaon", "gimingaw", "gipangita", "naa",
}
_PAM_MARKERS = {
    "mayap", "masanting", "kaluguran", "mangan", "makanyan", "dakal", "masalese",
    "nanu", "keni", "keta", "ikayu", "balamu", "meto", "ustu", "pota", "kaka",
    "malagu", "eku", "atlu", "pisan", "sarian", "komusta",
}


def _detect_ph_lang(text):
    """fbdaily language family incl. Bisaya + Kapampangan:
    returns 'pam', 'ceb', 'tl', 'en', or 'other'."""
    t = (text or "").strip().lower()
    if not t:
        return "tl"
    words = set(re.findall(r"[a-z']+", t))
    if words & _PAM_MARKERS:
        return "pam"
    if words & _CEB_MARKERS:
        return "ceb"
    return fb._detect_lang(text) if fb else "tl"


def _lang_name(code):
    return {
        "pam": "Kapampangan",
        "ceb": "Bisaya (Cebuano)",
        "tl":  "Tagalog/Taglish (the same Tagalog-English mix)",
        "en":  "the exact same language as the text (do not translate it)",
        "other": "the exact same language as the text (do not translate it)",
    }.get(code, "the exact same language as the text")


# ── Human typos in outgoing text (chat / comment / share caption) ────────────
# Real users on mobile don't type perfectly. Injects 1-2 tiny, natural typos
# into ~40% of messages. Structural typos only (adjacent-key slip, dropped
# letter, doubled letter, transposition, dropped space/punct) so it works in
# any language (Tagalog / Bisaya / Kapampangan / English) without changing
# meaning. Short messages (< 10 chars) are left alone.
_TYPO_QWERTY = {
    'a':'sqw','b':'vn','c':'xvd','d':'sfe','e':'wrd','f':'dgr','g':'fhv',
    'h':'gjb','i':'uok','j':'hkn','k':'jlm','l':'ko','m':'nk','n':'bmj',
    'o':'ipl','p':'ol','q':'wa','r':'et','s':'adw','t':'ry','u':'yi',
    'v':'cb','w':'qes','x':'zc','y':'tu','z':'xs',
}


def _maybe_add_typos(text, prob=0.40):
    """Return `text`, but with ~40% probability inject 1-2 natural typos so
    outgoing messages don't look machine-perfect. Never mangles emoji, never
    touches messages shorter than 10 chars, and never applies more than a
    couple of tweaks."""
    if not text or len(text.strip()) < 10:
        return text
    if random.random() > prob:
        return text
    out = list(text)
    n_typos = random.choices([1, 2], weights=[7, 3], k=1)[0]
    for _ in range(n_typos):
        letters = [i for i, ch in enumerate(out) if ch.isalpha() and ord(ch) < 128]
        spaces = [i for i, ch in enumerate(out) if ch == ' ']
        if not letters:
            break
        kind = random.choices(
            ['adj', 'drop', 'double', 'transpose', 'drop_punct', 'no_space'],
            weights=[4, 3, 2, 2, 1, 1], k=1)[0]
        if kind == 'adj':
            i = random.choice(letters)
            ch = out[i].lower()
            if ch in _TYPO_QWERTY:
                new = random.choice(_TYPO_QWERTY[ch])
                out[i] = new.upper() if out[i].isupper() else new
        elif kind == 'drop' and len(out) > 6:
            del out[random.choice(letters)]
        elif kind == 'double':
            i = random.choice(letters)
            out.insert(i, out[i])
        elif kind == 'transpose':
            pairs = [i for i in letters
                     if i + 1 < len(out) and out[i + 1].isalpha() and ord(out[i + 1]) < 128]
            if pairs:
                i = random.choice(pairs)
                out[i], out[i + 1] = out[i + 1], out[i]
        elif kind == 'drop_punct':
            for j in range(len(out) - 1, -1, -1):
                if out[j] in '.!?,':
                    del out[j]
                    break
        elif kind == 'no_space' and spaces:
            del out[random.choice(spaces)]
    return ''.join(out)


# ── Post-accurate reactions + comments (fbdaily only) ────────────────────────
# Sentiment lexicon (multilingual) -> the reaction that fits a post. Strong
# negative/funny signals win; otherwise fall back to the human weighted mix.
_SENTIMENT = [
    ("sad", ["namatay", "patay", "yumao", "nasawi", "condolence", "condolences",
             "rip", "nawala", "malungkot", "lungkot", "nagluksa", "namatayan",
             "died", "passed away", "death", "ospital", "hospital", "nag-antos",
             "kamatayan", "grief", "nagdadalamhati", "namatyan"]),
    ("angry", ["scam", "corrupt", "corruption", "kurakot", "kawatan", "magnanakaw",
               "injustice", "nakakagalit", "nakalagot", "buwisit", "walanghiya",
               "walang hiya", "anomalya", "pandarambong", "abuso",
               "napakawalanghiya"]),
    ("haha", ["haha", "hahaha", "lol", "lmao", "nakakatawa", "katawa", "lingaw",
              "kadlaw", "joke", "funny", "charot", "petmalu", "nakakaloka"]),
    ("wow", ["congrats", "congratulations", "wow", "amazing", "achievement",
             "milestone", "nakakamangha", "proud", "topnotch", "board exam",
             "passed the", "nakapasa", "graduate", "graduated", "champion",
             "winner", "record"]),
    ("love", ["happy birthday", "birthday", "anniversary", "monthsary", "wedding",
              "kinasal", "engaged", "newborn", "bagong silang", "love", "mahal",
              "sweet", "blessed", "in love", "sana all", "adorable", "cute",
              "gwapa", "gwapo", "ganda", "gorgeous", "thankful", "grateful"]),
]


def _reaction_for_post(post_text, cfg):
    """Reaction that fits the post's sentiment; weighted-random if unclear."""
    t = (post_text or "").lower()
    if t:
        for reaction, kws in _SENTIMENT:
            if any(k in t for k in kws):
                return reaction
    return _pick_reaction(cfg)


_POST_UI_JUNK = (
    "like", "comment", "share", "reply", "write a comment", "view more comments",
    "most relevant", "all comments", "see more", "see translation", "follow",
    "reels", "home", "search", "what's on your mind", "story", "menu", "send",
    "marketplace", "notifications", "sponsored", "suggested", "top fan", "author",
    "edited", "active now", "people you may know", "add friend", "message", "watch",
)
# status-bar / system chrome that shows up in the a11y dump but is NOT a caption
_STATUSBAR = (
    "notification", "battery", "charging", "wifi", "signal", "bars", "percent",
    "vibrate", "ringer", "silent", "bluetooth", "mobile data", "hotspot",
    "airplane", "screenshot", "do not disturb", "calling", "on facebook lite",
    # system dialogs / chrome that can appear over the feed (never a caption)
    "google account", "usb debugging", "use without", "sign in", "choose an account",
    "add account", "tap to turn", "developer options", "select input method",
)


def _extract_post_text(vis):
    """Best-effort: pull the post's caption out of the a11y text blob (the first
    substantial, sentence-like line that isn't UI chrome or status-bar text)."""
    if not vis:
        return ""
    for raw in vis.split("\n"):
        ln = raw.strip()
        low = ln.lower()
        if len(ln) < 25 or " " not in ln:        # captions are sentences
            continue
        if ln.endswith(":"):                      # status-bar descriptors ("X notification:")
            continue
        if any(j in low for j in _STATUSBAR):
            continue
        if any(j in low for j in _POST_UI_JUNK):
            continue
        if re.fullmatch(r"[\d\s.,:%kmb+]+", low):
            continue
        return ln[:400]
    return ""


POST_COMMENT_SYSTEM = (
    "You are a real person leaving a comment on a Facebook post. Your comment "
    "MUST make sense as a direct reaction to THIS specific post — its exact "
    "topic, situation, and details.\n\n"
    "RULES:\n"
    "1. Read the whole post carefully. Your comment must clearly reference what "
    "the post is actually about (the news, the photo, the announcement, the "
    "joke — whatever it is). A stranger reading only your comment should be "
    "able to guess roughly what the post said.\n"
    "2. NEVER a generic canned line like 'nice', 'wow', 'ganda', 'congrats' by "
    "itself — always tie it to the specific content of the post.\n"
    "3. If the post is sad/tragic, be respectful. If it's happy news, be warm. "
    "If it's a joke, joke back on the same joke.\n"
    "4. LANGUAGE: write ONLY in the language you are told to use; never switch "
    "languages or translate.\n\n"
    "Reply with ONLY the comment text: one short, natural line — no quotes, no "
    "labels, no hashtags, no emoji unless it clearly fits."
)


def _comment_for_post(post_text, cfg):
    """AI comment accurate/relevant to THIS post, in the post's language (incl.
    Bisaya / Kapampangan). Returns '' so the caller can fall back."""
    if fb is None or not post_text:
        return ""
    lang_name = _lang_name(_detect_ph_lang(post_text))
    prompt = (
        "Facebook post (between the triple quotes):\n"
        f'"""{post_text}"""\n\n'
        f"Write ONE short, natural Facebook comment reacting to THIS post, in "
        f"{lang_name}. It must clearly relate to what the post is about and read "
        "like a real person. Keep it casual and one line. Reply with only the "
        "comment text."
    )
    try:
        return fb.sanitize_comment(fb.ai_reply(prompt, system=POST_COMMENT_SYSTEM), "")
    except Exception:
        return ""


CHAT_SYSTEM = (
    "You are a real person chatting on Facebook Messenger. Your job is to "
    "reply to the OTHER person's most recent message — specifically to THAT "
    "message, in a way that makes clear sense as the next line in the "
    "conversation.\n\n"
    "RULES:\n"
    "1. Read the WHOLE conversation before replying so you have the context.\n"
    "2. Your reply must clearly RESPOND to what they just said:\n"
    "   - If they asked a question, ANSWER it (don't dodge or change topic).\n"
    "   - If they told you news, react to that specific news.\n"
    "   - If they made a joke, joke back on the same joke.\n"
    "   - If they said something ambiguous, ask a natural follow-up.\n"
    "3. NEVER a generic or canned line by itself ('okay', 'yes', 'haha', "
    "'nice', 'sure', 'ganda') — always tie the reply to what they actually "
    "said. A stranger reading only your line should be able to guess what "
    "they said just before.\n"
    "4. NEVER repeat something you already said earlier in this conversation.\n"
    "5. LANGUAGE: reply in the SAME language and mix they are using — Taglish "
    "-> Taglish, English -> English, Bisaya -> Bisaya, Kapampangan -> "
    "Kapampangan. Never switch languages mid-conversation.\n"
    "6. Keep it short and natural — one line, like real texting.\n\n"
    "Reply with ONLY your next message text — no quotes, no labels, no "
    "narration, no emoji unless the other person uses emoji."
)


def generate_chat_reply(name, thread, cfg):
    """Free-AI next reply that answers the person's latest message in a tone."""
    if fb is None or not thread:
        return ""
    cc = cfg.get("chat", {})
    persona = cc.get("persona", "a normal Filipino person")
    styles = cc.get("styles") or ["friendly and warm"]
    tone = random.Random(hash(name) & 0xffff).choice(styles)   # steady per person
    convo = "\n".join((("Me: " if mine else name + ": ") + txt)
                      for mine, txt in thread)
    # reply in the language THEY are using
    their_text = " ".join(txt for mine, txt in thread if not mine) or convo
    last = next((txt for mine, txt in reversed(thread) if not mine), "")
    lang = _detect_ph_lang(their_text)   # Tagalog / Bisaya / Kapampangan / English / other
    lang_rule = f"Reply in {_lang_name(lang)}."
    prompt = (
        f"You are {persona} chatting on Messenger with {name}, in a {tone} tone. "
        f"{lang_rule} Read the conversation and reply directly and specifically "
        f"to {name}'s LAST message — keep that same topic, don't be generic.\n\n"
        f"Conversation so far:\n{convo}\n\n"
        f"{name} just said: \"{last}\"\n"
        f"Your reply (one short line):"
    )
    try:
        return fb.sanitize_comment(fb.ai_reply(prompt, system=CHAT_SYSTEM), "")
    except Exception:
        return ""


def open_messenger(serial, w, h):
    _nav(serial, 2, w, h)                            # Messenger tab
    time.sleep(random.uniform(4, 6))


def _chat_dismiss_popups(serial, d):
    # PIN-restore prompt -> Skip, 'Previews are on' sheet, tips. NEVER creates or enters a PIN.
    try:
        fb.dismiss_messenger_popups(serial, d)
    except Exception:
        pass


def _send_chat_text(serial, d, w, h, text):
    """Paste `text` into the chat composer and send it (never the thumbs-up 'Send Like')."""
    return fb.send_chat(serial, d, text)


def reply_to_messages(serial, d, w, h, cfg):
    """Open Messenger, find every UNREAD conversation, and reply to each one in
    context (continuing the conversation with a per-person tone). Re-reads the
    list after each reply so it works through them reliably."""
    cc = cfg.get("chat", {})
    if not cc.get("enabled", True) or fb is None or d is None:
        return 0
    open_messenger(serial, w, h)
    _chat_dismiss_popups(serial, d)
    if not read_unread_convs(serial, d, w, h):
        _go_home(serial, w, h)
        return 0                                     # only chat if messages waiting
    replied, handled = 0, set()
    limit = cc.get("max_per_session", 5)
    for _ in range(limit + 3):
        if replied >= limit:
            break
        _chat_dismiss_popups(serial, d)
        convs = [c for c in read_unread_convs(serial, d, w, h) if c[0] not in handled]
        if not convs:
            break
        name, name_y = convs[0]
        handled.add(name)
        tap(serial, int(w * 0.5), name_y)            # open this conversation
        time.sleep(1.5)
        _chat_dismiss_popups(serial, d)
        time.sleep(random.uniform(3, 4.5))
        _chat_dismiss_popups(serial, d)
        thread = read_thread(serial, d, w, h, name)
        # reply only if THEIR message is the latest (last bubble isn't ours)
        if thread and not thread[-1][0]:
            reply = generate_chat_reply(name, thread, cfg)
            reply = _maybe_add_typos(reply)      # ~40% chance of a human typo
            if reply and _send_chat_text(serial, d, w, h, reply):
                log(serial, f"chat -> {name}: {reply}")
                replied += 1
        press_back(serial)                            # back to the conversation list
        time.sleep(random.uniform(1.5, 2.5))
    _go_home(serial, w, h)
    return replied


# ── Engagement (react / share / comment) — reuses ../fb.py ──────────────────
def _pick_reaction(cfg):
    weights = cfg["engage"].get("reaction_weights") or {}
    names = [n for n in weights if n in fb.REACTION_ORDER]
    if not names:
        return random.choice(fb.REACTION_ORDER)
    return random.choices(names, weights=[weights[n] for n in names], k=1)[0]


def _looks_like_profile(vis):
    return ("add friend" in vis or "add friends" in vis or
            ("message" in vis and "friends" in vis and "posts" in vis))


def _has_comment_box(d):
    """Locale-independent 'we're on a commentable post-detail' check: the comment
    composer is an EditText. Works regardless of the phone's FB UI language
    (some phones are set to Catalan/etc., so the English 'Write a comment' string
    is unreliable)."""
    try:
        return (d(className="android.widget.EditText").exists or
                d(className="android.widget.MultiAutoCompleteTextView").exists)
    except Exception:
        return False


def _open_comment_thread(serial, d, w, h):
    """Tap the Comment button (middle of the action row) so a photo/post opens
    its comment thread — a post-detail view with the comment composer where
    sharing and commenting are reliable."""
    if _has_comment_box(d):
        return True
    cys = []
    try:
        pt = fb.find_action_row_like_point(d)
        if pt:
            cys.append(int(pt[1]))
    except Exception:
        pass
    cys += [int(h * 0.94), int(h * 0.955)]
    for cy in cys:
        tap(serial, int(w * 0.5), cy)
        time.sleep(1.8)
        if _has_comment_box(d):
            return True
    return False


def _find_share_point(d):
    """Find the Share button = rightmost of the 3 equally-sized clickable
    ViewGroups (Like/Comment/Share) that form a post's action row."""
    try:
        root = fb.hierarchy_root(d)
    except Exception:
        root = None
    if root is None:
        return None
    rows = {}
    for node in root.iter("node"):
        if node.attrib.get("clickable") != "true":
            continue
        cls = node.attrib.get("class", "")
        if "ViewGroup" not in cls and "Button" not in cls:
            continue
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.attrib.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if not (150 <= x2 - x1 <= 300 and 45 <= y2 - y1 <= 115):
            continue
        cy = (y1 + y2) // 2
        rows.setdefault(round(cy / 24) * 24, []).append(((x1 + x2) // 2, cy))
    for _, pts in sorted(rows.items()):
        if len(pts) >= 3:
            pts.sort()
            return pts[-1]                 # rightmost button = Share
    return None


# ── Share caption (human "sense": sometimes just share, sometimes an emoji,
# sometimes a short relevant caption — always tied to what the post is about).
_SHARE_EMOJIS = ["❤️", "😂", "🥺", "🙏", "🔥", "😍", "💯", "😭", "👏",
                 "✨", "🥰", "😊", "😅", "🤣", "😱"]

SHARE_CAPTION_SYSTEM = (
    "You are a real person sharing a Facebook post to your own timeline. "
    "Write a SHORT caption (max ~8 words) that makes clear sense as your own "
    "share of THIS specific post — it must react to the post's actual content. "
    "Never generic. LANGUAGE: write ONLY in the language you are told to use. "
    "Reply with only the caption text, no quotes."
)


def _choose_share_caption(post_text, cfg):
    """Human 'sense' for the share caption:
       - 30% no caption (just share as-is — real people do this a lot)
       - 20% just a single emoji reaction
       - 50% a short AI caption relevant to the post (may add an emoji, may typo)
    Returns '' when there should be no caption."""
    r = random.random()
    if r < 0.30:
        return ""
    if r < 0.50:
        return random.choice(_SHARE_EMOJIS)
    if fb is None or not post_text:
        return random.choice(_SHARE_EMOJIS) if random.random() < 0.5 else ""
    lang_name = _lang_name(_detect_ph_lang(post_text))
    prompt = (
        f"Facebook post you are sharing:\n\"\"\"{post_text}\"\"\"\n\n"
        f"Write ONE short caption (max ~8 words) in {lang_name} that reacts to "
        f"THIS post specifically, so it makes clear sense as your share. "
        "Casual, one line. Reply with only the caption text."
    )
    try:
        caption = fb.sanitize_comment(fb.ai_reply(prompt, system=SHARE_CAPTION_SYSTEM), "")
    except Exception:
        caption = ""
    if not caption:
        return random.choice(_SHARE_EMOJIS) if random.random() < 0.5 else ""
    # 40% chance to append an emoji at the end
    if random.random() < 0.40:
        caption = caption.rstrip(".!?, ") + " " + random.choice(_SHARE_EMOJIS)
    # Real users typo on mobile — pass through the same 40% typo layer
    return _maybe_add_typos(caption)


def _type_share_caption(serial, d, w, h, caption):
    """Type `caption` into the share compose sheet's EditText (if any). Best
    effort — some layouts don't expose one, in which case we just proceed to
    'Share now' with no text."""
    if not caption or fb is None:
        return False
    try:
        composer = d(className="android.widget.EditText")
        if not composer.exists:
            return False
        b = composer.info.get("bounds", {}) or {}
        cx = ((b.get("left", 0) + b.get("right", 0)) // 2) or int(w * 0.5)
        cy = ((b.get("top", 0) + b.get("bottom", 0)) // 2) or int(h * 0.35)
        fb.type_text(serial, d, (cx, cy), caption)
        time.sleep(random.uniform(0.8, 1.8))
        return True
    except Exception:
        return False


def _confirm_share(serial, d, w=0, h=0, caption=""):
    """Complete a share once its sheet/menu is open. Handles both the compose
    sheet ('Share now') and the options menu ('Share to Facebook' -> 'Share
    now'). If `caption` is non-empty, types it into the compose EditText
    before hitting 'Share now'. Returns True on success."""
    try:
        # If we're already on the compose sheet ('Share now' visible), type the
        # caption there BEFORE we hit the button.
        if d(text="Share now").exists:
            if caption:
                _type_share_caption(serial, d, w, h, caption)
            d(text="Share now").click()
            time.sleep(3)
            return True
        for label in ("Share to Facebook", "Share to News Feed"):
            if d(text=label).exists:
                d(text=label).click()
                time.sleep(random.uniform(1.8, 4.2) + random.random() * 0.5)
                if d(text="Share now").exists:
                    if caption:
                        _type_share_caption(serial, d, w, h, caption)
                    d(text="Share now").click()
                    time.sleep(3)
                return True
        if d(textContains="Share now").exists:
            if caption:
                _type_share_caption(serial, d, w, h, caption)
            d(textContains="Share now").click()
            time.sleep(3)
            return True
        # a share sheet is open but unrecognized -> back out to retry cleanly
        if d(textContains="Copy link").exists or d(text="Share to").exists:
            press_back(serial)
            time.sleep(1)
    except Exception:
        pass
    return False


def _recover_to_post(serial, d):
    """If a stray share/tap opened a sheet, dismiss it so the next try is clean."""
    try:
        vis = fb.visible_text(d).lower()
    except Exception:
        vis = ""
    if any(k in vis for k in ("share to", "copy link", "send as a message",
                              "share to your story")):
        press_back(serial)
        time.sleep(0.8)


def _try_share(serial, d, w, h, post_text="", cfg=None):
    """Open the share sheet from the post's Share button and complete it. Tries
    the Share button from the a11y tree (rightmost action button), then several
    action-row Y positions (layout varies per post), recovering between tries.

    If a caption fits ('human sense' via _choose_share_caption), it's typed into
    the compose sheet before 'Share now'. Caption is decided ONCE per share
    attempt so retries don't burn extra AI calls."""
    caption = _choose_share_caption(post_text, cfg) if cfg is not None else ""
    if caption:
        log(serial, f"share caption: {caption}")
    cands = []
    sp = _find_share_point(d)
    if sp:
        cands.append(sp)
    try:
        pt = fb.find_action_row_like_point(d)
        if pt:
            cands.append((int(w * 0.83), int(pt[1])))
    except Exception:
        pass
    for fy in (0.42, 0.50, 0.58, 0.66, 0.94):
        cands.append((int(w * 0.83), int(h * fy)))
    seen = set()
    for (sx, sy) in cands:
        key = (sx, sy // 16)
        if key in seen:
            continue
        seen.add(key)
        tap(serial, sx, sy)
        time.sleep(random.uniform(1.8, 4.2) + random.random() * 0.5)
        if _confirm_share(serial, d, w, h, caption):
            return True
        _recover_to_post(serial, d)
    return False


def engage_post(serial, d, w, h, cfg):
    """Daily-activity engagement on a post in the account's own FEED: a reaction that fits the
    post, and sometimes a comment and/or a share. Works on the post's Like / Comment / Share row
    right in the feed (found on screen), reads the caption with OCR, skips ads and posts this
    account already reacted to. Never taps blindly."""
    done = []
    ec = cfg["engage"]
    row = post_text = None
    fallback = None                                  # a fresh post whose caption wasn't readable
    pending = ""
    for attempt in range(12):
        if stop_requested():
            return done
        swipe(serial, w // 2, int(h * 0.74), w // 2 + random.randint(-20, 20), int(h * random.uniform(0.36, 0.48)),
              random.randint(350, 600))
        time.sleep(random.uniform(1.4, 2.4))
        root = fb.hierarchy_root(d)
        rows = [r for r in fb.action_rows(d, root) if h * 0.22 < r["y1"] < h * 0.86]
        if not rows:
            lines, _img = fb.ocr(d)                  # remember a caption scrolling past
            nav = fb.nav_points(d, root)
            top_floor = (max(p[1] for p in nav) + int(h * 0.04)) if nav else int(h * 0.15)
            cap = fb.post_text_above(lines, {"y1": h, "y2": h}, top_floor)
            if cap:
                pending = cap
            elif fb.post_header_in(lines, top_floor, h):
                pending = ""
            continue
        lines, img = fb.ocr(d)
        nav = fb.nav_points(d, root)
        top_floor = (max(p[1] for p in nav) + int(h * 0.04)) if nav else int(h * 0.15)
        carried = pending                            # caption of a post whose row hadn't shown yet
        last_y2 = rows[-1]["y2"] if rows else top_floor
        pending = fb.post_text_above(lines, {"y1": h, "y2": h}, last_y2)   # header below the last row
        for i, r in enumerate(rows):
            top = rows[i - 1]["y2"] if i > 0 else top_floor
            if fb.is_sponsored(lines, r, top):
                continue
            if img is not None and fb.is_reacted(img, r):
                continue                             # already reacted to this one
            cap = fb.post_text_above(lines, r, top)
            if not cap and i == 0 and carried and not fb.post_header_in(lines, top, r["y1"]):
                cap = carried                        # same post: its caption scrolled off the top
            if cap:
                row, post_text = r, cap
                break
            if fallback is None and attempt >= 3:
                fallback = (attempt, r)
        if row:
            break
        if fallback is not None and attempt - fallback[0] >= 4:
            break                                    # no readable caption nearby: use that post
    if not row and fallback is not None:
        # re-find it: the feed has scrolled since, so react to the freshest post on screen instead
        rows = [r for r in fb.action_rows(d) if h * 0.22 < r["y1"] < h * 0.86]
        img = fb.screenshot(d)
        rows = [r for r in rows if img is None or not fb.is_reacted(img, r)]
        if rows:
            row, post_text = rows[0], ""
    if not row:
        log(serial, "engage: no fresh post found on screen")
        return done
    pause(*cfg["read_pause_seconds"])   # "read" the post first
    if post_text:
        log(serial, f"post: {post_text[:80]}")

    if random.random() < ec.get("react_prob", 0.9):
        reaction = _reaction_for_post(post_text, cfg) if post_text else _pick_reaction(cfg)
        try:
            res = str(fb.react_row(serial, d, row, reaction))
            if res.startswith("reacted"):
                done.append(f"react:{reaction}")
                log(serial, f"reacted {reaction}")
            else:
                log(serial, f"react {reaction} {res}")
        except Exception as e:
            log(serial, f"react error: {e}")
        time.sleep(random.uniform(1, 3))

    reacted = bool(done)
    want_comment = reacted and random.random() < ec.get("comment_prob", 0.3)
    want_share = reacted and random.random() < ec.get("share_prob", 0.3)
    if want_comment or want_share:
        rows = fb.action_rows(d)                     # the row may have shifted a little
        same = min(rows, key=lambda r: abs(r["y1"] - row["y1"]), default=None)
        if same is not None and abs(same["y1"] - row["y1"]) < 120:
            row = same
        else:
            want_comment = want_share = False
    if want_comment:
        # only when the caption could be read: a comment must be about THIS post
        text = _comment_for_post(post_text, cfg) if post_text else ""
        if not text and ec.get("comment_example"):
            text = fb.unique_comment(ec.get("comment_example", ""))
        if text:
            text = _maybe_add_typos(text)
            try:
                res = str(fb.comment_row(serial, d, row, text))
                if res.startswith("commented"):
                    done.append("comment")
                    log(serial, f"commented: {text}")
                else:
                    log(serial, f"comment {res}")
            except Exception as e:
                log(serial, f"comment error: {e}")
        elif post_text:
            log(serial, "comment skipped (AI busy — free limit is 1 reply / 45 s per connection)")
        else:
            log(serial, "comment skipped (caption not readable)")
        if want_share:
            rows = fb.action_rows(d)
            same = min(rows, key=lambda r: abs(r["y1"] - row["y1"]), default=None)
            want_share = same is not None and abs(same["y1"] - row["y1"]) < 120
            if want_share:
                row = same
    if want_share:
        caption = _choose_share_caption(post_text, cfg)
        try:
            if fb.share_row(serial, d, row, caption):
                done.append("share")
                log(serial, "shared" + (f" with caption: {caption}" if caption else ""))
            else:
                log(serial, "share failed (no 'Share now' sheet)")
        except Exception as e:
            log(serial, f"share error: {e}")
    return done


# ── Session ────────────────────────────────────────────────────────────────
def weighted_choice(weights):
    total = sum(weights.values())
    r = random.uniform(0, total)
    upto = 0
    for name, wt in weights.items():
        upto += wt
        if r <= upto:
            return name
    return "scroll"


def _go_home(serial, w, h):
    """Return to the news feed (Home tab in the top nav)."""
    _nav(serial, 0, w, h)
    time.sleep(random.uniform(2, 3.5))


def full_session(serial, cfg):
    """One no-arg run: this phone does its daily activity — confirm/add friends,
    then browse its own feed and react/comment/share on TIMELINE posts."""
    stats = {"reacts": 0, "comments": 0, "shares": 0,
             "confirmed": 0, "added": 0, "browse": 0, "replied": 0}
    session_start = time.time()
    wake(serial)
    w, h = screen_size(serial)
    d = None
    if fb is not None:
        try:
            d = fb.connect(serial)
            if not fb.ocr_available():
                log(serial, "Windows OCR unavailable — browse-only")
                d = None
        except Exception as e:
            log(serial, f"u2 connect failed, browse-only ({e})")
    if d is not None:
        _DEV[serial] = d
    else:
        _DEV.pop(serial, None)
    if stop_requested():
        return stats
    open_feed(serial, cfg["package"])

    # SAFETY: if FB is showing a verification / suspicious-activity / checkpoint
    # screen, don't touch the account further — pause this phone for the flag
    # cooldown instead. Anything we do while flagged digs it in deeper.
    warn = detect_verification(d)
    if warn:
        mark_flagged(serial, cfg, warn)
        try:
            sh(serial, "am", "force-stop", cfg.get("package", "com.facebook.lite"))
            sh(serial, "input", "keyevent", "KEYCODE_HOME")
        except Exception:
            pass
        return stats

    # Human-behavior scaling: warm-up curve (very low first days -> full at
    # ~2 weeks) and 1-2 rest days a week where this phone barely does anything.
    mult = warmup_multiplier(serial, cfg)
    rest = is_rest_day(serial, cfg)
    if rest:
        log(serial, "rest day — scroll-only, no engagement / friends")
    elif mult < 0.99:
        log(serial, f"warm-up scale {mult:.2f} "
                    f"(account_age={account_age_days(serial):.1f}d)")
    cfg = _scale_cfg_for_session(cfg, mult, rest)

    # PRIORITY #1 — messages. Reply to anyone who messaged BEFORE anything else.
    try:
        stats["replied"] += reply_to_messages(serial, d, w, h, cfg)
    except Exception as e:
        log(serial, f"chat error: {e}")
    last_chat = time.time()
    if stop_requested():
        return stats

    # Friends: confirm any incoming requests, then add a few.
    if cfg["weights"].get("confirm_requests", 0) > 0:
        try:
            stats["confirmed"] = confirm_requests(serial, w, h, cfg)
        except Exception as e:
            log(serial, f"confirm error: {e}")
    if cfg["weights"].get("add_friends", 0) > 0 and not stop_requested():
        try:
            stats["added"] = add_friends(serial, w, h, cfg)
        except Exception as e:
            log(serial, f"add_friends error: {e}")

    # Feed daily activity: browse the timeline and react / comment / share on
    # its posts (no fixed target post — whatever is in this account's feed).
    # Messages stay the priority: every `chat.check_interval_seconds` the loop
    # jumps back to Messenger to answer any new message/reply, then resumes.
    open_feed(serial, cfg["package"])
    weights = {k: v for k, v in cfg["weights"].items() if v > 0}
    if not cfg["engage"].get("enabled", True):
        weights.pop("engage", None)
    if d is None:
        for k in ("engage", "add_friends", "confirm_requests"):
            weights.pop(k, None)
    if not weights:
        weights = {"scroll": 1}
    chat_every = cfg.get("chat", {}).get("check_interval_seconds", 60)
    # When rotating accounts, give each account a fixed ~N-minute turn so it
    # switches on schedule; otherwise use the random session length.
    if cfg.get("switch_accounts", True) and cfg.get("account_switch_minutes"):
        end = session_start + float(cfg["account_switch_minutes"]) * 60
    else:
        end = time.time() + random.uniform(*cfg["session_minutes"]) * 60
    browse = 0
    while time.time() < end and not stop_requested():
        # watchdog: if a tapped link/app pushed us out of FB Lite, come back
        ensure_fb(serial, cfg["package"])
        # SAFETY re-check: if a checkpoint / verification screen appeared
        # mid-session, abort and cool this phone down.
        if d is not None and random.random() < 0.04:
            warn = detect_verification(d)
            if warn:
                mark_flagged(serial, cfg, warn)
                break
        # PRIORITY: check messages during browsing — handle new ones first
        if d is not None and (time.time() - last_chat) >= chat_every:
            try:
                stats["replied"] += reply_to_messages(serial, d, w, h, cfg)
            except Exception as e:
                log(serial, f"chat error: {e}")
            last_chat = time.time()
            _go_home(serial, w, h)                # back to the feed, then resume
            continue
        act = weighted_choice(weights)
        browse += 1
        try:
            if act == "scroll":
                scroll_feed(serial, w, h); pause(*cfg["read_pause_seconds"])
            elif act == "scroll_back":
                scroll_back(serial, w, h); pause(1.5, 4.0)
            elif act == "long_watch":
                pause(*cfg["watch_pause_seconds"])
            elif act == "open_post":
                open_post(serial, w, h, cfg)
            elif act == "notifications":
                check_notifications(serial, w, h, cfg)
            elif act == "reels":
                watch_reels(serial, w, h, cfg)
            elif act == "add_friends":
                stats["added"] += add_friends(serial, w, h, cfg)
            elif act == "confirm_requests":
                stats["confirmed"] += confirm_requests(serial, w, h, cfg)
            elif act == "engage":
                for e in engage_post(serial, d, w, h, cfg):
                    if str(e).startswith("react"):
                        stats["reacts"] += 1
                    elif e == "comment":
                        stats["comments"] += 1
                    elif e == "share":
                        stats["shares"] += 1
        except Exception as e:
            log(serial, f"activity '{act}' error: {e}")
            time.sleep(2)
        # Human-imperfection nudge — small chance of a scroll-back / stray
        # back button / mis-tap so activity isn't perfectly forward-only.
        _human_mistake(serial, w, h, cfg)
    stats["browse"] = browse

    # Rotate to the next saved account so EVERY account gets its ~15-min turn.
    if not stop_requested():
        try:
            rotate_account(serial, d, cfg)
        except Exception as e:
            log(serial, f"rotate error: {e}")

    sh(serial, "input", "keyevent", "KEYCODE_HOME")
    log(serial, f"DONE | reacts={stats['reacts']} comments={stats['comments']} "
                f"shares={stats['shares']} replied={stats['replied']} "
                f"confirm~{stats['confirmed']} add~{stats['added']} "
                f"browse={stats['browse']}")
    return stats


def _phone_forever(serial, cfg, totals, lock):
    """Run session after session on one phone until a stop is requested.
    Never dies on its own — errors are caught and it just starts the next
    session. Anti-detection gates go here (flag cooldown, quiet hours,
    daily session cap, variable between-session gap)."""
    if fb is not None and cfg.get("ai_key"):
        fb.AI_KEY = cfg["ai_key"]                 # Pollinations key: no 45-second free limit
    interruptible_sleep(random.uniform(*cfg["stagger_seconds"]))
    n = 0
    while not stop_requested():
        # Flag cooldown: FB showed a verification / suspicious-activity screen
        # recently, so leave this phone alone. Re-check hourly.
        cooldown = flag_cooldown_remaining(serial)
        if cooldown > 0:
            log(serial, f"in flag cooldown ({cooldown/3600:.1f}h left) — sleeping")
            interruptible_sleep(min(cooldown, 3600))
            continue
        # Quiet hours: no new sessions during 'sleep' hours (default 1am-7am).
        if in_quiet_hours(cfg):
            interruptible_sleep(random.uniform(600, 1800))
            continue
        # Daily session cap — real users don't open FB 30 times a day. Warm-up
        # and rest days scale this down further.
        cap = daily_session_cap(serial, cfg)
        done_today = sessions_today(serial)
        if done_today >= cap:
            log(serial, f"daily cap reached ({done_today}/{cap}) — pausing")
            interruptible_sleep(random.uniform(1800, 3600))
            continue

        record_session_start(serial)
        n += 1
        try:
            s = full_session(serial, cfg)
            with lock:
                for k in ("reacts", "comments", "shares", "replied", "added", "confirmed"):
                    totals[k] = totals.get(k, 0) + s.get(k, 0)
                totals["sessions"] = totals.get("sessions", 0) + 1
        except Exception as e:
            log(serial, f"session error (continuing): {e}")
            interruptible_sleep(5)
        if stop_requested():
            break
        # Between-session gap: warm-up + rest days stretch it out; also a 15%
        # chance of a much longer gap ("puts the phone down") so cadence
        # doesn't look mechanical.
        base = cfg.get("between_sessions_seconds", [30, 120])
        m = 1.0 / max(0.2, warmup_multiplier(serial, cfg))
        if is_rest_day(serial, cfg):
            m *= 3.0
        gap = random.uniform(base[0] * m, base[1] * m)
        if random.random() < 0.15:
            gap *= random.uniform(3, 10)
        log(serial, f"session {n} done; next in {gap:.0f}s (or until STOP)")
        interruptible_sleep(gap)
    # stopped: exit FB Lite and leave the phone idle on its home screen
    try:
        sh(serial, "am", "force-stop", cfg.get("package", "com.facebook.lite"))
        sh(serial, "input", "keyevent", "KEYCODE_HOME")
    except Exception:
        pass
    log(serial, "stopped — FB Lite closed.")


def main():
    """No arguments. Runs FOREVER on every USB phone until you stop it
    (Ctrl+C, or create a file named STOP in this folder)."""
    cfg = load_config()

    # a leftover STOP file from a previous run would stop us instantly — clear it
    try:
        if os.path.exists(STOP_FILE):
            os.remove(STOP_FILE)
    except Exception:
        pass
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    try:
        signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    except Exception:
        pass

    devices = usb_devices()
    print(f"\n=== FB Daily (continuous) @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    if not devices:
        print("No USB-connected phones found. Plug in via USB, enable USB "
              "debugging, and authorize this computer.")
        return
    if fb is None:
        print(f"[note] engagement/friends limited: ../fb.py not importable "
              f"({_FB_IMPORT_ERR})")
    print(f"USB phones: {len(devices)} -> {', '.join(devices)}")
    print("Running continuously. To STOP: press Ctrl+C, or create a file named "
          f"STOP here:\n  {STOP_FILE}\n  (Windows: `type nul > STOP` in this folder)\n")

    totals = {}
    lock = threading.Lock()
    threads = [threading.Thread(target=_phone_forever,
                                args=(s, cfg, totals, lock), daemon=True)
               for s in devices]
    for t in threads:
        t.start()

    # keep the main thread alive until stop, then let workers wind down
    try:
        while any(t.is_alive() for t in threads) and not stop_requested():
            time.sleep(0.5)
    except KeyboardInterrupt:
        STOP.set()
    STOP.set()
    print("\nStopping — letting each phone finish its current step, then closing "
          "FB Lite ...")
    for t in threads:
        t.join(timeout=120)
    # safety sweep: make sure FB Lite is closed on every phone
    pkg = cfg.get("package", "com.facebook.lite")
    for s in devices:
        try:
            sh(s, "am", "force-stop", pkg)
            sh(s, "input", "keyevent", "KEYCODE_HOME")
        except Exception:
            pass
    try:
        if os.path.exists(STOP_FILE):
            os.remove(STOP_FILE)
    except Exception:
        pass
    print("\n" + "=" * 62)
    print(f"Stopped after {totals.get('sessions', 0)} session(s) across "
          f"{len(devices)} phones.")
    print(f"  feed reactions       : {totals.get('reacts', 0)}")
    print(f"  feed comments        : {totals.get('comments', 0)}")
    print(f"  feed shares          : {totals.get('shares', 0)}")
    print(f"  chat replies         : {totals.get('replied', 0)}")
    print(f"  friend requests sent : ~{totals.get('added', 0)}")
    print(f"  confirm taps         : ~{totals.get('confirmed', 0)}")
    print("=" * 62)


if __name__ == "__main__":
    main()
