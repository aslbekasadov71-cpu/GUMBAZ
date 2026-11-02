#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gumbaz Express — Telegram botlar + Mini App server (v3).

  • Mijozlar boti: /start → telefon → joylashuv → Menyu (Mini App) → buyurtma.
  • Admin bot: rahbar va xodimlar. Yangi buyurtmalar, holat tugmalari, Admin panel (Mini App yoki brauzer).
    Rahbar (ADMIN_IDS) xodimlarga login/parol beradi va har biriga ruxsatlarni belgilaydi.
  • ADMIN_BOT_TOKEN bo'lmasa — hammasi bitta botda (/admin buyrug'i).
  • Render (bepul) uchun: webhook rejimi va Telegram'dagi zaxira (disk o'chsa ham ma'lumot qoladi).

Faqat Python 3.9+ standart kutubxonasi.  Ishga tushirish:  python3 bot.py
"""
import base64
import hashlib
import hmac
import html
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import signal
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("TZ", "Asia/Tashkent")
if hasattr(time, "tzset"):
    time.tzset()

ROOT = os.path.dirname(os.path.abspath(__file__))
WEBAPP_DIR = os.path.join(ROOT, "webapp")
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(ROOT, "data")
UPLOAD_DIR = os.path.join(WEBAPP_DIR, "uploads")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gumbaz")


# ═════════════════════════════ Config ═════════════════════════════
def _cast(v, like):
    if isinstance(like, bool):
        return str(v).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(like, int):
        return int(float(v))
    if isinstance(like, float):
        return float(v)
    if isinstance(like, list):
        return [int(x) for x in re.split(r"[,\s]+", str(v).strip("[] ")) if x]
    return str(v)


def load_config():
    with open(os.path.join(ROOT, "config.example.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    path = os.environ.get("GUMBAZ_CONFIG", os.path.join(ROOT, "config.json"))
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    on_render = bool(os.environ.get("RENDER"))
    cfg.setdefault("WEBHOOK", on_render)
    cfg.setdefault("BACKUP", "telegram" if on_render else "")
    for k in list(cfg) + ["BOT_TOKEN", "OWNER_LOGIN", "OWNER_PASSWORD", "TG_API"]:
        if os.environ.get(k):
            cfg[k] = _cast(os.environ[k], cfg.get(k, ""))
    if cfg.get("BOT_TOKEN") and not os.environ.get("CLIENT_BOT_TOKEN"):
        cfg["CLIENT_BOT_TOKEN"] = cfg["BOT_TOKEN"]
    if not cfg.get("CLIENT_BOT_TOKEN") or "BotFather" in cfg["CLIENT_BOT_TOKEN"]:
        sys.exit("CLIENT_BOT_TOKEN (yoki BOT_TOKEN) ni kiriting.")
    if not cfg.get("ADMIN_BOT_TOKEN") or "BotFather" in str(cfg["ADMIN_BOT_TOKEN"]):
        cfg["ADMIN_BOT_TOKEN"] = cfg["CLIENT_BOT_TOKEN"]  # bitta bot rejimi
    if os.environ.get("RENDER_EXTERNAL_URL") and ("example" in cfg.get("WEBAPP_URL", "") or not cfg.get("WEBAPP_URL")):
        cfg["WEBAPP_URL"] = os.environ["RENDER_EXTERNAL_URL"]
    cfg["WEBAPP_URL"] = cfg["WEBAPP_URL"].rstrip("/")
    cfg.setdefault("TG_API", "https://api.telegram.org")
    cfg["ADMIN_IDS"] = [int(x) for x in cfg.get("ADMIN_IDS", [])]
    cfg["ADMIN_CHAT_ID"] = int(cfg.get("ADMIN_CHAT_ID") or 0)
    if os.environ.get("PORT"):
        cfg["PORT"] = int(os.environ["PORT"])
    cfg["WEBHOOK"] = _cast(cfg["WEBHOOK"], True)
    cfg["BACKUP"] = str(cfg["BACKUP"] or "").lower()
    return cfg


CFG = load_config()
CLIENT, ADMIN = "client", "admin"
TOKENS = {CLIENT: CFG["CLIENT_BOT_TOKEN"], ADMIN: CFG["ADMIN_BOT_TOKEN"]}
SINGLE_BOT = TOKENS[CLIENT] == TOKENS[ADMIN]

SETTING_KEYS = {
    "RESTAURANT_NAME": str, "RESTAURANT_ADDRESS_UZ": str, "RESTAURANT_ADDRESS_RU": str,
    "RESTAURANT_LAT": float, "RESTAURANT_LON": float, "SUPPORT_PHONE": str, "SUPPORT_USERNAME": str,
    "WORK_HOURS": str, "MIN_ORDER": int, "ACCEPTING": bool, "CLOSED_MSG_UZ": str, "CLOSED_MSG_RU": str,
}
PERMS = ["stats", "orders", "stock", "menu", "banners", "customers", "broadcast", "settings"]


# ═════════════════════════════ Zaxira (Telegram) ═════════════════════════════
class Backup:
    """Bepul hostingda disk vaqtinchalik. Baza + menyu + rasmlar zip qilinib rahbarning admin bot
    chatidagi qadalgan (pinned) xabarga yoziladi; server qayta yonganda shu yerdan tiklanadi."""
    NAME = "gumbaz_backup.zip"

    def __init__(self):
        self.on = CFG["BACKUP"] == "telegram" and bool(CFG["ADMIN_IDS"])
        self.chat = CFG["ADMIN_IDS"][0] if CFG["ADMIN_IDS"] else 0
        self.dirty = False
        self.last = 0.0
        self.msg_id = None
        self.fails = 0
        self.lock = threading.Lock()

    def mark(self):
        self.dirty = True

    def restore(self):
        if not self.on:
            return
        try:
            chat = tg(ADMIN, "getChat", chat_id=self.chat)
            pm = (chat or {}).get("pinned_message") or {}
            doc = pm.get("document") or {}
            if doc.get("file_name") != self.NAME:
                log.info("Zaxira topilmadi — yangi baza bilan boshlanadi")
                return
            self.msg_id = pm["message_id"]
            tmp = os.path.join(tempfile.gettempdir(), "gumbaz_restore.zip")
            if not tg_download(ADMIN, doc["file_id"], tmp):
                log.warning("Zaxirani yuklab bo'lmadi")
                return
            with zipfile.ZipFile(tmp) as z:
                for n in z.namelist():
                    if n.startswith(("data/", "uploads/")) and ".." not in n and not n.endswith("/"):
                        dest = os.path.join(DATA_DIR, n[5:]) if n.startswith("data/") else os.path.join(UPLOAD_DIR, n[8:])
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        with z.open(n) as src, open(dest, "wb") as out:
                            shutil.copyfileobj(src, out)
            log.info("Zaxiradan tiklandi (%s KB)", doc.get("file_size", 0) // 1024)
        except Exception:
            log.error("Tiklashda xato:\n%s", traceback.format_exc())

    def run(self, force=False):
        if not self.on or not (self.dirty or force) or db.c is None:
            return
        with self.lock:
            self.dirty = False
            try:
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    snap = os.path.join(tempfile.gettempdir(), "gumbaz_snap.db")
                    with db.lock:
                        dst = sqlite3.connect(snap)
                        db.c.backup(dst)
                        dst.close()
                    z.write(snap, "data/gumbaz.db")
                    for name in ("menu.json", "banners.json"):
                        p = os.path.join(DATA_DIR, name)
                        if os.path.exists(p):
                            z.write(p, "data/" + name)
                    big = db.kv_get("tg_files", {})
                    for base, _, files in os.walk(UPLOAD_DIR):
                        for fn in files:
                            full = os.path.join(base, fn)
                            rel = os.path.relpath(full, UPLOAD_DIR).replace(os.sep, "/")
                            if "uploads/" + rel not in big and fn != ".keep":
                                z.write(full, "uploads/" + rel)
                data = buf.getvalue()
                caption = f"💾 Gumbaz zaxira · {time.strftime('%d.%m %H:%M')}\nBu xabarni o'chirmang va qadalgan holda qoldiring."
                ok = False
                if self.msg_id:
                    media = {"type": "document", "media": "attach://f", "caption": caption}
                    ok = tg_multipart(ADMIN, "editMessageMedia", {"chat_id": self.chat, "message_id": self.msg_id,
                                                                   "media": json.dumps(media)}, {"f": (self.NAME, data)}) is not None
                if not ok:
                    m = tg_multipart(ADMIN, "sendDocument", {"chat_id": self.chat, "disable_notification": "true",
                                                              "caption": caption}, {"document": (self.NAME, data)})
                    if m:
                        self.msg_id = m["message_id"]
                        tg(ADMIN, "pinChatMessage", chat_id=self.chat, message_id=self.msg_id, disable_notification=True)
                        ok = True
                if ok:
                    self.last = time.time()
                    self.fails = 0
                    log.info("Zaxira saqlandi (%d KB)", len(data) // 1024)
                else:
                    self.dirty = True
                    self.fails += 1
                    self.last = time.time() + min(300, 15 * self.fails)  # orqaga chekinish, 5 daqiqagacha
                    if self.fails in (1, 10) or self.fails % 50 == 0:
                        log.warning("Zaxira yuborilmadi — rahbar admin botga /start yozganmi?")
            except Exception:
                self.dirty = True
                self.fails += 1
                self.last = time.time() + min(300, 15 * self.fails)
                log.error("Zaxira xato:\n%s", traceback.format_exc())

    def loop(self):
        while True:
            time.sleep(5)
            if self.dirty and time.time() - self.last > 15:
                self.run()

    def store_big(self, rel_path, data, name):
        """Katta fayl (video) — alohida hujjat sifatida saqlanadi, zipga kirmaydi."""
        if not self.on:
            return
        m = tg_multipart(ADMIN, "sendDocument", {"chat_id": self.chat, "disable_notification": "true",
                                                  "caption": "🎬 Banner fayli (zaxira) — o'chirmang"}, {"document": (name, data)})
        if m and m.get("document"):
            files = db.kv_get("tg_files", {})
            files[rel_path] = m["document"]["file_id"]
            db.kv_set("tg_files", files)


BACKUP = Backup()


# ═════════════════════════════ Storage ═════════════════════════════
class DB:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.c = None

    def open(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.c = sqlite3.connect(self.path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            tg_id INTEGER PRIMARY KEY, phone TEXT, name TEXT, username TEXT,
            lang TEXT DEFAULT 'uz', photo TEXT, lat REAL, lon REAL, address TEXT,
            created INTEGER, updated INTEGER);
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, items TEXT, total INTEGER,
            delivery TEXT, payment TEXT, address TEXT, lat REAL, lon REAL, comment TEXT,
            status TEXT, admin_msg_id INTEGER, created INTEGER, updated INTEGER);
        CREATE TABLE IF NOT EXISTS order_msgs(order_id INTEGER, chat_id INTEGER, msg_id INTEGER);
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS staff(
            id INTEGER PRIMARY KEY AUTOINCREMENT, login TEXT UNIQUE COLLATE NOCASE, pass TEXT, name TEXT,
            perms TEXT, tg_id INTEGER, active INTEGER DEFAULT 1, created INTEGER, last_login INTEGER);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, staff_id INTEGER, owner INTEGER, expires INTEGER);
        CREATE INDEX IF NOT EXISTS ix_orders_user ON orders(tg_id, id);
        CREATE INDEX IF NOT EXISTS ix_orders_created ON orders(created);
        CREATE INDEX IF NOT EXISTS ix_order_msgs ON order_msgs(order_id);
        """)
        cols = {r[1] for r in self.c.execute("PRAGMA table_info(users)")}
        for col in ("apt", "entrance", "floor", "landmark"):  # aniq manzil maydonlari (v3.1)
            if col not in cols:
                self.c.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
        self.c.commit()

    def q(self, sql, args=(), one=False):
        with self.lock:
            rows = self.c.execute(sql, args).fetchall()
            return (dict(rows[0]) if rows else None) if one else [dict(r) for r in rows]

    def x(self, sql, args=()):
        with self.lock:
            cur = self.c.execute(sql, args)
            self.c.commit()
        BACKUP.mark()
        return cur.lastrowid

    def get_user(self, uid):
        return self.q("SELECT * FROM users WHERE tg_id=?", (uid,), one=True)

    def upsert_user(self, uid, **f):
        now = int(time.time())
        with self.lock:
            if self.get_user(uid):
                if f:
                    sets = ",".join(f"{k}=?" for k in f)
                    self.x(f"UPDATE users SET {sets}, updated=? WHERE tg_id=?", (*f.values(), now, uid))
            else:
                f.setdefault("lang", "uz")
                cols = ",".join(["tg_id", *f.keys(), "created", "updated"])
                self.x(f"INSERT INTO users({cols}) VALUES({','.join('?' * (len(f) + 3))})", (uid, *f.values(), now, now))
        return self.get_user(uid)

    def kv_get(self, k, default=None):
        r = self.q("SELECT v FROM kv WHERE k=?", (k,), one=True)
        return json.loads(r["v"]) if r else default

    def kv_set(self, k, v):
        self.x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))


