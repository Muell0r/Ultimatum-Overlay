"""
Inscribed Ultimatum -> Divination Card overlay for Path of Exile 1  (screen reading)

Hover an Inscribed Ultimatum, stop the mouse for a moment, and a pop-up with the
card it asks you to sacrifice appears next to the tooltip. Move the mouse and it
disappears. Press F8 to force a scan.

The tool only takes a screenshot around the mouse cursor and reads the text with
OCR. It never touches the game client or its memory.

Card data and art are downloaded once on the first start (needs internet) and are
then stored in the folder "card_data" next to this file.

Want your own full card pictures (like a screenshot of the real card)?
Put them in the folder "card_images" and name them after the card, e.g.
    card_images\\The Fox in the Brambles.png
Those pictures are shown instead of the built-in card layout.

Run in windowed or borderless windowed mode (an overlay can't draw over exclusive fullscreen).
"""

import ctypes
import io
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

NINJA_URL = "https://poe.ninja/api/data/itemoverview"
HEADERS = {"User-Agent": "ultimatum-card-overlay/2.0"}
BG = "#0d0b09"

# ------------------------------------------------------------------ paths
def base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE = base_dir()
DATA_DIR = os.path.join(BASE, "card_data")
IMG_DIR = os.path.join(DATA_DIR, "img")
OWN_DIR = os.path.join(BASE, "card_images")
CARDS_JSON = os.path.join(DATA_DIR, "cards.json")
for _d in (IMG_DIR, OWN_DIR):
    os.makedirs(_d, exist_ok=True)


def safe(name):
    return re.sub(r"[^\w\- ]+", "", name).strip()


# ------------------------------------------------------------------ card data
cards = {}   # card name -> {"name", "stack", "reward", "flavour", "icon"}


API = "https://poe.ninja"


