"""
Inscribed Ultimatum -> Divination Card overlay for Path of Exile 1  (screen reading)

Hover an Inscribed Ultimatum, keep the mouse still for a moment, and a card pop-up
(name, stack size, reward) appears next to the tooltip. Move the mouse and it
disappears. Press F8 to force a scan.

The tool only takes a screenshot around the mouse cursor and reads the text with
OCR. It never touches the game client or its memory.

Card data (names, stack sizes, rewards) is downloaded once from poedb.tw on the
first start (internet needed) and stored in the folder "card_data" next to this
file. Delete cards.json in that folder to force a fresh download.

Want your own full card pictures (e.g. screenshots of the real cards)?
Put them in the folder "card_images" and name them after the card, e.g.
    card_images\\The Fox in the Brambles.png
Those pictures are shown instead of the built-in card layout.

Run in windowed or borderless windowed mode (an overlay can't draw over exclusive fullscreen).
"""

import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk

import keyboard
import mss
import numpy as np
import requests
from PIL import Image, ImageTk
from rapidfuzz import fuzz, process, utils

# Correct pixel coordinates on scaled (125% / 150%) displays
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ------------------------------------------------------------------ settings
AUTO_MODE = True          # True: scan by itself when the mouse rests. False: only on hotkey
SCAN_HOTKEY = "f8"        # forces a scan right now
STILL_SECONDS = 0.6       # mouse must rest this long before an automatic scan
MOVE_TOLERANCE = 12       # pixels the mouse may move and still count as "resting"
REGION_HALF_WIDTH = 700   # screenshot size around the cursor (pixels)
REGION_UP = 500
REGION_DOWN = 500
MATCH_CUTOFF = 85         # 0-100, how close the OCR text must be to a real card name
TOOLTIP_HALF_WIDTH = 300  # pop-up is placed this far from the tooltip's centre
MAX_SECONDS = 60          # safety: pop-up never stays longer than this

POEDB_URL = "https://poedb.tw/us/Divination_Cards"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en",
}
BG = "#0d0b09"

# ------------------------------------------------------------------ paths
def base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE = base_dir()
DATA_DIR = os.path.join(BASE, "card_data")
OWN_DIR = os.path.join(BASE, "card_images")
CARDS_JSON = os.path.join(DATA_DIR, "cards.json")
for _d in (DATA_DIR, OWN_DIR):
    os.makedirs(_d, exist_ok=True)


def safe(name):
    return re.sub(r"[^\w\- ]+", "", name).strip()


# ------------------------------------------------------------------ card data
cards = {}   # card name -> {"name", "stack", "lines": [[text, color], ...]}


def line_color(text, classes):
    t, c = text.lower(), classes.lower()
    if "corrupt" in t:
        return "#d20000"
    if t == "disabled":
        return "#7a7a7a"
    if "unique" in c:
        return "#af6025"
    if "currency" in c:
        return "#aa9e82"
    if "gem" in c or re.search(r"\bgem\b", t):
        return "#1ba29b"
    if "magic" in c:
        return "#8888ff"
    if "rare" in c:
        return "#ffff77"
    return "#e6e0d2"