db = DB(os.path.join(DATA_DIR, "gumbaz.db"))


class JsonStore:
    def __init__(self, name, default):
        self.path = os.path.join(DATA_DIR, name)
        self.default = default
        self.lock = threading.RLock()
        self.data = default

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except FileNotFoundError:
            self.data = json.loads(json.dumps(self.default))

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        BACKUP.mark()


MENU = JsonStore("menu.json", {"categories": [], "products": []})
BANNERS = JsonStore("banners.json", [])


def setting(key):
    s = db.kv_get("settings", {})
    if key in s:
        return s[key]
    return CFG.get(key, True if key == "ACCEPTING" else "")


def all_settings():
    return {k: setting(k) for k in SETTING_KEYS}


def product_map():
    return {p["id"]: p for p in MENU.data["products"]}


def is_available(p):
    return p.get("available", True) is not False


# ═════════════════════════════ Texts ═════════════════════════════
T = {
    "uz": {
        "hello": "Assalomu alaykum! <b>Gumbaz Express</b> ga xush kelibsiz 🍕\n\nBuyurtma berish uchun telefon raqamingizni yuboring 👇",
        "send_phone": "📱 Raqamni yuborish",
        "own_phone": "Iltimos, o'zingizning raqamingizni tugma orqali yuboring 👇",
        "ask_loc": "Rahmat! Endi yetkazib berish manzilini aniqlash uchun joylashuvingizni yuboring 📍",
        "send_loc": "📍 Joylashuvni yuborish",
        "skip_loc": "Keyinroq kiritaman",
        "ready": "Hammasi tayyor! ✅\nPastdagi tugmani bosib menyuni oching 👇",
        "addr": "📍 Taxminiy manzil: {a}\n\nUy raqami, kvartira va mo'ljalni menyudagi «Manzil» bo'limida aniqlashtirasiz.",
        "open": "🍔 Menyuni ochish",
        "menu_btn": "Menyu",
        "status": {"new": "🆕 Yangi", "accepted": "✅ Qabul qilindi", "cooking": "👨‍🍳 Tayyorlanmoqda",
                   "on_way": "🛵 Yo'lda", "ready": "📦 Tayyor — olib ketishingiz mumkin",
                   "done": "🏁 Yetkazildi", "cancelled": "❌ Bekor qilindi"},
        "order_user": "Buyurtmangiz qabul qilindi! ✅\n\n<b>Buyurtma #{id}</b>\n{items}\n\n💰 Jami: <b>{total} so'm</b>\n🚚 {delivery}\n{extra}\nHolati o'zgarganda sizga xabar beramiz.",
        "status_user": "<b>Buyurtma #{id}</b>\nHolati: {status}",
        "deliv": {"courier": "Gumbaz Express kuryeri", "yandex": "Yandex kuryeri", "pickup": "Olib ketish"},
        "courier_note": "Yetkazish narxini kuryer yetkazganda aytadi.",
        "yandex_note": "Yandex kuryeri orqali yetkaziladi. Yetkazish narxini kuryer yetib borganda aytadi.",
        "logout": "Hisobdan chiqdingiz. Qayta kirish uchun /start ni bosing.",
    },
    "ru": {
        "hello": "Здравствуйте! Добро пожаловать в <b>Gumbaz Express</b> 🍕\n\nДля оформления заказа отправьте свой номер телефона 👇",
        "send_phone": "📱 Отправить номер",
        "own_phone": "Пожалуйста, отправьте свой номер кнопкой ниже 👇",
        "ask_loc": "Спасибо! Теперь отправьте геолокацию, чтобы определить адрес доставки 📍",
        "send_loc": "📍 Отправить геолокацию",
        "skip_loc": "Укажу позже",
        "ready": "Всё готово! ✅\nНажмите кнопку ниже, чтобы открыть меню 👇",
        "addr": "📍 Примерный адрес: {a}\n\nДом, квартиру и ориентир уточните в меню в разделе «Адрес».",
        "open": "🍔 Открыть меню",
        "menu_btn": "Меню",
        "status": {"new": "🆕 Новый", "accepted": "✅ Принят", "cooking": "👨‍🍳 Готовится",
                   "on_way": "🛵 В пути", "ready": "📦 Готов — можно забрать",
                   "done": "🏁 Доставлен", "cancelled": "❌ Отменён"},
        "order_user": "Ваш заказ принят! ✅\n\n<b>Заказ #{id}</b>\n{items}\n\n💰 Итого: <b>{total} сум</b>\n🚚 {delivery}\n{extra}\nМы сообщим, когда статус изменится.",
        "status_user": "<b>Заказ #{id}</b>\nСтатус: {status}",
        "deliv": {"courier": "Курьер Gumbaz Express", "yandex": "Курьер Яндекс", "pickup": "Самовывоз"},
        "courier_note": "Стоимость доставки курьер скажет при доставке.",
        "yandex_note": "Доставка курьером Яндекс. Стоимость доставки курьер скажет по прибытии.",
        "logout": "Вы вышли из аккаунта. Нажмите /start, чтобы войти снова.",
    },
}
STATUS_ORDER = ["new", "accepted", "cooking", "on_way", "ready", "done", "cancelled"]


def tr(lang, key):
    return T.get(lang if lang in T else "uz")[key]


def fmt(n):
    return f"{int(n):,}".replace(",", " ")


def esc(s):
    return html.escape(str(s or ""), quote=False)


# ═════════════════════════════ Telegram API ═════════════════════════════
def tg(bot, method, _timeout=35, **params):
    url = f"{CFG['TG_API']}/bot{TOKENS[bot]}/{method}"
    data = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
    for attempt in range(3):
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=_timeout) as r:
                return json.loads(r.read()).get("result")
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read())
            except Exception:
                body = {}
            retry = (body.get("parameters") or {}).get("retry_after")
            if e.code == 429 and retry:
                time.sleep(int(retry) + 1)
                continue
            desc = body.get("description", "")
            if not any(s in desc for s in ("not modified", "chat not found", "bot was blocked", "user is deactivated")):
                log.warning("TG %s %s: %s", method, e.code, desc)
            return None
        except Exception as e:
            if method == "getUpdates":
                raise
            log.warning("TG %s xato: %s", method, e)
            time.sleep(1 + attempt)
    return None


