"""
fb.py — Facebook Lite screen reading + actions for FB Daily (fbdaily/fbdaily.py).

FB Lite draws its feed, friends and comment screens itself, so the Android
accessibility tree has the *shapes* of the buttons but no text. This module
combines three sources to act reliably:

  * the accessibility tree (uiautomator2)  -> where the tappable areas are
    (the Like / Comment / Share row of a post, the 6 top tabs, picker buttons)
  * Windows' built-in OCR on a screenshot   -> what the screen says
    ("Confirm", "Add friend", "Share now", "Write a comment...", post captions)
  * pixel colours                           -> state (a post already reacted,
    the blue Send arrow, solid-blue Confirm buttons)

Messenger in FB Lite *is* accessible, so chats are read from the tree directly.

Text is typed by putting it on the phone's clipboard and pasting (works with
emoji / ñ, no extra keyboard app is installed on the phone).

AI text (comments, chat replies, share captions) comes from the free
Pollinations text API (no key).
"""
import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import uiautomator2 as u2

try:
    from PIL import Image
except Exception:                                   # pragma: no cover
    Image = None

ADB = shutil.which("adb") or "adb"                 # fbdaily / SCIENTIST overwrite this with their adb
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
FB_BLUE_HINT = (8, 102, 255)
REACTION_ORDER = ["like", "love", "care", "haha", "wow", "sad", "angry"]
SUPPORTS_ACCOUNT_SWITCH = False                     # FB Lite's account switcher isn't mapped yet


# ── adb ─────────────────────────────────────────────────────────────────────
def run(args, serial=None, timeout=30):
    cmd = [ADB] + (["-s", serial] if serial else []) + list(args)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        class _R:
            returncode, stdout, stderr = 1, "", "timeout"
        return _R()


def sh(serial, *cmd, timeout=30):
    return run(["shell", *cmd], serial=serial, timeout=timeout)


def tap(serial, x, y):
    sh(serial, "input", "tap", str(int(x)), str(int(y)))


def long_press(serial, x, y, ms=900):
    sh(serial, "input", "swipe", str(int(x)), str(int(y)), str(int(x) + 1), str(int(y) + 1), str(ms))


def back(serial):
    sh(serial, "input", "keyevent", "KEYCODE_BACK")


# ── device ──────────────────────────────────────────────────────────────────
_DEVS = {}
_DEV_LOCK = threading.Lock()


def connect(serial):
    """uiautomator2 device for `serial` (cached). `d.serial_` holds the serial."""
    with _DEV_LOCK:
        d = _DEVS.get(serial)
    if d is not None:
        try:
            d.info                                   # still alive?
            return d
        except Exception:
            pass
    d = u2.connect(serial)
    d.settings["wait_timeout"] = 3.0
    try:
        d.serial_ = serial
    except Exception:
        pass
    with _DEV_LOCK:
        _DEVS[serial] = d
    return d


def _serial(d):
    return getattr(d, "serial_", None) or getattr(d, "serial", None)


def screen_wh(d):
    try:
        info = d.info
        return int(info["displayWidth"]), int(info["displayHeight"])
    except Exception:
        return 1080, 2340


# ── accessibility tree ──────────────────────────────────────────────────────
_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def hierarchy_root(d):
    try:
        return ET.fromstring(d.dump_hierarchy(compressed=False))
    except Exception:
        return None


