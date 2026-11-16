"""
Inscribed Ultimatum -> Divination Card overlay for Path of Exile 1  (screen reading)

Hover an Inscribed Ultimatum, keep the mouse still for a moment, and a pop-up appears
next to the tooltip. Move the mouse and it disappears. Press F8 to force a scan.
  - Ultimatum that asks for divination cards: the card (name, stack size, reward)
  - Ultimatum whose reward is a unique item:  the unique item's picture

The tool only takes a screenshot around the mouse cursor and reads the text with
OCR. It never touches the game client or its memory.

Card and unique item data is downloaded from poedb.tw on the first start (internet
needed) and stored in the folder "card_data" next to this file. Item pictures are
downloaded the first time an item is shown. Delete the .json files in that folder
to force a fresh download.

Want your own full card pictures (e.g. screenshots of the real cards)?
Put them in the folder "card_images" and name them after the card, e.g.
    card_images\\The Fox in the Brambles.png      (works for unique items too)
Those pictures are shown instead of the built-in layout.

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
from urllib.parse import quote

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
POPUP_SHIFT_CM = 3        # extra distance (cm) of the pop-up from the tooltip, away from it
MAX_SECONDS = 60          # safety: pop-up never stays longer than this

POEDB_URL = "https://poedb.tw/us/Divination_Cards"
ULTIMATUM_URL = "https://poedb.tw/us/Inscribed_Ultimatum"
ITEM_URL = "https://poedb.tw/us/"
TRADE_ITEMS_URL = "https://www.pathofexile.com/api/trade/data/items"
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
UNIQUES_JSON = os.path.join(DATA_DIR, "uniques.json")
TRADE_NAMES_JSON = os.path.join(DATA_DIR, "all_unique_names.json")
UNIQUE_IMG_DIR = os.path.join(DATA_DIR, "unique_img")
for _d in (DATA_DIR, OWN_DIR, UNIQUE_IMG_DIR):
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


def _norm(x):
    return " ".join(str(x).split())          # also turns non-breaking spaces into normal ones


def parse_poedb(html):
    """Read the card list from the poedb.tw page (section 'Divination Cards Item')."""
    from bs4 import BeautifulSoup, Comment
    soup = BeautifulSoup(html, "html.parser")
    start = None
    for s in soup.find_all(string=re.compile(r"Divination\s+Cards\s+Item")):
        if s.find_parent(["a", "nav", "script", "style", "title"]) is None:
            start = s
            break
    if start is None:
        print("[info] section 'Divination Cards Item' not found on the page")
        return {}
    raw = {}
    cur = None
    for node in start.find_all_next(string=True):
        if isinstance(node, Comment) or node.find_parent(["script", "style"]) is not None:
            continue
        text = _norm(node)
        if not text:
            continue
        if re.match(r"(Divination Cards Atlas Tree|Disabled Item|Areas /)", text):
            break
        a = node.find_parent("a")
        href = a.get("href", "") if a is not None else ""
        if a is not None and href and not href.startswith(("#", "javascript")):
            cur = raw.setdefault(text, [])       # a link with text = a card name
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


def parse_unique_list(html):
    """Unique items that can appear in Inscribed Ultimatums (table 'Uniques' on poedb.tw)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    heading = None
    for t in soup.find_all(string=re.compile(r"Uniques\s*/\s*\d+")):
        if t.find_parent(["a", "nav", "script", "style", "title"]) is None:
            heading = t
            break
    table = heading.find_next("table") if heading is not None else None
    out = {}
    if table is None:
        return out
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        tier = _norm(tds[0].get_text(" "))
        name = _norm(tds[1].get_text(" "))
        if not name:
            continue
        a = tds[1].find("a")
        href = a.get("href", "") if a is not None else ""
        slug = href.rstrip("/").split("/")[-1] if href else name.replace(" ", "_")
        out[name] = {"name": name, "tier": tier, "slug": slug}
    return out


uniques = {}   # unique item name -> {"name", "tier", "slug"}