def tg_multipart(bot, method, fields, files):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, (name, data) in files.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\n'
                     f'Content-Type: application/octet-stream\r\n\r\n'.encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"{CFG['TG_API']}/bot{TOKENS[bot]}/{method}", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read()).get("result")
    except urllib.error.HTTPError as e:
        try:
            log.warning("TG %s %s: %s", method, e.code, json.loads(e.read()).get("description"))
        except Exception:
            pass
    except Exception as e:
        log.warning("TG %s xato: %s", method, e)
    return None


def tg_download(bot, file_id, dest):
    info = tg(bot, "getFile", file_id=file_id)
    if not info or "file_path" not in info:
        return False
    with urllib.request.urlopen(f"{CFG['TG_API']}/file/bot{TOKENS[bot]}/{info['file_path']}", timeout=120) as r, \
            open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return True


def app_url(page=""):
    return f"{CFG['WEBAPP_URL']}/{page}?v={int(time.time()) // 3600}"


# ═════════════════════════════ Geocoding ═════════════════════════════
_geo_cache, _geo_lock, _geo_last = {}, threading.Lock(), [0.0]


def reverse_geocode(lat, lon, lang="uz"):
    key = (round(lat, 4), round(lon, 4), lang)
    if key in _geo_cache:
        return _geo_cache[key]
    res = ""
    with _geo_lock:
        wait = 1.05 - (time.time() - _geo_last[0])
        if wait > 0:
            time.sleep(wait)
        _geo_last[0] = time.time()
        url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
            {"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 18, "addressdetails": 1,
             "accept-language": "ru" if lang == "ru" else "uz,ru"})
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"GumbazExpressBot/3.0 (+{CFG['WEBAPP_URL']})"})
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read())
            a = d.get("address", {})
            street = a.get("road") or a.get("pedestrian") or ""
            if street and a.get("house_number"):
                street += ", " + a["house_number"]
            parts = []
            for p in (street, a.get("suburb") or a.get("quarter") or a.get("neighbourhood") or a.get("village") or "",
                      a.get("city") or a.get("town") or a.get("county") or ""):
                if p and p not in parts:
                    parts.append(p)
            res = ", ".join(parts) or d.get("display_name", "")
        except Exception as e:
            log.info("geocode xato: %s", e)
    if res:
        _geo_cache[key] = res
        return res
    return f"{lat:.5f}, {lon:.5f}"


# ═════════════════════════════ Xarita kataklari (zaxira yo'li) ═════════════════════════════
from collections import OrderedDict

_tiles, _tiles_lock = OrderedDict(), threading.Lock()
TILE_SOURCES = (  # CARTO endi kalit talab qiladi — faqat OpenStreetMap manbalari"https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                "https://{s}.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png")


def get_tile(z, x, y):
    """Mijoz brauzeri xarita serveriga to'g'ridan-to'g'ri ulana olmasa, katak shu server orqali olinadi."""
    key = (z, x, y)
    with _tiles_lock:
        if key in _tiles:
            _tiles.move_to_end(key)
            return _tiles[key]
    for tpl in TILE_SOURCES:
        url = tpl.format(s="abc"[(x + y) % 3], z=z, x=x, y=y)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"GumbazExpress/3.1 (+{CFG['WEBAPP_URL']})",
                                                       "Referer": CFG["WEBAPP_URL"] + "/"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = r.read(2 * 1024 * 1024)
            if data[:4] == b"\x89PNG":
                with _tiles_lock:
                    _tiles[key] = data
                    while len(_tiles) > 400:
                        _tiles.popitem(last=False)
                return data
        except Exception as e:
            log.info("tile %s: %s", url, e)
    return None


# ═════════════════════════════ Staff / permissions ═════════════════════════════
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return f"{salt}${hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 120000).hex()}"


def check_pw(pw, stored):
    try:
        salt, _ = stored.split("$", 1)
        return hmac.compare_digest(hash_pw(pw, salt), stored)
    except Exception:
        return False


def is_owner(uid):
    return uid in CFG["ADMIN_IDS"]


def staff_by_tg(uid):
    r = db.q("SELECT * FROM staff WHERE tg_id=? AND active=1", (uid,), one=True)
    if r:
        r["perms"] = json.loads(r["perms"] or "[]")
    return r


def can_handle_orders(uid):
    if is_owner(uid):
        return True
    s = staff_by_tg(uid)
    return bool(s and "orders" in s["perms"])


def order_targets():
    t = set(CFG["ADMIN_IDS"])
    for s in db.q("SELECT tg_id, perms FROM staff WHERE active=1 AND tg_id IS NOT NULL"):
        if "orders" in json.loads(s["perms"] or "[]"):
            t.add(int(s["tg_id"]))
    t = sorted(t)
    if CFG["ADMIN_CHAT_ID"]:
        t.append(CFG["ADMIN_CHAT_ID"])
    return t


# ═════════════════════════════ Orders ═════════════════════════════
NEXT = {"new": ["accepted", "cancelled"], "accepted": ["cooking", "cancelled"],
        "cooking": ["on_way", "cancelled"], "on_way": ["done"], "ready": ["done"]}
BTN = {"accepted": "✅ Qabul qilish", "cooking": "👨‍🍳 Tayyorlanmoqda", "on_way": "🛵 Yo'lga chiqdi",
       "ready": "📦 Tayyor", "done": "🏁 Yetkazildi", "cancelled": "❌ Bekor qilish"}


def allowed_next(order):
    nxt = list(NEXT.get(order["status"], []))
    if order["delivery"] == "pickup":
        nxt = ["ready" if s == "on_way" else s for s in nxt]
    return nxt


def admin_kb(order, private=True):
    row = []
    for s in allowed_next(order):
        label = "🏁 Berildi" if s == "done" and order["delivery"] == "pickup" else BTN[s]
        row.append({"text": label, "callback_data": f"st:{order['id']}:{s}"})
    kb = [row] if row else []
    extra = []
    if order.get("lat") and order["delivery"] != "pickup":
        extra.append({"text": "🗺 Xarita", "url": f"https://maps.google.com/?q={order['lat']},{order['lon']}"})
    if private:  # web_app tugmasi faqat shaxsiy chatda ishlaydi
        extra.append({"text": "📋 Panel", "web_app": {"url": app_url("admin.html") + f"#order-{order['id']}"}})
    if extra:
        kb.append(extra)
    return {"inline_keyboard": kb}


def items_text(items, lang):
    lines = []
    for it in items:
        name = it["name"].get(lang) or it["name"]["uz"]
        if it.get("variant"):
            name += f" ({it['variant'].get(lang) or it['variant']['uz']})"
        lines.append(f"• {esc(name)} × {it['qty']} = {fmt(it['price'] * it['qty'])}")
    return "\n".join(lines)


def admin_text(order, user):
    items = json.loads(order["items"]) if isinstance(order["items"], str) else order["items"]
    pay = {"cash": "💵 Naqd", "card": "💳 Karta"}.get(order["payment"], order["payment"])
    who = esc(user.get("name") or "—")
    if user.get("username"):
        who += f" (@{esc(user['username'])})"
    lines = [f"<b>🧾 Buyurtma #{order['id']}</b>  —  {T['uz']['status'][order['status']]}", "",
             f"👤 {who}", f"📞 <code>{esc(user.get('phone'))}</code>", f"🚚 {T['uz']['deliv'][order['delivery']]}"]
    if order["delivery"] != "pickup":
        lines.append(f"📍 {esc(order['address'])}")
    lines += [pay, "", items_text(items, "uz"), "", f"💰 <b>Jami: {fmt(order['total'])} so'm</b>"]
    if order["comment"]:
        lines.append(f"💬 {esc(order['comment'])}")
    lines.append(f"🕒 {time.strftime('%d.%m %H:%M', time.localtime(order['created']))}")
    return "\n".join(lines)


def create_order(user, body):
    if not setting("ACCEPTING"):
        raise ValueError("closed")
    pm = product_map()
    raw = body.get("items") or []
    if not isinstance(raw, list) or not raw or len(raw) > 60:
        raise ValueError("cart_empty")
    items, total = [], 0
    for it in raw:
        p = pm.get(str(it.get("pid")))
        qty = int(it.get("qty") or 0)
        if not p or not is_available(p) or not (1 <= qty <= 50):
            raise ValueError("item_unavailable")
        variant, price = None, p.get("price")
        if p.get("variants"):
            v = next((v for v in p["variants"] if v["id"] == it.get("vid")), None)
            if not v:
                raise ValueError("item_unavailable")
            variant, price = v["name"], v["price"]
        items.append({"pid": p["id"], "vid": it.get("vid"), "name": p["name"], "variant": variant,
                      "price": int(price), "qty": qty})
        total += int(price) * qty
    if total < int(setting("MIN_ORDER") or 0):
        raise ValueError("min_order")
    delivery = body.get("delivery")
    if delivery not in ("courier", "yandex", "pickup"):
        raise ValueError("bad_delivery")
    payment = body.get("payment") if body.get("payment") in ("cash", "card") else "cash"
    lat = lon = None
    address = ""
    if delivery != "pickup":
        try:
            lat, lon = float(body["lat"]), float(body["lon"])
        except Exception:
            raise ValueError("no_address")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("no_address")
        address = str(body.get("address") or "")[:300] or reverse_geocode(lat, lon, user.get("lang"))
        extra = str(body.get("address_extra") or "")[:200]
        if extra:
            address += " — " + extra
    now = int(time.time())
    oid = db.x("INSERT INTO orders(tg_id,items,total,delivery,payment,address,lat,lon,comment,status,created,updated)"
               " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (user["tg_id"], json.dumps(items, ensure_ascii=False), total, delivery, payment, address,
                lat, lon, str(body.get("comment") or "")[:500], "new", now, now))
    order = db.q("SELECT * FROM orders WHERE id=?", (oid,), one=True)
    threading.Thread(target=notify_new_order, args=(order, user), daemon=True).start()
    return order