def nodes(d, root=None):
    """[(x1, y1, x2, y2, clickable, text, desc, cls)] for every node."""
    root = root if root is not None else hierarchy_root(d)
    out = []
    if root is None:
        return out
    for n in root.iter("node"):
        m = _BOUNDS.match(n.attrib.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        out.append((x1, y1, x2, y2, n.attrib.get("clickable") == "true", (n.attrib.get("text") or "").strip(),
                    (n.attrib.get("content-desc") or "").strip(), n.attrib.get("class", "")))
    return out


def action_rows(d, root=None):
    """Like / Comment / Share rows of feed posts: three equal-width tappable cells spanning the
    screen width. Returns [{'y1','y2','cells':[like, comment, share]}] top->bottom, each cell
    (x1, y1, x2, y2)."""
    w, h = screen_wh(d)
    groups = {}
    for x1, y1, x2, y2, ck, _t, _d, _c in nodes(d, root):
        if not ck:
            continue
        cw, ch = x2 - x1, y2 - y1
        if not (0.27 * w <= cw <= 0.37 * w and 0.025 * h <= ch <= 0.075 * h):
            continue
        groups.setdefault((y1, y2), []).append((x1, y1, x2, y2))
    rows = []
    for (y1, y2), cells in groups.items():
        cells = sorted(set(cells))
        if len(cells) != 3 or cells[-1][2] - cells[0][0] < 0.85 * w:
            continue
        rows.append({"y1": y1, "y2": y2, "cells": cells})
    return sorted(rows, key=lambda r: r["y1"])


def nav_points(d, root=None):
    """Centres of FB Lite's 6 top tabs (home, friends, messages, video, notifications, market)."""
    w, h = screen_wh(d)
    cand = {}
    for x1, y1, x2, y2, ck, _t, _d, _c in nodes(d, root):
        if ck and y2 < 0.2 * h and abs((x2 - x1) - w / 6) < 0.03 * w and (y2 - y1) > 0.03 * h:
            cand.setdefault(y1, []).append(((x1 + x2) // 2, (y1 + y2) // 2))
    for _y, pts in sorted(cand.items()):
        if len(pts) == 6:
            return sorted(pts)
    return None


def has_nav(d, root=None):
    return nav_points(d, root) is not None


# ── screenshot + OCR (Windows.Media.Ocr, offline) ───────────────────────────
class Line:
    __slots__ = ("text", "box", "words")

    def __init__(self, text, box, words):
        self.text, self.box, self.words = text, box, words

    def __repr__(self):
        return f"Line({self.text!r}, {self.box})"


_OCR_LOCAL = threading.local()


def _ocr_engine():
    eng = getattr(_OCR_LOCAL, "eng", None)
    if eng is None:
        from winrt.windows.media.ocr import OcrEngine
        eng = OcrEngine.try_create_from_user_profile_languages()
        if eng is None:                            # display language without an OCR pack
            try:
                from winrt.windows.globalization import Language
                eng = OcrEngine.try_create_from_language(Language("en-US"))
                if eng is None:
                    langs = list(OcrEngine.available_recognizer_languages)
                    if langs:
                        eng = OcrEngine.try_create_from_language(langs[0])
            except Exception:
                eng = None
        _OCR_LOCAL.eng = eng
    return eng


async def _ocr_async(img):
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter
    img = img.convert("RGBA")
    w, h = img.size
    dw = DataWriter()
    dw.write_bytes(img.tobytes("raw", "BGRA"))
    bmp = SoftwareBitmap.create_copy_from_buffer(dw.detach_buffer(), BitmapPixelFormat.BGRA8, w, h,
                                                 BitmapAlphaMode.PREMULTIPLIED)
    res = await _ocr_engine().recognize_async(bmp)
    out = []
    for ln in res.lines:
        words = []
        for wd in ln.words:
            r = wd.bounding_rect
            words.append((wd.text, (int(r.x), int(r.y), int(r.x + r.width), int(r.y + r.height))))
        if not words:
            continue
        box = (min(b[0] for _, b in words), min(b[1] for _, b in words),
               max(b[2] for _, b in words), max(b[3] for _, b in words))
        out.append(Line(ln.text, box, words))
    out.sort(key=lambda l: (l.box[1], l.box[0]))
    return out


def ocr_available():
    try:
        return _ocr_engine() is not None
    except Exception:
        return False


def screenshot(d):
    try:
        return d.screenshot()
    except Exception:
        return None


def ocr(d, img=None):
    """(lines, image). Lines are sorted top->bottom."""
    img = img if img is not None else screenshot(d)
    if img is None:
        return [], None
    try:
        return asyncio.run(_ocr_async(img)), img
    except Exception:
        return [], img


def visible_text(d):
    """Everything readable on screen: OCR lines + any accessibility text."""
    lines, _img = ocr(d)
    parts = [l.text for l in lines]
    for _x1, _y1, _x2, _y2, _ck, t, ds, _c in nodes(d):
        if t:
            parts.append(t)
        elif ds and len(ds) > 3:
            parts.append(ds)
    return "\n".join(parts)


def _norm(s):
    return re.sub(r"[^a-z0-9ñ]+", " ", (s or "").lower()).strip()


def find_phrase(lines, phrase, exact_line=False):
    """Boxes (x1, y1, x2, y2) where `phrase` appears as consecutive OCR words."""
    want = _norm(phrase).split()
    hits = []
    for ln in lines:
        if exact_line:
            if _norm(ln.text) == " ".join(want):
                hits.append(ln.box)
            continue
        ws = [(_norm(t), b) for t, b in ln.words]
        for i in range(len(ws) - len(want) + 1):
            if all(ws[i + k][0] == want[k] for k in range(len(want))):
                bs = [ws[i + k][1] for k in range(len(want))]
                hits.append((min(b[0] for b in bs), min(b[1] for b in bs),
                             max(b[2] for b in bs), max(b[3] for b in bs)))
    return hits


def center(box):
    return (box[0] + box[2]) // 2, (box[1] + box[3]) // 2


# ── pixel colours ───────────────────────────────────────────────────────────
def _px(img, x, y):
    try:
        return img.getpixel((int(x), int(y)))[:3]
    except Exception:
        return (255, 255, 255)


def is_fb_blue(rgb):
    r, g, b = rgb
    return b > 200 and r < 90 and 70 < g < 170


def colored_fraction(img, box, step=4):
    """Share of clearly coloured (saturated, not grey) pixels inside box."""
    x1, y1, x2, y2 = box
    tot = hit = 0
    for y in range(int(y1) + 2, int(y2) - 2, step):
        for x in range(int(x1) + 2, int(x2) - 2, step):
            r, g, b = _px(img, x, y)
            mx, mn = max(r, g, b), min(r, g, b)
            tot += 1
            if mx > 60 and (mx - mn) > 70:
                hit += 1
    return hit / tot if tot else 0.0


def blue_centroid(img, box, step=3):
    x1, y1, x2, y2 = box
    xs, ys = [], []
    for y in range(max(0, int(y1)), int(y2), step):
        for x in range(max(0, int(x1)), int(x2), step):
            if is_fb_blue(_px(img, x, y)):
                xs.append(x)
                ys.append(y)
    if len(xs) < 6:
        return None
    return sum(xs) // len(xs), sum(ys) // len(ys)


# ── typing ──────────────────────────────────────────────────────────────────
def keyboard_shown(serial):
    out = sh(serial, "dumpsys", "input_method", timeout=10).stdout or ""
    return "mInputShown=true" in out


def hide_keyboard(serial):
    if keyboard_shown(serial):
        back(serial)
        time.sleep(0.8)


def paste_text(serial, d, text):
    """Put `text` on the phone clipboard and paste it into the focused field."""
    d.clipboard = text
    time.sleep(0.4)
    sh(serial, "input", "keyevent", "279")          # KEYCODE_PASTE
    time.sleep(0.8)


def type_text(serial, d, point, text):
    """Tap the field at `point`, then paste `text` into it."""
    if point:
        tap(serial, *point)
        time.sleep(1.0)
    paste_text(serial, d, text)


# ── feed posts ──────────────────────────────────────────────────────────────
_TIME_TOKEN = re.compile(r"(^|\s)(\d{1,2}\s?(m|h|d|w|y|min|mins|hr|hrs|hour|hours|day|days)|just now|yesterday)"
                         r"(\s|$|\W)", re.I)
_HEADER_MARK = re.compile(r"(\bfollow\b|\bsuggested for you\b|\bshared a\b|\bis with\b|\bupdated (his|her|their)\b)")
_FEED_JUNK = ("reels", "people you may know", "friend requests", "suggested for you", "sponsored",
              "what's on your mind", "create story", "see all", "add friend", "follow", "see more",
              "write a comment", "most relevant", "facebook", "translated from", "see translation",
              "see original", "rate this translation")


def post_text_above(lines, row, top_limit):
    """Caption of the post whose action row is `row`: OCR lines between the post header
    (the line with the '3d' / '5h' timestamp) and the media / action row."""
    region = [l for l in lines if l.box[1] >= top_limit and l.box[3] <= row["y1"]]
    if not region:
        return ""
    if any("sponsored" in l.text.lower() for l in region):
        return ""
    # the post header (author name, then '3d · 🌐' / '· Follow') must be visible at the top of the
    # region; otherwise the caption is scrolled away and only photo text would be left.
    start = None
    for i, l in enumerate(region[:4]):
        low = l.text.lower()
        if _TIME_TOKEN.search(l.text) or _HEADER_MARK.search(low):
            start = i + 1
    if start is None:
        return ""
    cap, prev_bottom = [], None
    for l in region[start:]:
        low = l.text.lower().strip()
        if prev_bottom is not None and l.box[1] - prev_bottom > 90:
            break                                  # big gap: the photo / video starts
        if len(low) < 2 or any(low.startswith(j) for j in _FEED_JUNK):
            if low.startswith(("translated from", "see translation", "see original")):
                break                              # the rest is FB's translation of the same text
            continue
        cap.append(l.text.strip())
        prev_bottom = l.box[3]
    text = " ".join(cap)[:400]
    return text if looks_like_caption(text) else ""


def looks_like_caption(text):
    """Real sentence-ish text, not a few letters OCR'd off a photo ('ON LIN E')."""
    words = re.findall(r"[A-Za-zÀ-ÿñÑ']{3,}", text or "")
    if len(words) < 2:
        return False
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 10:
        return False
    upper = sum(c.isupper() for c in letters) / len(letters)
    return not (upper > 0.8 and len(letters) < 25)


def post_header_in(lines, y_from, y_to):
    """True if a post header (timestamp / 'Follow' line) is visible between y_from and y_to."""
    return any(_TIME_TOKEN.search(l.text) or _HEADER_MARK.search(l.text.lower())
               for l in lines if l.box[1] >= y_from and l.box[3] <= y_to)


def is_sponsored(lines, row, top_limit):
    return any("sponsored" in l.text.lower() for l in lines
               if l.box[1] >= top_limit and l.box[3] <= row["y1"])


def is_reacted(img, row):
    """True when the post's Like cell shows a coloured (reacted) icon."""
    return colored_fraction(img, row["cells"][0]) > 0.015


_REACTION_LABEL = {r: r.capitalize() for r in REACTION_ORDER}


def react_row(serial, d, row, reaction):
    """React to the post of `row`. Returns 'reacted:<r>' or 'failed:<why>'."""
    like = row["cells"][0]
    lx, ly = center(like)
    reaction = reaction if reaction in REACTION_ORDER else "like"
    if reaction == "like":
        tap(serial, lx, ly)
    else:
        picked = False
        for hold in (900, 1300):
            long_press(serial, lx, ly, hold)
            time.sleep(1.4)
            lines, _img = ocr(d)
            labels = find_phrase(lines, _REACTION_LABEL[reaction])
            if len([r for r in REACTION_ORDER if find_phrase(lines, _REACTION_LABEL[r])]) < 3 or not labels:
                back(serial)
                time.sleep(0.8)
                continue
            lab = labels[0]
            cx = (lab[0] + lab[2]) // 2
            target = None
            for x1, y1, x2, y2, ck, _t, _d, _c in nodes(d):     # the picker button holding this label
                if ck and x1 <= cx <= x2 and y1 <= lab[1] <= y2 and (x2 - x1) < 0.3 * screen_wh(d)[0]:
                    target = ((x1 + x2) // 2, (y1 + lab[1]) // 2)
                    break
            if target is None:
                target = (cx, lab[1] - int(0.045 * screen_wh(d)[1]))
            tap(serial, *target)
            picked = True
            break
        if not picked:
            return "failed:picker"
    time.sleep(1.6)
    img = screenshot(d)
    if img is None:
        return f"reacted:{reaction}"
    rows = action_rows(d)
    same = min(rows, key=lambda r: abs(r["y1"] - row["y1"]), default=None)
    if same is not None and abs(same["y1"] - row["y1"]) < 80 and is_reacted(img, same):
        return f"reacted:{reaction}"
    return "failed:not-confirmed"


def _close_sheets(serial, d, max_backs=3):
    """Back out of comment / share sheets until the feed (post action rows) is visible."""
    for _ in range(max_backs):
        if action_rows(d) and has_nav(d):
            lines, _ = ocr(d)
            if not find_phrase(lines, "write a comment") and not find_phrase(lines, "share now"):
                return True
        back(serial)
        time.sleep(1.0)
    return bool(action_rows(d))


def _find_send_arrow(d, text, hint_box=None):
    """The blue send arrow at the right end of the comment box holding `text`. The box grows to
    several lines and moves when the keyboard closes, so it is found by the typed text itself."""
    w, h = screen_wh(d)
    lines, img = ocr(d)
    if img is None:
        return None
    want = set(_norm(text).split())
    mine = [l for l in lines if l.box[1] > 0.30 * h and want & set(_norm(l.text).split())]
    bands = []
    if mine:
        top = min(l.box[1] for l in mine if l.box[1] >= max(l2.box[1] for l2 in mine) - int(0.12 * h))
        bottom = max(l.box[3] for l in mine)
        bands.append((top - int(0.03 * h), bottom + int(0.03 * h)))
    if hint_box is not None:
        bands.append((hint_box[1] - int(0.035 * h), hint_box[3] + int(0.035 * h)))
    for y1, y2 in bands:
        pt = blue_centroid(img, (int(w * 0.80), max(0, y1), w - 2, min(h - 1, y2)))
        if pt is not None:
            return pt
    return None


def _clear_field(serial, n):
    sh(serial, "input", "keyevent", "KEYCODE_MOVE_END")
    sh(serial, "input", "keyevent", *(["67"] * (n + 8)))
    time.sleep(0.5)


def comment_row(serial, d, row, text):
    """Open the comments of `row`'s post, write `text`, send it. 'commented' or 'failed:<why>'."""
    if not text:
        return "failed:no-text"
    w, h = screen_wh(d)
    tap(serial, *center(row["cells"][1]))
    time.sleep(2.8)
    lines, _img = ocr(d)
    box = (find_phrase(lines, "write a comment") or [None])[0]
    if box is None:
        _close_sheets(serial, d)
        return "failed:no-composer"
    tap(serial, *center(box))
    time.sleep(1.0)
    paste_text(serial, d, text)
    time.sleep(1.0)
    send = _find_send_arrow(d, text, box)
    if send is None:
        # the text didn't land / no send arrow: clear any draft and leave without posting
        _clear_field(serial, len(text))
        _close_sheets(serial, d)
        return "failed:no-send-button"
    tap(serial, *send)
    time.sleep(3.0)
    lines, _ = ocr(d)
    first = " ".join(_norm(text).split()[:2])
    ok = (bool(find_phrase(lines, "your comment was posted")) or bool(find_phrase(lines, "write a comment"))
          or bool(first and any(first in _norm(l.text) for l in lines)))
    _close_sheets(serial, d)
    return "commented" if ok else "failed:not-confirmed"


def share_row(serial, d, row, caption=""):
    """Share `row`'s post to the timeline ('Share now'), optional caption. True on success."""
    tap(serial, *center(row["cells"][2]))
    time.sleep(3.0)
    lines, _ = ocr(d)
    btn = (find_phrase(lines, "share now") or [None])[0]
    if btn is None:
        _close_sheets(serial, d)
        return False
    if caption:
        say = (find_phrase(lines, "say something about this") or [None])[0]
        if say is not None:
            tap(serial, *center(say))
            time.sleep(1.0)
            paste_text(serial, d, caption)
            time.sleep(0.8)
            lines, _ = ocr(d)
            btn = (find_phrase(lines, "share now") or [None])[0]
            if btn is None:                        # keyboard covers it: hide the keyboard
                back(serial)
                time.sleep(1.0)
                lines, _ = ocr(d)
                btn = (find_phrase(lines, "share now") or [None])[0]
            if btn is None:
                _close_sheets(serial, d)
                return False
    tap(serial, *center(btn))
    time.sleep(3.5)
    lines, _ = ocr(d)
    done = not find_phrase(lines, "share now")
    if not done:
        _close_sheets(serial, d)
    return done


def find_action_row_like_point(d):
    rows = action_rows(d)
    return center(rows[0]["cells"][0]) if rows else None


def react(d, reaction):
    """Compatibility: react to the first post on screen."""
    rows = action_rows(d)
    if not rows:
        return "failed:no-post"
    return react_row(_serial(d), d, rows[0], reaction)


def post_comment(serial, d, text):
    rows = action_rows(d)
    if not rows:
        return "failed:no-post"
    return comment_row(serial, d, rows[0], text)


# ── friends ─────────────────────────────────────────────────────────────────
def blue_button_hits(d, phrase):
    """Centres of OCR'd `phrase` that sit on a solid FB-blue button (e.g. Confirm)."""
    lines, img = ocr(d)
    out = []
    for b in find_phrase(lines, phrase, exact_line=True):
        x1, y1, x2, y2 = b
        probe = [(x1 - 12, (y1 + y2) // 2), (x2 + 12, (y1 + y2) // 2), ((x1 + x2) // 2, y1 - 10)]
        if img is not None and sum(is_fb_blue(_px(img, x, y)) for x, y in probe) >= 2:
            out.append(center(b))
    return out


def text_hits(d, phrase, exact_line=True):
    lines, _ = ocr(d)
    return [center(b) for b in find_phrase(lines, phrase, exact_line=exact_line)]


# ── messenger (accessible) ──────────────────────────────────────────────────
def unread_chats(d):
    """[(name, x, y)] of unread conversations in the Messenger list."""
    out = []
    for x1, y1, x2, y2, ck, _t, desc, _c in nodes(d):
        if desc.lower().startswith("unread,"):
            parts = [p.strip() for p in desc.split(",")]
            name = parts[1] if len(parts) > 1 else ""
            if name:
                out.append((name, (x1 + x2) // 2, (y1 + y2) // 2))
    return sorted(out, key=lambda c: c[2])


def dismiss_messenger_popups(serial, d):
    """PIN-restore prompts, 'Previews are on' sheets, tips. Never sets or enters a PIN."""
    for _ in range(4):
        lines, _ = ocr(d)
        txt = " ".join(l.text.lower() for l in lines)
        if "are you sure you want to skip" in txt:
            hit = find_phrase(lines, "yes")
            if hit:
                tap(serial, *center(hit[0]))
                time.sleep(2)
                continue
        if "restore your messages" in txt or "entering your pin" in txt:
            hit = find_phrase(lines, "skip")
            if hit:
                tap(serial, *center(hit[0]))
                time.sleep(2)
                continue
        if "previews are on" in txt:
            w, h = screen_wh(d)
            sh(serial, "input", "swipe", str(w // 2), str(int(h * 0.58)), str(w // 2), str(int(h * 0.97)), "300")
            time.sleep(1.5)
            continue
        hit = None
        for lbl in ("not now", "got it", "close", "ok", "maybe later", "dismiss"):
            hit = find_phrase(lines, lbl, exact_line=True)
            if hit:
                break
        if hit:
            tap(serial, *center(hit[0]))
            time.sleep(1.5)
            continue
        return


def send_chat(serial, d, text):
    """Paste `text` into the chat composer and press Send. Never presses 'Send Like'."""
    try:
        el = d(className="android.widget.EditText")
        if not el.exists:
            return False
        b = el.info.get("bounds", {})
        tap(serial, (b["left"] + b["right"]) // 2, (b["top"] + b["bottom"]) // 2)
        time.sleep(0.9)
        paste_text(serial, d, text)
        time.sleep(0.8)
        for x1, y1, x2, y2, _ck, _t, desc, _c in nodes(d):
            low = desc.lower()
            if low.startswith("send") and "like" not in low and "sticker" not in low:
                tap(serial, (x1 + x2) // 2, (y1 + y2) // 2)
                time.sleep(2)
                hide_keyboard(serial)            # so the next Back leaves the thread
                return True
    except Exception:
        pass
    return False


# ── accounts (switching not mapped for FB Lite yet) ─────────────────────────
_BLOCK_SIGNS = ("log in", "create new account", "forgot password", "your account has been",
                "we suspended your account", "account locked")


def wait_for_account_block(d, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        low = visible_text(d).lower()
        if has_nav(d):
            return ""
        for s in _BLOCK_SIGNS:
            if s in low:
                return s
        time.sleep(1)
    return ""


def account_rows(d):
    return []


def find_switch_account_arrow(d):
    return None


def chooser_present(serial, d):
    return False


def dismiss_chooser(serial, d):
    return None


# ── AI text (Pollinations) ──────────────────────────────────────────────────
# Without a key Pollinations allows ONE reply every ~45 s per internet connection, and every phone
# shares this PC's connection. So AI calls are queued through one pacer (all phones / threads);
# a phone waits its turn (bounded) instead of hitting the limit. A Pollinations key ("ai_key" in
# fbdaily/config.json, or the POLLINATIONS_KEY environment variable) lifts the limit.
_AI_URL = "https://text.pollinations.ai/openai"
AI_KEY = os.environ.get("POLLINATIONS_KEY", "")
AI_MIN_GAP = 50.0                                   # seconds between keyless calls (a refused call restarts the window)
AI_MAX_WAIT = 150.0                                 # give up (skip this comment) after waiting this long
_AI_LOCK = threading.Lock()
_AI_LAST = [0.0]


def _ai_post(msgs, timeout):
    body = json.dumps({"model": "openai", "messages": msgs, "private": True}).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "fbdaily/1.0"}
    if AI_KEY:
        headers["Authorization"] = f"Bearer {AI_KEY}"
    req = urllib.request.Request(_AI_URL, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return data["choices"][0]["message"]["content"] or ""


def ai_reply(prompt, system="", timeout=40):
    """One AI completion. Raises on failure (callers fall back / skip)."""
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    deadline = time.time() + AI_MAX_WAIT
    if not _AI_LOCK.acquire(timeout=AI_MAX_WAIT):
        raise TimeoutError("AI queue busy")
    try:
        for _attempt in range(3):
            if not AI_KEY:
                wait = _AI_LAST[0] + AI_MIN_GAP - time.time()
                if wait > 0:
                    if time.time() + wait > deadline:
                        raise TimeoutError("AI free limit: no slot in time")
                    time.sleep(wait)
            try:
                _AI_LAST[0] = time.time()
                return _ai_post(msgs, timeout)
            except urllib.error.HTTPError as e:
                if e.code in (402, 429):           # limited: the window restarts now, wait a full gap
                    _AI_LAST[0] = time.time()
                    continue
                raise
        raise TimeoutError("AI rate limited")
    finally:
        _AI_LOCK.release()


def sanitize_comment(text, fallback=""):
    t = (text or "").strip()
    t = re.split(r"\n\s*(---|\*\*Sponsor|🌸|Powered by)", t)[0]          # drop sponsor footers
    t = next((ln.strip() for ln in t.splitlines() if ln.strip()), "")
    t = re.sub(r"^(comment|reply|caption|me|answer)\s*:\s*", "", t, flags=re.I)
    t = t.strip().strip('"“”\'').strip()
    if not t or len(t) > 220 or "pollinations" in t.lower() or t.lower().startswith(("as an ai", "i can't", "sorry")):
        return fallback
    return t


_POOL = ["Ang galing nito!", "Grabe, ang ganda 😍", "Nice one!", "Love this ❤️", "Wow, amazing!",
         "Ang cute naman", "Sobrang ganda talaga", "Galing! 👏", "Congrats po!", "Haha ang kulit 😂"]
_RECENT = []


def unique_comment(example=""):
    pool = [example] if example else []
    pool += [c for c in _POOL if c not in _RECENT]
    c = random.choice(pool or _POOL)
    _RECENT.append(c)
    del _RECENT[:-6]
    return c


_TL = {"ang", "ng", "mga", "sa", "ko", "mo", "po", "naman", "talaga", "grabe", "ganda", "galing",
       "salamat", "kayo", "ikaw", "ako", "hindi", "oo", "na", "pa", "lang", "din", "rin", "yung", "kasi",
       "sobra", "sana", "wala", "meron", "tayo", "kami", "nyo", "niya", "siya", "ba", "naku", "hala"}


def _detect_lang(text):
    words = set(re.findall(r"[a-zñ']+", (text or "").lower()))
    if len(words & _TL) >= 1:
        return "tl"
    if re.search(r"[a-z]", (text or "").lower()):
        return "en"
    return "other"