def api_get(path, **params):
    r = requests.get(API + path, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def _entry(name, stack=None, mods=None, flavour=None, icon=None):
    reward = ""
    if mods and isinstance(mods, list) and isinstance(mods[0], dict):
        reward = mods[0].get("text", "") or ""
    return {"name": name, "stack": stack, "reward": reward,
            "flavour": flavour or "", "icon": icon}


def parse_lines(data):
    """poe.ninja item overview: lines[] with name, stackSize, explicitModifiers, ..."""
    out = {}
    lines = data.get("lines") if isinstance(data, dict) else None
    for line in lines or []:
        if isinstance(line, dict) and line.get("name"):
            out[line["name"]] = _entry(line["name"], line.get("stackSize"),
                                       line.get("explicitModifiers"),
                                       line.get("flavourText"), line.get("icon"))
    return out


def parse_core_items(data):
    """poe.ninja exchange overview: item names and icons are listed in core.items"""
    out = {}
    core = data.get("core") if isinstance(data, dict) else None
    items = core.get("items") if isinstance(core, dict) else None
    if isinstance(items, dict):
        items = list(items.values())
    for it in items or []:
        if not isinstance(it, dict) or not it.get("name"):
            continue
        icon = it.get("icon") or it.get("image")
        if icon and not str(icon).startswith("http"):
            icon = "https://web.poecdn.com/" + str(icon).lstrip("/")
        out[it["name"]] = _entry(it["name"], it.get("stackSize"),
                                 it.get("explicitModifiers"), it.get("flavourText"), icon)
    return out


def merge(into, new):
    for name, c in new.items():
        cur = into.setdefault(name, _entry(name))
        for k, v in c.items():
            if v and not cur.get(k):
                cur[k] = v


def fetch_cards():
    """Cards are identical in every league, so any league works as the source."""
    ids = []
    try:
        for lg in api_get("/poe1/api/economy/leagues"):
            ids.append(lg.get("id") or lg.get("name"))
    except Exception as e:
        print(f"[info] league list not available: {e}")
    ids.append("Standard")
    ids = [i for n, i in enumerate(ids) if i and i not in ids[:n]]
    attempts = [
        ("/poe1/api/economy/stash/current/item/overview", parse_lines),
        ("/poe1/api/economy/exchange/current/overview", parse_core_items),
        ("/api/data/itemoverview", parse_lines),
    ]
    out = {}
    for league in ids:
        for path, parser in attempts:
            try:
                data = api_get(path, league=league, type="DivinationCard")
            except Exception as e:
                print(f"[info] {path} (league {league}): {e}")
                continue
            got = parser(data)
            print(f"[info] {path} (league {league}): {len(got)} cards")
            if not got:
                print("       sample:", json.dumps(data)[:600])
            merge(out, got)
        if out:
            break
    return out


def load_cards():
    exists = os.path.exists(CARDS_JSON)
    stale = exists and (time.time() - os.path.getmtime(CARDS_JSON)) > 14 * 86400
    if not exists or stale:
        try:
            data = fetch_cards()
            if data:
                with open(CARDS_JSON, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                print(f"[ok] downloaded data for {len(data)} cards")
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


def cached_icon(card):
    path = os.path.join(IMG_DIR, safe(card["name"]) + ".png")
    if os.path.exists(path):
        return path
    if not card.get("icon"):
        return None
    try:
        data = requests.get(card["icon"], headers=HEADERS, timeout=15).content
        Image.open(io.BytesIO(data)).convert("RGBA").save(path)
        return path
    except Exception:
        return None


def get_card_image(card):
    """Returns (PIL image or None, is_own_image)."""
    own = find_own_image(card["name"])
    if own:
        try:
            img = Image.open(own).convert("RGBA")
            img.thumbnail((500, 780))
            return img, True
        except Exception:
            pass
    path = cached_icon(card)
    if path:
        try:
            return Image.open(path).convert("RGBA"), False
        except Exception:
            pass
    return None, False


def prefetch_images():
    """Download all card art in the background so later lookups work offline."""
    for card in list(cards.values()):
        cached_icon(card)
        time.sleep(0.03)
    print("[ok] card art cached")


TAG_COLORS = {
    "uniqueitem": "#af6025", "rareitem": "#ffff77", "magicitem": "#8888ff",
    "normal": "#c8c8c8", "currencyitem": "#aa9e82", "gemitem": "#1ba29b",
    "corrupted": "#d20000", "default": "#c8c8c8", "enchanted": "#b4b4ff",
}


def parse_reward(markup):
    parts = re.findall(r"<(\w+)>\{(.*?)\}", markup or "", re.S)
    if parts:
        return [(TAG_COLORS.get(tag, "#c8c8c8"), txt.strip()) for tag, txt in parts]
    plain = re.sub(r"<[^>]+>|[{}]", "", markup or "").strip()
    return [("#c8c8c8", plain)] if plain else []


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
    img_card, own = get_card_image(card)
    jobs.put(("card", card, amount, img_card, own, pos, tip_cx))


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
    frame = tk.Frame(win, bg=BG, padx=12, pady=10)
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


def build_card(f, card, amount, img, own):
    global photo_ref
    if own and img is not None:                    # your own full card picture, shown as is
        photo_ref = ImageTk.PhotoImage(img)
        tk.Label(f, image=photo_ref, bg=BG, bd=0).pack()
        return
    tk.Label(f, text=card["name"], bg=BG, fg="#e8d9b0",
             font=("Georgia", 14, "bold")).pack(pady=(0, 6))
    if img is not None:
        photo_ref = ImageTk.PhotoImage(img)
        tk.Label(f, image=photo_ref, bg=BG).pack()
    if card.get("stack"):
        tk.Label(f, text=f"Stack size: {card['stack']}", bg=BG, fg="#ffffff",
                 font=("Segoe UI", 11, "bold")).pack(pady=(8, 2))
    for color, txt in parse_reward(card.get("reward", "")):
        tk.Label(f, text=txt, bg=BG, fg=color, font=("Segoe UI", 11),
                 wraplength=280, justify="center").pack()
    flavour = re.sub(r"<[^>]+>|[{}]", "", card.get("flavour", "")).strip()
    if flavour:
        tk.Label(f, text=flavour, bg=BG, fg="#c8741c", font=("Georgia", 9, "italic"),
                 wraplength=280, justify="center").pack(pady=(8, 0))


def build_msg(f, msg):
    tk.Label(f, text=msg, bg=BG, fg="#d20000", font=("Segoe UI", 10), wraplength=280).pack()


def poll():
    try:
        while True:
            job = jobs.get_nowait()
            if job[0] == "hide":
                close_popup()
            elif job[0] == "card":
                _, card, amount, img, own, cursor, tip_cx = job
                show_popup(lambda f: build_card(f, card, amount, img, own), cursor, tip_cx)
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
    threading.Thread(target=prefetch_images, daemon=True).start()
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