def parse_poedb(html):
    """Read the card list from the poedb.tw page (section 'Divination Cards Item')."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    heading = None
    for tag in soup.find_all(["h2", "h3", "h4", "h5", "h6"]):
        if tag.get_text(" ", strip=True).startswith("Divination Cards Item"):
            heading = tag
            break
    if heading is None:
        return {}
    raw = {}
    cur = None
    for node in heading.find_all_next(string=True):
        text = " ".join(str(node).split())
        if not text:
            continue
        if text.startswith("Divination Cards Atlas Tree"):
            break
        parent = node.parent
        href = parent.get("href", "") if parent is not None and parent.name == "a" else ""
        if href and re.match(r"^(https://poedb\.tw)?/us/[^#?]+$", href):
            cur = raw.setdefault(text, [])
            continue
        if cur is not None:
            cls = " ".join(" ".join(p.get("class", [])) for p in list(node.parents)[:3])
            cur.append((text, cls))
    out = {}
    for name, strings in raw.items():
        stack, lines, i = None, [], 0
        while i < len(strings):
            s, cls = strings[i]
            if s.lower().startswith("stack size"):
                buf, j = s, i
                while not re.search(r"\d+\s*/\s*\d+", buf) and j + 1 < len(strings) and j - i < 3:
                    j += 1
                    buf += " " + strings[j][0]
                m = re.search(r"\d+\s*/\s*([\d,]+)", buf)
                if m:
                    stack = m.group(1)
                i = j + 1
                continue
            lines.append([s, line_color(s, cls)])
            i += 1
        out[name] = {"name": name, "stack": stack, "lines": lines}
    return out


def load_cards():
    exists = os.path.exists(CARDS_JSON)
    stale = exists and (time.time() - os.path.getmtime(CARDS_JSON)) > 14 * 86400
    if not exists or stale:
        try:
            r = requests.get(POEDB_URL, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = parse_poedb(r.text)
            print(f"[info] poedb.tw: {len(data)} cards found")
            if len(data) >= 100:
                with open(CARDS_JSON, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
            else:
                print("[warn] too few cards found, the page layout may have changed.")
                print("       start of the page:", r.text[:300].replace("\n", " "))
        except Exception as e:
            print(f"[warn] could not download card data: {e}")
    if os.path.exists(CARDS_JSON):
        with open(CARDS_JSON, encoding="utf-8") as f:
            cards.update(json.load(f))
    print(f"[ok] {len(cards)} cards known")


def find_own_image(name):
    try:
        files = {safe(os.path.splitext(f)[0]).lower() + os.path.splitext(f)[1].lower(): f
                 for f in os.listdir(OWN_DIR)}
    except OSError:
        return None
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        f = files.get(safe(name).lower() + ext)
        if f:
            return os.path.join(OWN_DIR, f)
    return None


def get_own_image(card):
    path = find_own_image(card["name"])
    if not path:
        return None
    try:
        img = Image.open(path).convert("RGBA")
        img.thumbnail((500, 780))
        return img
    except Exception:
        return None


# ------------------------------------------------------------------ OCR
_ocr = None
_ocr_lock = threading.Lock()


def get_ocr():
    global _ocr
    with _ocr_lock:
        if _ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            _ocr = RapidOCR()
    return _ocr


def grab(cx, cy):
    with mss.mss() as sct:
        mon = sct.monitors[0]  # all screens together
        left = max(mon["left"], cx - REGION_HALF_WIDTH)
        top = max(mon["top"], cy - REGION_UP)
        right = min(mon["left"] + mon["width"], cx + REGION_HALF_WIDTH)
        bottom = min(mon["top"] + mon["height"], cy + REGION_DOWN)
        shot = sct.grab({"left": left, "top": top, "width": right - left, "height": bottom - top})
    return np.array(shot)[:, :, :3], left, top   # BGR


def strip_count(text):
    """'The Fox in the Brambles X4' -> ('The Fox in the Brambles', 4)"""
    text = text.strip()
    m = re.search(r"\s*[xX×]\s*(\d+)\s*$", text)
    if m:
        return text[: m.start()].strip(), int(m.group(1))
    m = re.search(r"\s+[xX×]\S{0,2}$", text)       # OCR garbled the number
    if m:
        return text[: m.start()].strip(), 1
    return text, 1


def find_card(items):
    """
    items: list of (x1, x2, cy, height, text) in screen coordinates.
    Looks for the 'Requires Sacrifice:' line and matches the name after it
    against all known divination cards.
    Returns (card, amount, tooltip_center_x) or None.
    """
    names = list(cards.keys())
    best = None
    for it in items:
        x1, x2, cy, h, text = it
        if len(text) < 9 or fuzz.partial_ratio("sacrifice", text.lower()) < 80:
            continue
        left, right = x1, x2
        cand = text.split(":", 1)[1] if ":" in text else ""
        if not cand.strip():
            # the name may be its own text box right of the label
            same = [o for o in items
                    if o is not it and abs(o[2] - cy) < max(h, 10) * 0.7 and o[0] >= x1]
            if same:
                nxt = min(same, key=lambda o: o[0])
                cand = nxt[4]
                right = max(right, nxt[1])
        cand, amount = strip_count(cand)
        if len(cand) < 3:
            continue
        hit = process.extractOne(cand, names, scorer=fuzz.ratio,
                                 processor=utils.default_process, score_cutoff=MATCH_CUTOFF)
        if hit and (best is None or hit[1] > best[0]):
            best = (hit[1], hit[0], amount, (left + right) / 2)
    if not best:
        return None
    return cards[best[1]], best[2], best[3]


# ------------------------------------------------------------------ worker
jobs = queue.Queue()              # worker thread -> UI thread
popup_visible = threading.Event()
force = threading.Event()


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def get_cursor_pos():
    p = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def scan(pos, forced=False):
    img, left, top = grab(*pos)
    result, _ = get_ocr()(img)
    items = []
    for box, text, _score in (result or []):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        items.append((left + min(xs), left + max(xs),
                      top + (min(ys) + max(ys)) / 2, max(ys) - min(ys), text))
    found = find_card(items)
    if not found:
        if forced:
            jobs.put(("msg", "No Inscribed Ultimatum with a known card found at the cursor.", pos))
        return
    card, amount, tip_cx = found
    print(f"[hit] {card['name']} (x{amount})")
    jobs.put(("card", card, amount, get_own_image(card), pos, tip_cx))


def watcher():
    anchor, since, scanned = None, time.time(), None
    while True:
        time.sleep(0.12)
        try:
            pos = get_cursor_pos()
            now = time.time()
            if force.is_set():
                force.clear()
                jobs.put(("hide",))
                time.sleep(0.25)
                pos = get_cursor_pos()
                scan(pos, forced=True)
                anchor, since, scanned = pos, time.time(), pos
                continue
            if (anchor is None or abs(pos[0] - anchor[0]) > MOVE_TOLERANCE
                    or abs(pos[1] - anchor[1]) > MOVE_TOLERANCE):
                anchor, since, scanned = pos, now, None
                if popup_visible.is_set():
                    jobs.put(("hide",))
                continue
            if AUTO_MODE and scanned is None and now - since >= STILL_SECONDS:
                scanned = anchor
                scan(pos)
        except Exception as e:
            print(f"[error] {e}")
            time.sleep(1)


# ------------------------------------------------------------------ UI
root = None
popup = None
hide_job = None
photo_ref = None


def close_popup():
    global popup, hide_job
    if hide_job is not None:
        root.after_cancel(hide_job)
        hide_job = None
    if popup is not None:
        popup.destroy()
        popup = None
    popup_visible.clear()


def make_click_through(win):
    """No focus stealing, mouse clicks go through to the game."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(win.winfo_id())
        style = user32.GetWindowLongW(hwnd, -20)
        # LAYERED | TRANSPARENT | TOOLWINDOW | NOACTIVATE
        user32.SetWindowLongW(hwnd, -20, style | 0x80000 | 0x20 | 0x80 | 0x08000000)
    except Exception:
        pass