def load_table(label, url, path, parser, target, min_count, probes):
    exists = os.path.exists(path)
    stale = exists and (time.time() - os.path.getmtime(path)) > 14 * 86400
    if not exists or stale:
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = parser(r.text)
            print(f"[info] poedb.tw: {len(data)} {label} found")
            if len(data) >= min_count:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
            else:
                print(f"[warn] too few {label} found, the page layout may have changed.")
                for key in probes:
                    i = r.text.find(key)
                    if i >= 0:
                        print("       page code around the first entry:")
                        print("       " + r.text[max(0, i - 400): i + 800].replace("\n", " "))
                        break
                else:
                    print("       start of the page:", r.text[:300].replace("\n", " "))
        except Exception as e:
            print(f"[warn] could not download {label}: {e}")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            target.update(json.load(f))
    print(f"[ok] {len(target)} {label} known")


def name_to_slug(name):
    """'Doryani's Fist' -> 'Doryanis_Fist', 'Jack, the Axe' -> 'Jack%2C_the_Axe'"""
    return quote(re.sub(r"['\u2019`]", "", name).replace(" ", "_"), safe="_-.~")


def load_all_unique_names():
    """All unique item names from the official trade site data. The Ultimatum list on poedb
    does not contain every possible reward (e.g. drop-restricted uniques like The Immortal Will)."""
    names = []
    if os.path.exists(TRADE_NAMES_JSON) and \
            (time.time() - os.path.getmtime(TRADE_NAMES_JSON)) < 14 * 86400:
        with open(TRADE_NAMES_JSON, encoding="utf-8") as f:
            names = json.load(f)
    else:
        try:
            r = requests.get(TRADE_ITEMS_URL, timeout=30, headers={
                **HEADERS, "User-Agent": "ultimatum-card-overlay/2.1 (personal hobby tool)"})
            r.raise_for_status()
            found = set()
            for group in r.json().get("result", []):
                for e in group.get("entries", []):
                    if (e.get("flags") or {}).get("unique") and e.get("name"):
                        found.add(e["name"])
            names = sorted(found)
            print(f"[info] trade site: {len(names)} unique item names found")
            if len(names) >= 300:
                with open(TRADE_NAMES_JSON, "w", encoding="utf-8") as f:
                    json.dump(names, f, ensure_ascii=False)
        except Exception as e:
            print(f"[warn] could not download the full unique item list: {e}")
            if os.path.exists(TRADE_NAMES_JSON):
                with open(TRADE_NAMES_JSON, encoding="utf-8") as f:
                    names = json.load(f)
    added = 0
    for n in names:
        if n not in uniques:
            uniques[n] = {"name": n, "tier": "-", "slug": name_to_slug(n)}
            added += 1
    print(f"[ok] {len(uniques)} unique items known ({added} not on the Ultimatum list)")


def load_cards():
    load_table("cards", POEDB_URL, CARDS_JSON, parse_poedb, cards, 100,
               ("Three_Faces_in_the_Dark", "The_Doctor", "Divination Cards Item"))
    load_table("unique items", ULTIMATUM_URL, UNIQUES_JSON, parse_unique_list, uniques, 100,
               ("Goldwyrm", "Uniques /"))
    load_all_unique_names()