def notify_new_order(order, user):
    try:
        text = admin_text(order, user)
        for chat in order_targets():
            msg = tg(ADMIN, "sendMessage", chat_id=chat, text=text, parse_mode="HTML", reply_markup=admin_kb(order, chat > 0))
            if msg:
                db.x("INSERT INTO order_msgs(order_id,chat_id,msg_id) VALUES(?,?,?)", (order["id"], chat, msg["message_id"]))
        lang = user.get("lang") or "uz"
        L = T[lang]
        if order["delivery"] == "pickup":
            extra = "🏪 " + esc(setting("RESTAURANT_ADDRESS_RU" if lang == "ru" else "RESTAURANT_ADDRESS_UZ")) + "\n"
        else:
            extra = "📍 " + esc(order["address"]) + "\nℹ️ " + L["yandex_note" if order["delivery"] == "yandex" else "courier_note"] + "\n"
        tg(CLIENT, "sendMessage", chat_id=user["tg_id"], parse_mode="HTML",
           text=L["order_user"].format(id=order["id"], items=items_text(json.loads(order["items"]), lang),
                                       total=fmt(order["total"]), delivery=L["deliv"][order["delivery"]], extra=extra))
    except Exception:
        log.error("notify xato:\n%s", traceback.format_exc())


def set_status(oid, status):
    order = db.q("SELECT * FROM orders WHERE id=?", (oid,), one=True)
    if not order:
        return None, "Buyurtma topilmadi"
    if status not in allowed_next(order):
        return order, "Bu holatga o'tkazib bo'lmaydi"
    db.x("UPDATE orders SET status=?, updated=? WHERE id=?", (status, int(time.time()), oid))
    order["status"] = status
    user = db.get_user(order["tg_id"]) or {"tg_id": order["tg_id"], "lang": "uz"}

    def _notify():
        text = admin_text(order, user)
        for m in db.q("SELECT chat_id,msg_id FROM order_msgs WHERE order_id=?", (oid,)):
            tg(ADMIN, "editMessageText", chat_id=m["chat_id"], message_id=m["msg_id"], text=text,
               parse_mode="HTML", reply_markup=admin_kb(order, m["chat_id"] > 0))
        lang = user.get("lang") or "uz"
        st = T[lang]["status"][status]
        if status == "done" and order["delivery"] == "pickup":
            st = "🏁 " + ("Выдан" if lang == "ru" else "Berildi")
        tg(CLIENT, "sendMessage", chat_id=order["tg_id"], parse_mode="HTML",
           text=T[lang]["status_user"].format(id=oid, status=st))

    threading.Thread(target=_notify, daemon=True).start()
    return order, None


# ═════════════════════════════ Bot: mijozlar ═════════════════════════════
def user_lang(frm):
    u = db.get_user(frm["id"])
    if u and u.get("lang"):
        return u["lang"]
    return "ru" if (frm.get("language_code") or "").startswith("ru") else "uz"


def client_webapp_kb(lang):
    return {"inline_keyboard": [[{"text": tr(lang, "open"), "web_app": {"url": app_url()}}]]}


def set_client_menu(chat_id, lang):
    tg(CLIENT, "setChatMenuButton", chat_id=chat_id,
       menu_button={"type": "web_app", "text": tr(lang, "menu_btn"), "web_app": {"url": CFG["WEBAPP_URL"] + "/"}})


def ask_phone(chat_id, lang):
    tg(CLIENT, "sendMessage", chat_id=chat_id, text=tr(lang, "hello"), parse_mode="HTML",
       reply_markup={"keyboard": [[{"text": tr(lang, "send_phone"), "request_contact": True}]],
                     "resize_keyboard": True, "one_time_keyboard": True})


def ask_location(chat_id, lang):
    tg(CLIENT, "sendMessage", chat_id=chat_id, text=tr(lang, "ask_loc"),
       reply_markup={"keyboard": [[{"text": tr(lang, "send_loc"), "request_location": True}],
                                  [{"text": tr(lang, "skip_loc")}]], "resize_keyboard": True, "one_time_keyboard": True})


def send_ready(chat_id, lang, address=None):
    text = tr(lang, "ready")
    if address:
        text = tr(lang, "addr").format(a=esc(address)) + "\n\n" + text
    tg(CLIENT, "sendMessage", chat_id=chat_id, text="✅", reply_markup={"remove_keyboard": True})
    tg(CLIENT, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=client_webapp_kb(lang))
    set_client_menu(chat_id, lang)


def client_message(msg):
    if msg["chat"]["type"] != "private":
        return
    chat, frm = msg["chat"]["id"], msg.get("from") or {}
    uid = frm.get("id")
    if not uid:
        return
    text = (msg.get("text") or "").strip()
    lang = user_lang(frm)
    name = " ".join(x for x in [frm.get("first_name"), frm.get("last_name")] if x)

    if text.split("@")[0].split(" ")[0] == "/start":
        u = db.get_user(uid)
        if not u:
            u = db.upsert_user(uid, name=name, username=frm.get("username"), lang=lang)
        elif u.get("username") != frm.get("username"):
            db.upsert_user(uid, username=frm.get("username"))
        if SINGLE_BOT and (is_owner(uid) or staff_by_tg(uid)):
            tg(CLIENT, "sendMessage", chat_id=chat, text="👨‍💼 Admin panel uchun /admin buyrug'ini yuboring.")
        if not u.get("phone"):
            ask_phone(chat, lang)
        elif u.get("lat") is None:
            ask_location(chat, lang)
        else:
            send_ready(chat, lang, u.get("address"))
        return

    if msg.get("contact"):
        c = msg["contact"]
        if c.get("user_id") != uid:
            tg(CLIENT, "sendMessage", chat_id=chat, text=tr(lang, "own_phone"))
            return
        phone = re.sub(r"[^\d+]", "", c.get("phone_number", ""))
        if not phone.startswith("+"):
            phone = "+" + phone
        db.upsert_user(uid, phone=phone, name=(db.get_user(uid) or {}).get("name") or name,
                       username=frm.get("username"), lang=lang)
        ask_location(chat, lang)
        return

    if msg.get("location"):
        u = db.get_user(uid)
        if not u or not u.get("phone"):
            ask_phone(chat, lang)
            return
        lat, lon = msg["location"]["latitude"], msg["location"]["longitude"]
        addr = reverse_geocode(lat, lon, lang)
        db.upsert_user(uid, lat=lat, lon=lon, address=addr)
        send_ready(chat, lang, addr)
        return

    u = db.get_user(uid)
    if text in (T["uz"]["skip_loc"], T["ru"]["skip_loc"]) and u and u.get("phone"):
        send_ready(chat, lang)
    elif not u or not u.get("phone"):
        ask_phone(chat, lang)
    else:
        tg(CLIENT, "sendMessage", chat_id=chat, text=tr(lang, "ready"), reply_markup=client_webapp_kb(lang))


# ═════════════════════════════ Bot: admin ═════════════════════════════
def admin_message(msg):
    """True qaytarsa — xabar admin tomonidan qayta ishlangan."""
    chat, frm = msg["chat"]["id"], msg.get("from") or {}
    uid = frm.get("id")
    text = (msg.get("text") or "").strip()
    cmd = text.split(" ")[0].split("@")[0].lower() if text.startswith("/") else ""
    private = msg["chat"]["type"] == "private"
    if cmd == "/chatid":
        if is_owner(uid):
            tg(ADMIN, "sendMessage", chat_id=chat, text=f"Chat ID: <code>{chat}</code>", parse_mode="HTML")
        return True
    if not private:
        return True
    admin_cmds = ("/admin", "/panel", "/stats", "/myid") if SINGLE_BOT else ("/admin", "/panel", "/stats", "/myid", "/start", "")
    if cmd not in admin_cmds:
        return False
    if cmd == "/myid":
        tg(ADMIN, "sendMessage", chat_id=chat, text=f"Sizning Telegram ID: <code>{uid}</code>", parse_mode="HTML")
        return True
    owner, staff = is_owner(uid), staff_by_tg(uid)
    if cmd == "/stats" and (owner or (staff and "stats" in staff["perms"])):
        r = stats_today()
        tg(ADMIN, "sendMessage", chat_id=chat, parse_mode="HTML",
           text=f"📊 <b>Bugun</b>\nBuyurtmalar: {r['orders']}\nSavdo: {fmt(r['revenue'])} so'm\nO'rtacha chek: {fmt(r['avg'])} so'm")
        return True
    kb = {"inline_keyboard": [[{"text": "📊 Admin panelni ochish", "web_app": {"url": app_url("admin.html")}}]]}
    if not SINGLE_BOT:
        tg(ADMIN, "setChatMenuButton", chat_id=chat,
           menu_button={"type": "web_app", "text": "Panel", "web_app": {"url": CFG["WEBAPP_URL"] + "/admin.html"}})
    if owner or staff:
        new = db.q("SELECT COUNT(*) c FROM orders WHERE status='new'", one=True)["c"]
        who = "Rahbar" if owner else esc(staff["name"] or staff["login"])
        tg(ADMIN, "sendMessage", chat_id=chat, parse_mode="HTML", reply_markup=kb,
           text=f"Salom, <b>{who}</b>! 👋\n\nYangi buyurtmalar shu chatga keladi.\nHozir kutilayotgan: <b>{new}</b> ta.")
        if owner and BACKUP.on and (not BACKUP.msg_id or BACKUP.fails):
            BACKUP.fails = 0
            threading.Thread(target=BACKUP.run, kwargs={"force": True}, daemon=True).start()
    else:
        tg(ADMIN, "sendMessage", chat_id=chat, parse_mode="HTML", reply_markup=kb,
           text="👨‍💼 <b>Gumbaz Express — xodimlar paneli</b>\n\nRahbar bergan login va parol bilan kiring. "
                "Kirganingizdan keyin buyurtma xabarlari shu chatga keladi.")
    return True