def show_popup(build, cursor, tip_cx):
    global popup, hide_job
    close_popup()
    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.attributes("-alpha", 0.97)
    win.configure(bg="#c9a55c")
    frame = tk.Frame(win, bg=BG, padx=16, pady=12)
    frame.pack(padx=2, pady=2)
    build(frame)
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    if tip_cx is None:
        x = cursor[0] + 30
    else:
        x = int(tip_cx + TOOLTIP_HALF_WIDTH + 12)          # right of the tooltip
        if x + w > sw - 10:
            x = int(tip_cx - TOOLTIP_HALF_WIDTH - 12 - w)  # otherwise left of it
    x = max(10, min(x, sw - w - 10))
    y = max(10, min(cursor[1] - h // 2, sh - h - 10))
    win.geometry(f"+{x}+{y}")
    make_click_through(win)
    popup = win
    popup_visible.set()
    hide_job = root.after(MAX_SECONDS * 1000, close_popup)


def build_card(f, card, amount, img):
    global photo_ref
    if img is not None:                            # your own full card picture, shown as is
        photo_ref = ImageTk.PhotoImage(img)
        tk.Label(f, image=photo_ref, bg=BG, bd=0).pack()
        return
    tk.Label(f, text=card["name"], bg=BG, fg="#e8d9b0",
             font=("Georgia", 15, "bold"), wraplength=300).pack(pady=(0, 8))
    tk.Frame(f, bg="#6b5a37", height=1).pack(fill="x", pady=2)
    if card.get("stack"):
        tk.Label(f, text=f"Stack size: {card['stack']}", bg=BG, fg="#ffffff",
                 font=("Segoe UI", 12, "bold")).pack(pady=(8, 8))
    tk.Frame(f, bg="#6b5a37", height=1).pack(fill="x", pady=2)
    for text, color in card.get("lines", []):
        tk.Label(f, text=text, bg=BG, fg=color, font=("Segoe UI", 13, "bold"),
                 wraplength=300, justify="center").pack(pady=1)
    if not card.get("lines"):
        tk.Label(f, text="(no reward data)", bg=BG, fg="#7a7a7a",
                 font=("Segoe UI", 11)).pack(pady=6)


def build_msg(f, msg):
    tk.Label(f, text=msg, bg=BG, fg="#d20000", font=("Segoe UI", 10), wraplength=280).pack()


def poll():
    try:
        while True:
            job = jobs.get_nowait()
            if job[0] == "hide":
                close_popup()
            elif job[0] == "card":
                _, card, amount, img, cursor, tip_cx = job
                show_popup(lambda f: build_card(f, card, amount, img), cursor, tip_cx)
            elif job[0] == "msg":
                msg, cursor = job[1], job[2]
                show_popup(lambda f: build_msg(f, msg), cursor, None)
    except queue.Empty:
        pass
    root.after(100, poll)


# ------------------------------------------------------------------ main
def main():
    global root
    load_cards()
    if not cards:
        print("[error] no card data available. Connect to the internet and start again.")
        input("Press Enter to close...")
        return
    root = tk.Tk()
    root.withdraw()
    threading.Thread(target=lambda: (get_ocr(), print("[ok] text recognition ready")),
                     daemon=True).start()
    try:
        keyboard.add_hotkey(SCAN_HOTKEY, force.set)
    except Exception as e:
        print(f"[warn] hotkey not available ({e}); automatic mode still works")
    threading.Thread(target=watcher, daemon=True).start()
    mode = "automatic (rest the mouse on the item)" if AUTO_MODE else f"hotkey only ({SCAN_HOTKEY.upper()})"
    print(f"Overlay running, mode: {mode}. Close this window to quit.")
    root.after(100, poll)
    root.mainloop()


if __name__ == "__main__":
    main()