def find_og_image(html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for attr in ("property", "name"):
        tag = soup.find("meta", attrs={attr: "og:image"})
        if tag is not None and tag.get("content"):
            return tag["content"]
    m = re.search(r"https://cdn\.poedb\.tw/image/Art/2DItems/[^\"'\s)]+\.webp", html)
    return m.group(0) if m else None


def fetch_unique_image(u):
    """Item picture from the item's poedb.tw page, cached in card_data/unique_img."""
    path = os.path.join(UNIQUE_IMG_DIR, safe(u["name"]) + ".png")
    if not os.path.exists(path):
        try:
            r = requests.get(ITEM_URL + u["slug"], headers=HEADERS, timeout=20)
            r.raise_for_status()
            url = find_og_image(r.text)
            if not url:
                print(f"[warn] no picture found on the page of {u['name']}")
                return None
            data = requests.get(url, headers=HEADERS, timeout=20).content
            Image.open(io.BytesIO(data)).convert("RGBA").save(path)
        except Exception as e:
            print(f"[warn] could not get the picture of {u['name']}: {e}")
            return None
    try:
        img = Image.open(path).convert("RGBA")
    except Exception:
        return None
    f = 200 / max(img.size)
    return img.resize((max(1, int(img.width * f)), max(1, int(img.height * f))), Image.LANCZOS)


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
    with (getattr(mss, "MSS", None) or mss.mss)() as sct:
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


def find_unique_reward(items):
    """Looks for 'Reward: <unique item>' and matches the name against the Ultimatum uniques.
    Returns (unique, tooltip_center_x) or None."""
    names = list(uniques.keys())
    if not names:
        return None
    best = None
    for it in items:
        x1, x2, cy, h, text = it
        label = text.split(":", 1)[0].strip().lower()
        if fuzz.ratio(label, "reward") < 75:
            continue
        left, right = x1, x2
        cand = text.split(":", 1)[1] if ":" in text else ""
        if not cand.strip():
            same = [o for o in items
                    if o is not it and abs(o[2] - cy) < max(h, 10) * 0.7 and o[0] >= x1]
            if same:
                nxt = min(same, key=lambda o: o[0])
                cand = nxt[4]
                right = max(right, nxt[1])
        cand = cand.strip()
        if len(cand) < 3:
            continue
        hit = process.extractOne(cand, names, scorer=fuzz.ratio,
                                 processor=utils.default_process, score_cutoff=MATCH_CUTOFF)
        if hit and (best is None or hit[1] > best[0]):
            best = (hit[1], hit[0], (left + right) / 2)
    if not best:
        return None
    return uniques[best[1]], best[2]


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
    reward = find_unique_reward(items)
    if reward:
        uniq, tip_cx = reward
        print(f"[hit] reward: {uniq['name']}")
        own = get_own_image(uniq)
        img = own if own is not None else fetch_unique_image(uniq)
        jobs.put(("unique", uniq, img, own is not None, pos, tip_cx))
        return
    found = find_card(items)
    if not found:
        if forced:
            print("[debug] no card found. Text read around the cursor:")
            for it in items[:40]:
                print("        ", it[4])
            jobs.put(("msg", "No Inscribed Ultimatum with a known card or unique reward found at the cursor.", pos))
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
    shift = int(root.winfo_fpixels("1c") * POPUP_SHIFT_CM)
    if tip_cx is None:
        x = cursor[0] + 30 + shift
    else:
        x = int(tip_cx + TOOLTIP_HALF_WIDTH + 12 + shift)            # right of the tooltip
        if x + w > sw - 10:
            x = int(tip_cx - TOOLTIP_HALF_WIDTH - 12 - shift - w)    # otherwise left of it
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


def build_unique(f, uniq, img, own):
    global photo_ref
    if own and img is not None:
        photo_ref = ImageTk.PhotoImage(img)
        tk.Label(f, image=photo_ref, bg=BG, bd=0).pack()
        return
    tk.Label(f, text=uniq["name"], bg=BG, fg="#af6025",
             font=("Georgia", 15, "bold"), wraplength=300).pack(pady=(0, 8))
    if img is not None:
        photo_ref = ImageTk.PhotoImage(img)
        tk.Label(f, image=photo_ref, bg=BG).pack()
    else:
        tk.Label(f, text="(no picture available)", bg=BG, fg="#7a7a7a",
                 font=("Segoe UI", 11)).pack(pady=6)
    tier = uniq.get("tier")
    info = "Unique reward" + (f"  |  tier {tier}" if tier and tier != "-" else "")
    tk.Label(f, text=info, bg=BG, fg="#9a9a9a", font=("Segoe UI", 9)).pack(pady=(8, 0))


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
            elif job[0] == "unique":
                _, uniq, img, own, cursor, tip_cx = job
                show_popup(lambda f: build_unique(f, uniq, img, own), cursor, tip_cx)
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
    if not cards and not uniques:
        print("[error] no data available. Connect to the internet and start again.")
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