def admin_callback(cq):
    data, uid = cq.get("data") or "", cq["from"]["id"]
    if not data.startswith("st:"):
        tg(ADMIN, "answerCallbackQuery", callback_query_id=cq["id"])
        return
    if not can_handle_orders(uid):
        tg(ADMIN, "answerCallbackQuery", callback_query_id=cq["id"], text="⛔ Ruxsat yo'q", show_alert=True)
        return
    _, oid, st = data.split(":")
    order, err = set_status(int(oid), st)
    tg(ADMIN, "answerCallbackQuery", callback_query_id=cq["id"], text=err or T["uz"]["status"][st])


def handle_update(bot, up):
    try:
        if "message" in up:
            m = up["message"]
            if bot == ADMIN or SINGLE_BOT:
                if admin_message(m):
                    return
            if bot == CLIENT:
                client_message(m)
        elif "callback_query" in up and (bot == ADMIN or SINGLE_BOT):
            admin_callback(up["callback_query"])
    except Exception:
        log.error("[%s] handler xato:\n%s", bot, traceback.format_exc())


def bot_setup(bot):
    me = tg(bot, "getMe")
    log.info("%s bot: @%s", "Mijozlar" if bot == CLIENT else "Admin", (me or {}).get("username"))
    if bot == CLIENT:
        tg(bot, "setMyCommands", commands=[{"command": "start", "description": "Boshlash / Начать"}])
        if SINGLE_BOT:
            for oid in CFG["ADMIN_IDS"]:
                tg(bot, "setMyCommands", scope={"type": "chat", "chat_id": oid},
                   commands=[{"command": "start", "description": "Boshlash"}, {"command": "admin", "description": "Admin panel"},
                             {"command": "stats", "description": "Bugungi statistika"}])
    else:
        tg(bot, "setMyCommands", commands=[{"command": "start", "description": "Admin panel"},
                                           {"command": "stats", "description": "Bugungi statistika"},
                                           {"command": "myid", "description": "Telegram ID"}])


def webhook_secret(bot):
    return hashlib.sha256(("wh:" + TOKENS[bot]).encode()).hexdigest()[:40]


def polling(bot):
    key = f"offset_{bot}"
    offset = db.kv_get(key, 0)
    tg(bot, "deleteWebhook")
    while True:
        try:
            ups = tg(bot, "getUpdates", _timeout=40, offset=offset, timeout=30, allowed_updates=["message", "callback_query"])
        except Exception as e:
            log.warning("[%s] getUpdates: %s", bot, e)
            time.sleep(3)
            continue
        for up in ups or []:
            offset = up["update_id"] + 1
            with db.lock:
                db.c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, json.dumps(offset)))
                db.c.commit()
            handle_update(bot, up)
        if ups is None:
            time.sleep(2)


# ═════════════════════════════ Statistics ═════════════════════════════
def day_start(ts=None):
    lt = time.localtime(ts or time.time())
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


def stats_today():
    r = db.q("SELECT COUNT(*) c, COALESCE(SUM(total),0) s FROM orders WHERE created>=? AND status!='cancelled'",
             (day_start(),), one=True)
    return {"orders": r["c"], "revenue": r["s"], "avg": (r["s"] // r["c"]) if r["c"] else 0}


def dashboard(days=7):
    days = max(1, min(int(days), 31))
    start = day_start() - (days - 1) * 86400
    by_day = [{"day": start + i * 86400, "revenue": 0, "orders": 0} for i in range(days)]
    top, deliv = {}, {"courier": 0, "yandex": 0, "pickup": 0}
    for r in db.q("SELECT total,status,created,items,delivery FROM orders WHERE created>=?", (start,)):
        if r["status"] == "cancelled":
            continue
        i = min(days - 1, (day_start(r["created"]) - start) // 86400)
        by_day[i]["revenue"] += r["total"]
        by_day[i]["orders"] += 1
        deliv[r["delivery"]] = deliv.get(r["delivery"], 0) + 1
        for it in json.loads(r["items"]):
            t = top.setdefault(it["pid"], {"pid": it["pid"], "name": it["name"], "qty": 0, "revenue": 0})
            t["qty"] += it["qty"]
            t["revenue"] += it["qty"] * it["price"]
    yest = db.q("SELECT COALESCE(SUM(total),0) s, COUNT(*) c FROM orders WHERE created>=? AND created<? AND status!='cancelled'",
                (day_start() - 86400, day_start()), one=True)
    active = db.q("SELECT COUNT(*) c FROM orders WHERE status IN ('new','accepted','cooking','on_way','ready')", one=True)["c"]
    return {"today": stats_today(), "yesterday": {"revenue": yest["s"], "orders": yest["c"]}, "by_day": by_day,
            "top": sorted(top.values(), key=lambda x: -x["qty"])[:5], "delivery": deliv, "active": active,
            "customers": db.q("SELECT COUNT(*) c FROM users WHERE phone IS NOT NULL", one=True)["c"],
            "new_customers": db.q("SELECT COUNT(*) c FROM users WHERE phone IS NOT NULL AND created>=?", (day_start(),), one=True)["c"]}


# ═════════════════════════════ Web helpers ═════════════════════════════
SECRETS = {b: hmac.new(b"WebAppData", t.encode(), hashlib.sha256).digest() for b, t in TOKENS.items()}


def verify_init_data(init_data, bot, max_age=7 * 86400):
    if not init_data:
        return None
    try:
        data = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    their = data.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    if not hmac.compare_digest(hmac.new(SECRETS[bot], check.encode(), hashlib.sha256).hexdigest(), their):
        return None
    try:
        if time.time() - int(data.get("auth_date", 0)) > max_age:
            return None
        return json.loads(data.get("user", "{}"))
    except Exception:
        return None


_rate, _fails, _seen_updates = {}, {}, {}


def rate_ok(key, seconds):
    now = time.time()
    if now - _rate.get(key, 0) < seconds:
        return False
    _rate[key] = now
    return True


def login_blocked(key):
    lst = [t for t in _fails.get(key, []) if time.time() - t < 600]
    _fails[key] = lst
    return len(lst) >= 6


def public_user(u):
    return {k: u.get(k) for k in ("tg_id", "phone", "name", "lang", "photo", "lat", "lon", "address",
                                  "apt", "entrance", "floor", "landmark")} if u else None


def client_settings():
    s = all_settings()
    return {"restaurant": {"name": s["RESTAURANT_NAME"], "lat": s["RESTAURANT_LAT"], "lon": s["RESTAURANT_LON"],
                           "address": {"uz": s["RESTAURANT_ADDRESS_UZ"], "ru": s["RESTAURANT_ADDRESS_RU"]}},
            "support_phone": s["SUPPORT_PHONE"], "support_username": s["SUPPORT_USERNAME"],
            "work_hours": s["WORK_HOURS"] or "24/7", "min_order": int(s["MIN_ORDER"] or 0),
            "accepting": bool(s["ACCEPTING"]), "closed_msg": {"uz": s["CLOSED_MSG_UZ"], "ru": s["CLOSED_MSG_RU"]}}


def slug(s):
    s = re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")
    return (s or "x")[:24] + "_" + secrets.token_hex(2)


def clean_l10n(v, required=True, limit=80):
    v = v if isinstance(v, dict) else {}
    out = {"uz": str(v.get("uz") or "").strip()[:limit], "ru": str(v.get("ru") or "").strip()[:limit]}
    if required and not out["uz"] and not out["ru"]:
        raise ValueError("name_required")
    out["uz"] = out["uz"] or out["ru"]
    out["ru"] = out["ru"] or out["uz"]
    return out


def clean_product(p, existing=None):
    if p.get("cat") not in {c["id"] for c in MENU.data["categories"]}:
        raise ValueError("bad_category")
    out = dict(existing or {})
    out["cat"] = p["cat"]
    out["name"] = clean_l10n(p.get("name"))
    d = clean_l10n(p.get("desc"), required=False, limit=240)
    if d["uz"] or d["ru"]:
        out["desc"] = d
    else:
        out.pop("desc", None)
    img = str(p.get("img") or "")
    if img and not re.match(r"^(img|uploads)/[\w./-]+$", img):
        raise ValueError("bad_image")
    out["img"] = img
    if p.get("badge") in ("hit", "spicy", "new"):
        out["badge"] = p["badge"]
    else:
        out.pop("badge", None)
    out["available"] = p.get("available", True) is not False
    variants = p.get("variants") or []
    if variants:
        vs = []
        for i, v in enumerate(variants[:6]):
            price = int(v.get("price") or 0)
            if price <= 0:
                raise ValueError("bad_price")
            vs.append({"id": str(v.get("id") or f"v{i + 1}")[:12], "name": clean_l10n(v.get("name"), limit=30), "price": price})
        if len({v["id"] for v in vs}) != len(vs):
            raise ValueError("bad_variant")
        out["variants"] = vs
        out.pop("price", None)
    else:
        price = int(p.get("price") or 0)
        if price <= 0:
            raise ValueError("bad_price")
        out["price"] = price
        out.pop("variants", None)
    return out


def remove_upload(src):
    if src and str(src).startswith("uploads/"):
        full = os.path.realpath(os.path.join(WEBAPP_DIR, src))
        if full.startswith(os.path.realpath(UPLOAD_DIR) + os.sep):
            try:
                os.remove(full)
            except OSError:
                pass
        files = db.kv_get("tg_files", {})
        if src in files:
            files.pop(src)
            db.kv_set("tg_files", files)


def broadcast(text, notify_chat):
    users = db.q("SELECT tg_id FROM users WHERE phone IS NOT NULL")
    ok = 0
    for u in users:
        if tg(CLIENT, "sendMessage", chat_id=u["tg_id"], text=text):
            ok += 1
        time.sleep(0.05)
    if notify_chat:
        tg(ADMIN, "sendMessage", chat_id=notify_chat, text=f"📣 Xabar yuborildi: {ok}/{len(users)} mijoz")


def public_staff(s):
    return {"id": s["id"], "login": s["login"], "name": s["name"],
            "perms": json.loads(s["perms"] or "[]") if isinstance(s["perms"], str) else s["perms"],
            "active": bool(s["active"]), "telegram": bool(s["tg_id"]), "last_login": s["last_login"]}


# admin marshrut → kerakli ruxsat (None = har qanday kirgan xodim; "owner" = faqat rahbar)
ROUTE_PERM = {
    "me": None, "logout": None, "settings": None, "menu": None,
    "dashboard": "stats", "orders": "orders", "order": "orders", "order/status": "orders",
    "product/toggle": ("stock", "menu"), "product/save": "menu", "product/delete": "menu",
    "category/save": "menu", "category/delete": "menu",
    "banner/save": "banners", "banner/delete": "banners",
    "settings/save": "settings", "customers": "customers", "broadcast": "broadcast",
    "staff": "owner", "staff/save": "owner", "staff/delete": "owner",
}
UPLOAD_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "video/mp4": "mp4", "video/quicktime": "mov"}


class Handler(BaseHTTPRequestHandler):
    server_version = "Gumbaz/3.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, f, *args):
        if self.path.startswith("/api/"):
            log.info("%s %s", self.address_string(), f % args)

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self, limit):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            self.close_connection = True
            raise ValueError("too_large")
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/menu":
            prods = [dict(p, available=is_available(p)) for p in MENU.data["products"]]
            return self.send_json({"categories": MENU.data["categories"], "products": prods,
                                   "banners": BANNERS.data, "settings": client_settings()})
        if path in ("/api/health", "/healthz"):
            return self.send_json({"ok": True})
        m = re.match(r"^/tiles/(\d{1,2})/(\d{1,7})/(\d{1,7})\.png$", path)
        if m:
            z, x, y = (int(v) for v in m.groups())
            data = get_tile(z, x, y) if z <= 19 and x < 2 ** z and y < 2 ** z else None
            self.send_response(200 if data else 404)
            self.send_header("Content-Type", "image/png" if data else "text/plain")
            self.send_header("Content-Length", str(len(data or b"")))
            if data:
                self.send_header("Cache-Control", "public, max-age=604800")
            self.end_headers()
            if data:
                self.wfile.write(data)
            return
        return self.serve_static(path)

    def do_HEAD(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/api/health", "/healthz", "/api/menu"):  # monitoring (UptimeRobot) HEAD yuboradi
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self.serve_static(path, head=True)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path.startswith("/tg/"):
                return self.webhook(path[4:])
            if path.startswith("/api/admin/"):
                return self.admin_api(path[len("/api/admin/"):])
            return self.client_api(path[len("/api/"):])
        except ValueError as e:
            return self.send_json({"ok": False, "error": str(e) or "bad_request"}, 400)
        except (KeyError, TypeError):
            return self.send_json({"ok": False, "error": "bad_request"}, 400)
        except Exception:
            log.error("API xato:\n%s", traceback.format_exc())
            return self.send_json({"ok": False, "error": "server_error"}, 500)

    def webhook(self, bot):
        raw = self.read_body(2 * 1024 * 1024)
        if bot not in TOKENS or not hmac.compare_digest(self.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), webhook_secret(bot)):
            return self.send_json({"ok": False}, 403)
        try:
            up = json.loads(raw)
        except ValueError:
            return self.send_json({"ok": False}, 400)
        self.send_json({"ok": True})
        uid = up.get("update_id")
        if uid is not None:
            seen = _seen_updates.setdefault(bot, [])
            if uid in seen:
                return
            seen.append(uid)
            del seen[:-500]
        threading.Thread(target=handle_update, args=(bot, up), daemon=True).start()

    # ─────────── Client API ───────────
    def client_api(self, route):
        body = json.loads(self.read_body(4 * 1024 * 1024) or b"{}")
        tu = verify_init_data(self.headers.get("X-Init-Data", ""), CLIENT)
        if not tu or "id" not in tu:
            return self.send_json({"ok": False, "error": "unauthorized"}, 401)
        uid = int(tu["id"])
        user = db.get_user(uid)
        if route == "me":
            return self.send_json({"ok": True, "user": public_user(user), "registered": bool(user and user.get("phone"))})
        if not user or not user.get("phone"):
            return self.send_json({"ok": False, "error": "not_registered"}, 403)
        if route == "profile":
            f = {}
            if "name" in body:
                nm = str(body["name"]).strip()[:60]
                if not nm:
                    raise ValueError("bad_name")
                f["name"] = nm
            if body.get("lang") in ("uz", "ru"):
                f["lang"] = body["lang"]
            u = db.upsert_user(uid, **f)
            if "lang" in f:
                set_client_menu(uid, f["lang"])
            return self.send_json({"ok": True, "user": public_user(u)})
        if route == "avatar":
            m = re.match(r"^data:image/(jpeg|png|webp);base64,(.+)$", str(body.get("image", "")), re.S)
            if not m:
                raise ValueError("bad_image")
            raw = base64.b64decode(m.group(2))
            if len(raw) > 3 * 1024 * 1024:
                raise ValueError("too_large")
            os.makedirs(os.path.join(UPLOAD_DIR, "avatars"), exist_ok=True)
            fname = f"uploads/avatars/{uid}_{int(time.time())}.{'jpg' if m.group(1) == 'jpeg' else m.group(1)}"
            with open(os.path.join(WEBAPP_DIR, fname), "wb") as f:
                f.write(raw)
            remove_upload(user.get("photo"))
            return self.send_json({"ok": True, "user": public_user(db.upsert_user(uid, photo=fname))})
        if route == "address":
            lat, lon = float(body["lat"]), float(body["lon"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("bad_coords")
            addr = re.sub(r"\s+", " ", str(body.get("address") or "")).strip()[:200] or reverse_geocode(lat, lon, user.get("lang"))
            det = {k: re.sub(r"\s+", " ", str(body.get(k) or "")).strip()[:lim] for k, lim in
                   (("apt", 12), ("entrance", 12), ("floor", 12), ("landmark", 120))}
            return self.send_json({"ok": True, "user": public_user(db.upsert_user(uid, lat=lat, lon=lon, address=addr, **det))})
        if route == "geocode":
            return self.send_json({"ok": True, "address": reverse_geocode(float(body["lat"]), float(body["lon"]),
                                                                          body.get("lang") or user.get("lang"))})
        if route == "order":
            if not rate_ok(("order", uid), 15):
                return self.send_json({"ok": False, "error": "too_fast"}, 429)
            try:
                order = create_order(user, body)
            except ValueError as e:
                _rate.pop(("order", uid), None)
                return self.send_json({"ok": False, "error": str(e)}, 400)
            return self.send_json({"ok": True, "order": {k: order[k] for k in ("id", "total", "status", "delivery")}})
        if route == "orders":
            rows = db.q("SELECT id,items,total,delivery,payment,address,status,created FROM orders WHERE tg_id=? ORDER BY id DESC LIMIT 30", (uid,))
            for r in rows:
                r["items"] = json.loads(r["items"])
            return self.send_json({"ok": True, "orders": rows})
        if route == "logout":
            db.x("DELETE FROM users WHERE tg_id=?", (uid,))
            tg(CLIENT, "setChatMenuButton", chat_id=uid, menu_button={"type": "default"})
            tg(CLIENT, "sendMessage", chat_id=uid, text=tr(user.get("lang") or "uz", "logout"), reply_markup={"remove_keyboard": True})
            return self.send_json({"ok": True})
        return self.send_json({"ok": False, "error": "not_found"}, 404)

    # ─────────── Admin API ───────────
    def admin_identity(self):
        tu = None
        idata = self.headers.get("X-Init-Data", "")
        if idata:
            for b in ((ADMIN,) if SINGLE_BOT else (ADMIN, CLIENT)):
                tu = verify_init_data(idata, b, max_age=86400)
                if tu:
                    break
        tok = self.headers.get("X-Admin-Token", "")
        if tok:
            th = hashlib.sha256(tok.encode()).hexdigest()
            s = db.q("SELECT * FROM sessions WHERE token=? AND expires>?", (th, int(time.time())), one=True)
            if s:
                if s["owner"]:
                    return {"owner": True, "staff": None, "perms": PERMS, "name": "Rahbar", "id": 0}, tu
                st = db.q("SELECT * FROM staff WHERE id=? AND active=1", (s["staff_id"],), one=True)
                if st:
                    return {"owner": False, "staff": st, "perms": json.loads(st["perms"] or "[]"),
                            "name": st["name"] or st["login"], "id": st["id"]}, tu
        if tu and "id" in tu:
            uid = int(tu["id"])
            if is_owner(uid):
                return {"owner": True, "staff": None, "perms": PERMS, "name": tu.get("first_name") or "Rahbar", "id": 0}, tu
            st = staff_by_tg(uid)
            if st:
                return {"owner": False, "staff": st, "perms": st["perms"], "name": st["name"] or st["login"], "id": st["id"]}, tu
        return None, tu

    def new_session(self, staff_id, owner):
        tok = secrets.token_urlsafe(32)
        db.x("DELETE FROM sessions WHERE expires<?", (int(time.time()),))
        db.x("INSERT INTO sessions(token,staff_id,owner,expires) VALUES(?,?,?,?)",
             (hashlib.sha256(tok.encode()).hexdigest(), staff_id, 1 if owner else 0, int(time.time()) + 30 * 86400))
        return tok

    def admin_api(self, route):
        raw_body = self.read_body(25 * 1024 * 1024 if route == "upload" else 2 * 1024 * 1024)
        if route == "login":
            body = json.loads(raw_body or b"{}")
            login, pw = str(body.get("login") or "").strip().lower(), str(body.get("password") or "")
            key = (login, (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip())
            if login_blocked(key):
                return self.send_json({"ok": False, "error": "too_many"}, 429)
            _, tu = self.admin_identity()
            if CFG.get("OWNER_PASSWORD") and login == str(CFG.get("OWNER_LOGIN") or "rahbar").lower() \
                    and hmac.compare_digest(pw.encode(), str(CFG["OWNER_PASSWORD"]).encode()):
                return self.send_json({"ok": True, "token": self.new_session(0, True)})
            st = db.q("SELECT * FROM staff WHERE login=? AND active=1", (login,), one=True)
            if not st or not check_pw(pw, st["pass"]):
                _fails.setdefault(key, []).append(time.time())
                return self.send_json({"ok": False, "error": "bad_login"}, 401)
            if tu and "id" in tu:  # Telegram ichidan kirsa — buyurtma xabarlari uchun bog'laymiz
                db.x("UPDATE staff SET tg_id=NULL WHERE tg_id=? AND id!=?", (int(tu["id"]), st["id"]))
                db.x("UPDATE staff SET tg_id=? WHERE id=?", (int(tu["id"]), st["id"]))
            db.x("UPDATE staff SET last_login=? WHERE id=?", (int(time.time()), st["id"]))
            return self.send_json({"ok": True, "token": self.new_session(st["id"], False)})

        me, tu = self.admin_identity()
        if not me:
            return self.send_json({"ok": False, "error": "login_required", "id": (tu or {}).get("id")}, 401)
        perms = set(me["perms"])
        if route == "upload":
            need = "banners" if self.headers.get("X-Kind") == "banner" else "menu"
        elif route == "sort":
            need = "banners" if json.loads(raw_body or b"{}").get("kind") == "banners" else "menu"
        else:
            need = ROUTE_PERM.get(route, "owner")
        if not me["owner"]:
            if need == "owner":
                return self.send_json({"ok": False, "error": "forbidden"}, 403)
            if need and not any(n in perms for n in (need if isinstance(need, tuple) else (need,))):
                return self.send_json({"ok": False, "error": "forbidden"}, 403)

        if route == "upload":
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            ext = UPLOAD_TYPES.get(ctype)
            if not ext:
                raise ValueError("bad_type")
            if len(raw_body) < 100:
                raise ValueError("empty")
            if len(raw_body) > 19 * 1024 * 1024 and BACKUP.on:
                raise ValueError("too_large_20")
            kind = "banners" if self.headers.get("X-Kind") == "banner" else "products"
            os.makedirs(os.path.join(UPLOAD_DIR, kind), exist_ok=True)
            src = f"uploads/{kind}/{int(time.time())}_{secrets.token_hex(3)}.{ext}"
            with open(os.path.join(WEBAPP_DIR, src), "wb") as f:
                f.write(raw_body)
            if len(raw_body) > 2 * 1024 * 1024:
                threading.Thread(target=BACKUP.store_big, args=(src, raw_body, os.path.basename(src)), daemon=True).start()
            BACKUP.mark()
            return self.send_json({"ok": True, "src": src})

        body = json.loads(raw_body or b"{}")
        if route == "me":
            return self.send_json({"ok": True, "name": me["name"], "owner": me["owner"],
                                   "perms": PERMS if me["owner"] else [p for p in PERMS if p in perms]})
        if route == "logout":
            tok = self.headers.get("X-Admin-Token", "")
            if tok:
                db.x("DELETE FROM sessions WHERE token=?", (hashlib.sha256(tok.encode()).hexdigest(),))
            if me["staff"] and tu and me["staff"].get("tg_id") == tu.get("id"):
                db.x("UPDATE staff SET tg_id=NULL WHERE id=?", (me["staff"]["id"],))
            return self.send_json({"ok": True})
        if route == "dashboard":
            return self.send_json({"ok": True, **dashboard(body.get("days", 7))})

        if route == "orders":
            st = body.get("status") or "active"
            groups = {"active": ("new", "accepted", "cooking", "on_way", "ready"), "done": ("done",), "cancelled": ("cancelled",)}
            sts = groups.get(st, (st,) if st in STATUS_ORDER else groups["active"])
            sql = (f"SELECT o.*, u.name uname, u.phone uphone, u.username uusername FROM orders o "
                   f"LEFT JOIN users u ON u.tg_id=o.tg_id WHERE o.status IN ({','.join('?' * len(sts))})")
            args = list(sts)
            if body.get("q"):
                qq = str(body["q"]).strip().lstrip("#")
                if qq.isdigit():
                    sql += " AND (o.id=? OR u.phone LIKE ?)"
                    args += [int(qq), f"%{qq}%"]
                else:
                    sql += " AND u.name LIKE ?"
                    args.append(f"%{qq}%")
            rows = db.q(sql + " ORDER BY o.id DESC LIMIT 40", args)
            for r in rows:
                r["items"] = json.loads(r["items"])
                r["next"] = allowed_next(r)
                r.pop("admin_msg_id", None)
            counts = {r["status"]: r["c"] for r in db.q("SELECT status, COUNT(*) c FROM orders WHERE status IN ('new','accepted','cooking','on_way','ready') GROUP BY status")}
            return self.send_json({"ok": True, "orders": rows, "active_counts": counts})
        if route == "order":
            r = db.q("SELECT o.*, u.name uname, u.phone uphone, u.username uusername FROM orders o "
                     "LEFT JOIN users u ON u.tg_id=o.tg_id WHERE o.id=?", (int(body["id"]),), one=True)
            if not r:
                return self.send_json({"ok": False, "error": "not_found"}, 404)
            r["items"] = json.loads(r["items"])
            r["next"] = allowed_next(r)
            return self.send_json({"ok": True, "order": r})
        if route == "order/status":
            order, err = set_status(int(body["id"]), str(body["status"]))
            if err:
                return self.send_json({"ok": False, "error": err}, 400)
            return self.send_json({"ok": True, "status": order["status"], "next": allowed_next(order)})

        if route == "menu":
            return self.send_json({"ok": True, "categories": MENU.data["categories"],
                                   "products": [dict(p, available=is_available(p)) for p in MENU.data["products"]],
                                   "banners": BANNERS.data})
        if route == "product/save":
            p = body.get("product") or {}
            with MENU.lock:
                prods = MENU.data["products"]
                cur = next((x for x in prods if x["id"] == p.get("id")), None) if p.get("id") else None
                new = clean_product(p, cur)
                if cur:
                    if cur.get("img") != new.get("img"):
                        remove_upload(cur.get("img"))
                    if cur["cat"] != new["cat"]:
                        prods.remove(cur)
                        idx = max([i for i, x in enumerate(prods) if x["cat"] == new["cat"]], default=len(prods) - 1)
                        prods.insert(idx + 1, cur)
                    pid = cur["id"]
                    cur.clear()
                    cur.update(new)
                    cur["id"] = pid
                    saved = cur
                else:
                    new["id"] = slug(new["name"]["uz"])
                    idx = max([i for i, x in enumerate(prods) if x["cat"] == new["cat"]], default=len(prods) - 1)
                    prods.insert(idx + 1, new)
                    saved = new
                MENU.save()
            return self.send_json({"ok": True, "product": saved})
        if route == "product/toggle":
            with MENU.lock:
                p = product_map().get(body.get("id"))
                if not p:
                    return self.send_json({"ok": False, "error": "not_found"}, 404)
                p["available"] = bool(body.get("available"))
                MENU.save()
            return self.send_json({"ok": True, "available": p["available"]})
        if route == "product/delete":
            with MENU.lock:
                p = product_map().get(body.get("id"))
                if p:
                    MENU.data["products"].remove(p)
                    remove_upload(p.get("img"))
                    MENU.save()
            return self.send_json({"ok": True})
        if route == "sort":
            kind, ids = body.get("kind"), [str(i) for i in body.get("ids") or []]
            if kind == "banners":
                with BANNERS.lock:
                    m = {b["id"]: b for b in BANNERS.data}
                    if set(ids) != set(m):
                        raise ValueError("bad_ids")
                    BANNERS.data[:] = [m[i] for i in ids]
                    BANNERS.save()
                return self.send_json({"ok": True})
            with MENU.lock:
                if kind == "categories":
                    m = {c["id"]: c for c in MENU.data["categories"]}
                    if set(ids) != set(m):
                        raise ValueError("bad_ids")
                    MENU.data["categories"] = [m[i] for i in ids]
                elif kind == "products":
                    prods = MENU.data["products"]
                    m = {p["id"]: p for p in prods}
                    if not ids or any(i not in m for i in ids):
                        raise ValueError("bad_ids")
                    cat = m[ids[0]]["cat"]
                    if set(ids) != {p["id"] for p in prods if p["cat"] == cat}:
                        raise ValueError("bad_ids")
                    slots = [i for i, p in enumerate(prods) if p["cat"] == cat]
                    for slot, pid in zip(slots, ids):
                        prods[slot] = m[pid]
                else:
                    raise ValueError("bad_kind")
                MENU.save()
            return self.send_json({"ok": True})
        if route == "category/save":
            name = clean_l10n(body.get("name"), limit=40)
            with MENU.lock:
                cats = MENU.data["categories"]
                c = next((x for x in cats if x["id"] == body.get("id")), None) if body.get("id") else None
                if c:
                    c["name"] = name
                else:
                    c = {"id": slug(name["uz"]), "name": name}
                    cats.append(c)
                MENU.save()
            return self.send_json({"ok": True, "category": c})
        if route == "category/delete":
            with MENU.lock:
                if any(p["cat"] == body.get("id") for p in MENU.data["products"]):
                    raise ValueError("category_not_empty")
                MENU.data["categories"] = [c for c in MENU.data["categories"] if c["id"] != body.get("id")]
                MENU.save()
            return self.send_json({"ok": True})
        if route == "banner/save":
            b = body.get("banner") or {}
            src = str(b.get("src") or "")
            if not re.match(r"^(banners|uploads)/[\w./-]+$", src):
                raise ValueError("bad_src")
            with BANNERS.lock:
                cur = next((x for x in BANNERS.data if x["id"] == b.get("id")), None) if b.get("id") else None
                if cur and cur.get("src") != src:
                    remove_upload(cur.get("src"))
                    cur.pop("poster", None)
                item = cur if cur else {"id": "b" + secrets.token_hex(4)}
                item.update({"type": "video" if src.lower().endswith((".mp4", ".mov")) else "image", "src": src})
                item.pop("product", None)
                item.pop("category", None)
                if b.get("product") in product_map():
                    item["product"] = b["product"]
                elif b.get("category") in {c["id"] for c in MENU.data["categories"]}:
                    item["category"] = b["category"]
                if not cur:
                    BANNERS.data.insert(0, item)
                BANNERS.save()
            return self.send_json({"ok": True, "banner": item})
        if route == "banner/delete":
            with BANNERS.lock:
                b = next((x for x in BANNERS.data if x["id"] == body.get("id")), None)
                if b:
                    BANNERS.data.remove(b)
                    remove_upload(b.get("src"))
                    BANNERS.save()
            return self.send_json({"ok": True})

        if route == "settings":
            return self.send_json({"ok": True, "settings": all_settings()})
        if route == "settings/save":
            s = db.kv_get("settings", {})
            for k, typ in SETTING_KEYS.items():
                if k in (body.get("settings") or {}):
                    v = body["settings"][k]
                    if typ is bool:
                        s[k] = bool(v)
                    elif v in (None, ""):
                        s[k] = "" if typ is str else CFG.get(k, 0)
                    else:
                        s[k] = typ(v)
            if not (-90 <= float(s.get("RESTAURANT_LAT", setting("RESTAURANT_LAT"))) <= 90):
                raise ValueError("bad_coords")
            db.kv_set("settings", s)
            return self.send_json({"ok": True, "settings": all_settings()})

        if route == "customers":
            rows = db.q("SELECT u.tg_id,u.name,u.phone,u.username,u.address,u.created,"
                        " (SELECT COUNT(*) FROM orders o WHERE o.tg_id=u.tg_id AND o.status!='cancelled') orders,"
                        " (SELECT COALESCE(SUM(total),0) FROM orders o WHERE o.tg_id=u.tg_id AND o.status!='cancelled') spent"
                        " FROM users u WHERE u.phone IS NOT NULL ORDER BY spent DESC, u.created DESC LIMIT 100")
            return self.send_json({"ok": True, "customers": rows,
                                   "total": db.q("SELECT COUNT(*) c FROM users WHERE phone IS NOT NULL", one=True)["c"]})
        if route == "broadcast":
            text = str(body.get("text") or "").strip()[:3500]
            if not text:
                raise ValueError("empty")
            if not rate_ok(("bc", me["id"], me["owner"]), 60):
                return self.send_json({"ok": False, "error": "too_fast"}, 429)
            n = db.q("SELECT COUNT(*) c FROM users WHERE phone IS NOT NULL", one=True)["c"]
            threading.Thread(target=broadcast, args=(text, (tu or {}).get("id")), daemon=True).start()
            return self.send_json({"ok": True, "queued": n})

        if route == "staff":
            return self.send_json({"ok": True, "staff": [public_staff(s) for s in db.q("SELECT * FROM staff ORDER BY id")],
                                   "perms": PERMS, "owner_ids": CFG["ADMIN_IDS"]})
        if route == "staff/save":
            s = body.get("staff") or {}
            login = re.sub(r"\s+", "", str(s.get("login") or "")).lower()[:32]
            name = str(s.get("name") or "").strip()[:40]
            perms_new = [p for p in PERMS if p in (s.get("perms") or [])]
            pw = str(s.get("password") or "")
            if not re.match(r"^[a-z0-9_.-]{3,32}$", login):
                raise ValueError("bad_login_format")
            if login == str(CFG.get("OWNER_LOGIN") or "rahbar").lower():
                raise ValueError("login_taken")
            if pw and len(pw) < 6:
                raise ValueError("short_password")
            other = db.q("SELECT id FROM staff WHERE login=?", (login,), one=True)
            active = 1 if s.get("active", True) else 0
            if s.get("id"):
                sid = int(s["id"])
                if other and other["id"] != sid:
                    raise ValueError("login_taken")
                db.x("UPDATE staff SET login=?, name=?, perms=?, active=? WHERE id=?", (login, name, json.dumps(perms_new), active, sid))
                if pw:
                    db.x("UPDATE staff SET pass=? WHERE id=?", (hash_pw(pw), sid))
                if pw or not active:
                    db.x("DELETE FROM sessions WHERE staff_id=? AND owner=0", (sid,))
            else:
                if other:
                    raise ValueError("login_taken")
                if not pw:
                    raise ValueError("short_password")
                sid = db.x("INSERT INTO staff(login,pass,name,perms,active,created) VALUES(?,?,?,?,1,?)",
                           (login, hash_pw(pw), name, json.dumps(perms_new), int(time.time())))
            return self.send_json({"ok": True, "staff": public_staff(db.q("SELECT * FROM staff WHERE id=?", (sid,), one=True))})
        if route == "staff/delete":
            sid = int(body["id"])
            db.x("DELETE FROM sessions WHERE staff_id=? AND owner=0", (sid,))
            db.x("DELETE FROM staff WHERE id=?", (sid,))
            return self.send_json({"ok": True})
        return self.send_json({"ok": False, "error": "not_found"}, 404)

    # ─────────── Static (Range — iPhone'da video uchun shart) ───────────
    def serve_static(self, path, head=False):
        if path in ("", "/"):
            path = "/index.html"
        rel = urllib.parse.unquote(path).lstrip("/")
        root = os.path.realpath(WEBAPP_DIR) + os.sep
        full = os.path.realpath(os.path.join(WEBAPP_DIR, rel))
        if full.startswith(root) and rel.startswith("uploads/") and not os.path.isfile(full) and db.c is not None:
            fid = db.kv_get("tg_files", {}).get(rel)  # katta fayl Telegram zaxirasida
            if fid:
                os.makedirs(os.path.dirname(full), exist_ok=True)
                try:
                    tg_download(ADMIN, fid, full)
                except Exception as e:
                    log.warning("fayl tiklanmadi %s: %s", rel, e)
        if not full.startswith(root) or not os.path.isfile(full):
            body = b"Not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)
            return
        size = os.path.getsize(full)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-cache" if full.endswith(".html") else "public, max-age=86400")
        self.end_headers()
        if head:
            return
        with open(full, "rb") as f:
            f.seek(start)
            left = length
            try:
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass


def keep_alive():
    """Render bepul tarifi 15 daqiqa so'rovsiz qolsa serverni uxlatadi. KEEP_ALIVE=1 bo'lsa
    har 10 daqiqada o'z manziliga so'rov yuboriladi va server uxlamaydi."""
    url = CFG["WEBAPP_URL"] + "/healthz"
    while True:
        time.sleep(600)
        try:
            urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "gumbaz-keepalive"}), timeout=20).read()
        except Exception as e:
            log.info("keep-alive: %s", e)


def shutdown(signum, frame):
    log.info("To'xtatilmoqda — zaxira saqlanmoqda…")
    try:
        BACKUP.run()
    finally:
        os._exit(0)


def main():
    for t, e in (("video/mp4", ".mp4"), ("video/quicktime", ".mov"), ("image/webp", ".webp"), ("application/javascript", ".js")):
        mimetypes.add_type(t, e)
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    BACKUP.restore()  # bazani ochishdan oldin
    seed = os.path.join(ROOT, "data")
    for name in ("menu.json", "banners.json"):
        if not os.path.exists(os.path.join(DATA_DIR, name)) and os.path.exists(os.path.join(seed, name)):
            shutil.copy(os.path.join(seed, name), os.path.join(DATA_DIR, name))
    db.open()
    MENU.load()
    BANNERS.load()
    BACKUP.dirty = False

    srv = ThreadingHTTPServer((CFG.get("HOST", "0.0.0.0"), int(CFG.get("PORT", 8080))), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Web server :%s → %s  (%s, %s, zaxira: %s)", CFG.get("PORT", 8080), CFG["WEBAPP_URL"],
             "webhook" if CFG["WEBHOOK"] else "polling", "bitta bot" if SINGLE_BOT else "ikki bot",
             "telegram" if BACKUP.on else "yo'q")
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=BACKUP.loop, daemon=True).start()
    if str(os.environ.get("KEEP_ALIVE", "")).lower() in ("1", "true", "yes", "on"):
        threading.Thread(target=keep_alive, daemon=True).start()
        log.info("Keep-alive yoqilgan: server uxlamaydi")

    bots = [CLIENT] if SINGLE_BOT else [CLIENT, ADMIN]
    for b in bots:
        bot_setup(b)
    if CFG["WEBHOOK"]:
        for b in bots:
            ok = tg(b, "setWebhook", url=f"{CFG['WEBAPP_URL']}/tg/{b}", secret_token=webhook_secret(b),
                    allowed_updates=["message", "callback_query"], max_connections=20)
            log.info("Webhook [%s]: %s", b, "o'rnatildi" if ok else "XATO")
        while True:
            time.sleep(3600)
    else:
        for b in bots[1:]:
            threading.Thread(target=polling, args=(b,), daemon=True).start()
        polling(bots[0])


if __name__ == "__main__":
    main()
