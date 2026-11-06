#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ulgurji savdo — distribyutor uchun Telegram botlar + Mini App server.

  • Do'konlar boti: /start → telefon → do'kon nomi → joylashuv → Katalog (Mini App) → buyurtma.
    Do'konchi yetkazish kunini (hudud marshruti bo'yicha) va to'lovni (naqd / nasiya) tanlaydi.
  • Admin bot: rahbar (distribyutor) va xodimlar. Yangi buyurtmalar, holat tugmalari, Admin panel:
    katalog (dona + blok/korobka), do'konlar, qarz daftari (nasiya, to'lovlar), hududlar va
    marshrut kunlari, yuklash ro'yxati (mashinaga nima ortish kerak).
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
log = logging.getLogger("ulgurji")


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
    path = os.environ.get("APP_CONFIG") or os.environ.get("GUMBAZ_CONFIG") or os.path.join(ROOT, "config.json")
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
# Mapbox "public" tokeni (pk.…) — mini app xaritasi uchun. Bo'lmasa OpenStreetMap ishlatiladi.
MAPBOX_TOKEN = (os.environ.get("MAPBOX_TOKEN") or str(CFG.get("MAPBOX_TOKEN") or "")).strip()
if MAPBOX_TOKEN and not MAPBOX_TOKEN.startswith("pk."):
    print("MAPBOX_TOKEN 'pk.' bilan boshlanishi kerak (public token) — e'tiborsiz qoldirildi", file=sys.stderr)
    MAPBOX_TOKEN = ""
CLIENT, ADMIN = "client", "admin"
TOKENS = {CLIENT: CFG["CLIENT_BOT_TOKEN"], ADMIN: CFG["ADMIN_BOT_TOKEN"]}
SINGLE_BOT = TOKENS[CLIENT] == TOKENS[ADMIN]

SETTING_KEYS = {
    "COMPANY_NAME": str, "BASE_ADDRESS_UZ": str, "BASE_ADDRESS_RU": str, "BASE_LAT": float, "BASE_LON": float,
    "SUPPORT_PHONE": str, "SUPPORT_USERNAME": str, "WORK_HOURS": str, "MIN_ORDER": int, "ACCEPTING": bool,
    "CLOSED_MSG_UZ": str, "CLOSED_MSG_RU": str,
    "ALLOW_DEBT": bool,          # nasiyaga buyurtma berish mumkinmi
    "DEBT_LIMIT": int,           # har bir do'kon uchun standart qarz chegarasi (0 = cheksiz)
    "SAME_DAY_UNTIL": int,       # shu soatgacha berilgan buyurtma bugunga yetkazilishi mumkin (0 = faqat ertadan)
}
BOOL_DEFAULTS = {"ACCEPTING": True, "ALLOW_DEBT": True}
PERMS = ["stats", "orders", "stock", "menu", "banners", "shops", "debts", "broadcast", "settings"]


# ═════════════════════════════ Zaxira (Telegram) ═════════════════════════════
class Backup:
    """Bepul hostingda disk vaqtinchalik. Baza + menyu + rasmlar zip qilinib rahbarning admin bot
    chatidagi qadalgan (pinned) xabarga yoziladi; server qayta yonganda shu yerdan tiklanadi."""
    NAME = "ulgurji_backup.zip"

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
            tmp = os.path.join(tempfile.gettempdir(), "ulgurji_restore.zip")
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
                    snap = os.path.join(tempfile.gettempdir(), "ulgurji_snap.db")
                    with db.lock:
                        dst = sqlite3.connect(snap)
                        db.c.backup(dst)
                        dst.close()
                    z.write(snap, "data/ulgurji.db")
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
                caption = f"💾 Zaxira · {time.strftime('%d.%m %H:%M')}\nBu xabarni o'chirmang va qadalgan holda qoldiring."
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
        CREATE TABLE IF NOT EXISTS ledger(
            id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, amount INTEGER, kind TEXT, order_id INTEGER,
            note TEXT, by_name TEXT, created INTEGER);
        CREATE INDEX IF NOT EXISTS ix_ledger_user ON ledger(tg_id, id);
        CREATE INDEX IF NOT EXISTS ix_orders_user ON orders(tg_id, id);
        CREATE INDEX IF NOT EXISTS ix_orders_created ON orders(created);
        CREATE INDEX IF NOT EXISTS ix_order_msgs ON order_msgs(order_id);
        """)
        for table, extra in (("users", (("landmark", "TEXT"), ("shop", "TEXT"), ("zone", "TEXT"), ("days", "TEXT"), ("debt_limit", "INTEGER"),
                                        ("note", "TEXT"), ("blocked", "INTEGER DEFAULT 0"))),
                             ("orders", (("delivery_date", "TEXT"), ("zone", "TEXT"), ("paid", "INTEGER"), ("debt", "INTEGER")))):
            cols = {r[1] for r in self.c.execute(f"PRAGMA table_info({table})")}
            for col, typ in extra:
                if col not in cols:
                    self.c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.c.execute("CREATE INDEX IF NOT EXISTS ix_orders_date ON orders(delivery_date)")
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

    # ── qarz daftari: amount > 0 — qarz oshdi (nasiyaga mahsulot), amount < 0 — to'lov ──
    def balance(self, uid):
        return int(self.q("SELECT COALESCE(SUM(amount),0) s FROM ledger WHERE tg_id=?", (uid,), one=True)["s"])

    def ledger_add(self, uid, amount, kind, order_id=None, note="", by_name=""):
        self.x("INSERT INTO ledger(tg_id,amount,kind,order_id,note,by_name,created) VALUES(?,?,?,?,?,?,?)",
               (uid, int(amount), kind, order_id, str(note or "")[:200], str(by_name or "")[:60], int(time.time())))
        return self.balance(uid)


db = DB(os.path.join(DATA_DIR, "ulgurji.db"))


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
    return CFG.get(key, BOOL_DEFAULTS.get(key, ""))


def all_settings():
    return {k: setting(k) for k in SETTING_KEYS}


def product_map():
    return {p["id"]: p for p in MENU.data["products"]}


def is_available(p):
    return p.get("available", True) is not False


# ═════════════════════════════ Texts ═════════════════════════════
T = {
    "uz": {
        "hello": "Assalomu alaykum! <b>{company}</b> ulgurji buyurtma botiga xush kelibsiz 📦\n\n"
                 "Endi har safar agentni kutmaysiz — kerakli mahsulotni o'zingiz buyurtma qilasiz.\n\n"
                 "Ro'yxatdan o'tish uchun telefon raqamingizni yuboring 👇",
        "send_phone": "📱 Raqamni yuborish",
        "own_phone": "Iltimos, o'zingizning raqamingizni tugma orqali yuboring 👇",
        "ask_shop": "Rahmat! Endi <b>do'koningiz nomini</b> yozing ✍️\n\nMasalan: <i>Baraka market</i>",
        "bad_shop": "Do'kon nomini matn bilan yozing (2–60 belgi) ✍️",
        "ask_loc": "Endi do'koningiz <b>joylashuvini</b> yuboring 📍\nMahsulot shu manzilga yetkaziladi.",
        "send_loc": "📍 Joylashuvni yuborish",
        "skip_loc": "Keyinroq kiritaman",
        "ready": "Hammasi tayyor! ✅\nPastdagi tugma orqali katalogni oching va buyurtma bering 👇",
        "addr": "📍 Manzil: {a}",
        "shop_line": "🏪 Do'kon: <b>{s}</b>",
        "open": "📦 Katalogni ochish",
        "menu_btn": "Katalog",
        "status": {"new": "🆕 Yangi", "accepted": "✅ Qabul qilindi", "on_way": "🚚 Yo'lda",
                   "done": "🏁 Yetkazildi", "cancelled": "❌ Bekor qilindi"},
        "order_user": "Buyurtmangiz qabul qilindi! ✅\n\n<b>Buyurtma #{id}</b>\n{items}\n\n💰 Jami: <b>{total} so'm</b>\n"
                      "📅 Yetkazish: <b>{date}</b>\n💳 To'lov: {pay}\n\nHolati o'zgarganda xabar beramiz.",
        "status_user": "<b>Buyurtma #{id}</b>\nHolati: {status}",
        "done_user": "🏁 <b>Buyurtma #{id} yetkazildi</b>\n\n💰 Jami: {total} so'm\n💵 To'langan: {paid} so'm{debt_line}\n\n{bal_line}",
        "debt_line": "\n📒 Nasiyaga: {debt} so'm",
        "bal_debt": "📒 Umumiy qarzingiz: <b>{bal} so'm</b>",
        "bal_zero": "✅ Qarzingiz yo'q. Rahmat!",
        "bal_over": "✅ Oldindan to'lov (haqdorlik): <b>{bal} so'm</b>",
        "pay_user": "💵 <b>To'lov qabul qilindi: {amount} so'm</b>\n{note}\n{bal_line}",
        "adj_user": "📒 Hisobingizga tuzatish kiritildi: {amount} so'm\n{note}\n{bal_line}",
        "pay": {"cash": "💵 Naqd", "debt": "📒 Nasiya"},
        "logout": "Hisobdan chiqdingiz. Qayta kirish uchun /start ni bosing.",
        "blocked": "Hisobingiz vaqtincha to'xtatilgan. Ma'lumot uchun: {phone}",
        "wd": ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"],
        "mon": ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr"],
        "date_fmt": "{wd}, {d}-{m}",
        "pcs": "dona",
        "days_line": "🚚 Yetkazish kunlari: <b>{d}</b>",
        "days_any": "istalgan ish kuni (Du–Sh)",
    },
    "ru": {
        "hello": "Здравствуйте! Добро пожаловать в бот оптовых заказов <b>{company}</b> 📦\n\n"
                 "Больше не нужно ждать агента — заказывайте нужный товар сами.\n\n"
                 "Для регистрации отправьте свой номер телефона 👇",
        "send_phone": "📱 Отправить номер",
        "own_phone": "Пожалуйста, отправьте свой номер кнопкой ниже 👇",
        "ask_shop": "Спасибо! Теперь напишите <b>название магазина</b> ✍️\n\nНапример: <i>Барака маркет</i>",
        "bad_shop": "Напишите название магазина текстом (2–60 символов) ✍️",
        "ask_loc": "Теперь отправьте <b>геолокацию магазина</b> 📍\nТовар будет доставлен по этому адресу.",
        "send_loc": "📍 Отправить геолокацию",
        "skip_loc": "Укажу позже",
        "ready": "Всё готово! ✅\nОткройте каталог кнопкой ниже и сделайте заказ 👇",
        "addr": "📍 Адрес: {a}",
        "shop_line": "🏪 Магазин: <b>{s}</b>",
        "open": "📦 Открыть каталог",
        "menu_btn": "Каталог",
        "status": {"new": "🆕 Новый", "accepted": "✅ Принят", "on_way": "🚚 В пути",
                   "done": "🏁 Доставлен", "cancelled": "❌ Отменён"},
        "order_user": "Ваш заказ принят! ✅\n\n<b>Заказ #{id}</b>\n{items}\n\n💰 Итого: <b>{total} сум</b>\n"
                      "📅 Доставка: <b>{date}</b>\n💳 Оплата: {pay}\n\nМы сообщим, когда статус изменится.",
        "status_user": "<b>Заказ #{id}</b>\nСтатус: {status}",
        "done_user": "🏁 <b>Заказ #{id} доставлен</b>\n\n💰 Итого: {total} сум\n💵 Оплачено: {paid} сум{debt_line}\n\n{bal_line}",
        "debt_line": "\n📒 В долг: {debt} сум",
        "bal_debt": "📒 Ваш общий долг: <b>{bal} сум</b>",
        "bal_zero": "✅ Долгов нет. Спасибо!",
        "bal_over": "✅ Предоплата: <b>{bal} сум</b>",
        "pay_user": "💵 <b>Оплата получена: {amount} сум</b>\n{note}\n{bal_line}",
        "adj_user": "📒 В ваш счёт внесена корректировка: {amount} сум\n{note}\n{bal_line}",
        "pay": {"cash": "💵 Наличные", "debt": "📒 В долг"},
        "logout": "Вы вышли из аккаунта. Нажмите /start, чтобы войти снова.",
        "blocked": "Ваш аккаунт временно приостановлен. Справки: {phone}",
        "wd": ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"],
        "mon": ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"],
        "date_fmt": "{wd}, {d} {m}",
        "pcs": "шт",
        "days_line": "🚚 Дни доставки: <b>{d}</b>",
        "days_any": "любой рабочий день (Пн–Сб)",
    },
}
STATUS_ORDER = ["new", "accepted", "on_way", "done", "cancelled"]
ACTIVE = ("new", "accepted", "on_way")


def tr(lang, key):
    return T.get(lang if lang in T else "uz")[key]


def fmt(n):
    return f"{int(n):,}".replace(",", " ")


def esc(s):
    return html.escape(str(s or ""), quote=False)


def company():
    return setting("COMPANY_NAME") or "Ulgurji savdo"


# ── birliklar va qadoq (dona / blok / korobka) ──
UNITS = {"pc": {"uz": "dona", "ru": "шт"}, "kg": {"uz": "kg", "ru": "кг"}, "l": {"uz": "litr", "ru": "л"},
         "pack": {"uz": "pachka", "ru": "пачка"}}


def unit_name(p, lang="uz"):
    return UNITS.get(p.get("unit") or "pc", UNITS["pc"])[lang if lang in ("uz", "ru") else "uz"]


def sell_units(p):
    """Mahsulotni qaysi birlikda sotish mumkin: ['pack', 'pc'] dan bir yoki ikkalasi."""
    if not p.get("pack"):
        return ["pc"]
    mode = p.get("sell") or "both"
    return ["pack"] if mode == "pack" else ["pc"] if mode == "pc" else ["pack", "pc"]


def unit_price(p, unit):
    if unit == "pack" and p.get("pack"):
        return int(p["pack"].get("price") or int(p["price"]) * int(p["pack"]["qty"]))
    return int(p["price"])


def qty_text(item, lang="uz"):
    """'3 korobka (72 dona)' yoki '5 dona'"""
    nm = item.get("unit_name") or {}
    if item.get("unit") == "pack":
        return f"{item['qty']} {(nm.get(lang) or nm.get('uz', '')).lower()} ({item['qty'] * item['pack_qty']} {item['base'].get(lang) or item['base']['uz']})"
    return f"{item['qty']} {(item.get('base') or UNITS['pc']).get(lang) or 'dona'}"


# ── marshrut: hafta kunlari (Du–Sh) va har bir kunga boriladigan ko'chalar ──
WORKDAYS = list(range(6))   # 0=Dushanba … 5=Shanba (yakshanba — dam olish)


def routes():
    """{"0": ["Bog'ishamol", ...], ...} — har bir hafta kuniga ko'cha/mahalla kalit so'zlari."""
    r = db.kv_get("routes")
    return {str(d): list((r or {}).get(str(d)) or []) for d in WORKDAYS}


def norm_place(s):
    s = to_latin(str(s or "")).lower()
    s = re.sub(r"[ʻʼ‘’`´'ʹ]", "", s).replace("x", "h").replace("ё", "yo")
    s = re.sub(r"\b(ko\s?chasi|kochasi|ko\s?cha|tor|shoh|mahallasi|mahalla|mfy|massivi|ulitsa|street|st)\b", " ", s)
    return re.sub(r"[^a-z0-9а-я ]+", " ", re.sub(r"\s+", " ", s)).strip()


def matched_days(user, rts=None):
    txt = " " + norm_place(f"{user.get('address') or ''} {user.get('landmark') or ''}") + " "
    out = []
    for d, kws in (rts or routes()).items():
        for k in kws:
            nk = norm_place(k)
            if nk and nk in txt:
                out.append(int(d))
                break
    return sorted(out)


def manual_days(user):
    try:
        v = json.loads(user.get("days") or "null")
    except ValueError:
        v = None
    return sorted({int(d) for d in v if int(d) in WORKDAYS}) if isinstance(v, list) and v else None


def shop_days(user, rts=None):
    """Do'konga biriktirilgan kunlar: qo'lda belgilangan yoki manzil bo'yicha topilgan ([] — topilmadi)."""
    return manual_days(user) or matched_days(user, rts)


def _today():
    import datetime
    lt = time.localtime()
    return datetime.date(lt.tm_year, lt.tm_mon, lt.tm_mday), lt.tm_hour


def delivery_dates(days, n=4):
    """Do'kon kunlaridan eng yaqin n ta sana (ISO). Kun topilmasa — istalgan ish kuni (Du–Sh)."""
    import datetime
    today, hour = _today()
    until = int(setting("SAME_DAY_UNTIL") or 0)
    start = today if until and hour < until else today + datetime.timedelta(days=1)
    days = set(days or WORKDAYS)
    out = []
    for i in range(28):
        d = start + datetime.timedelta(days=i)
        if d.weekday() in days:
            out.append(d.isoformat())
            if len(out) >= n:
                break
    return out


def days_text(days, lang="uz"):
    L_ = T[lang if lang in T else "uz"]
    return ", ".join(L_["wd"][d] for d in days) if days else L_["days_any"]


def date_label(iso, lang="uz"):
    import datetime
    try:
        d = datetime.date.fromisoformat(str(iso))
    except ValueError:
        return str(iso or "—")
    L_ = T[lang if lang in T else "uz"]
    return L_["date_fmt"].format(wd=L_["wd"][d.weekday()], d=d.day, m=L_["mon"][d.month - 1])


def bal_line(bal, lang):
    L_ = T[lang if lang in T else "uz"]
    if bal > 0:
        return L_["bal_debt"].format(bal=fmt(bal))
    if bal < 0:
        return L_["bal_over"].format(bal=fmt(-bal))
    return L_["bal_zero"]


def debt_limit(user):
    lim = user.get("debt_limit")
    return int(lim) if lim not in (None, "") else int(setting("DEBT_LIMIT") or 0)


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


_CYR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюяўқғҳ",
                ["a", "b", "v", "g", "d", "e", "yo", "j", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u", "f",
                 "x", "ts", "ch", "sh", "sh", "'", "i", "", "e", "yu", "ya", "o'", "q", "g'", "h"]))
_STREET_RU = [(r"^(?:улица|ул\.)\s+(.+)$", r"\1 ko'chasi"), (r"^(.+?)\s+(?:улица|ул\.)$", r"\1 ko'chasi"),
              (r"^проспект\s+(.+)$", r"\1 shoh ko'chasi"), (r"^(.+?)\s+проспект$", r"\1 shoh ko'chasi"),
              (r"^(?:переулок|пер\.)\s+(.+)$", r"\1 tor ko'chasi"), (r"^(?:махалля|махалла)\s+(.+)$", r"\1 mahallasi"),
              (r"^(.+?)\s+(?:махалля|махалла)$", r"\1 mahallasi"), (r"^(.+?)\s+область$", r"\1 viloyati")]


# Rasmiy o'zbekcha nomlar (OSM ko'pincha ruscha beradi)
_UZ_NAMES = {
    "кашкадарьинская область": "Qashqadaryo viloyati", "самаркандская область": "Samarqand viloyati",
    "бухарская область": "Buxoro viloyati", "сурхандарьинская область": "Surxondaryo viloyati",
    "ташкентская область": "Toshkent viloyati", "навоийская область": "Navoiy viloyati",
    "джизакская область": "Jizzax viloyati", "сырдарьинская область": "Sirdaryo viloyati",
    "ферганская область": "Farg'ona viloyati", "андижанская область": "Andijon viloyati",
    "наманганская область": "Namangan viloyati", "хорезмская область": "Xorazm viloyati",
    "республика каракалпакстан": "Qoraqalpog'iston Respublikasi", "ташкент": "Toshkent",
    "шахрисабз": "Shahrisabz", "шахрисабзский район": "Shahrisabz tumani", "карши": "Qarshi", "китаб": "Kitob",
    "яккабаг": "Yakkabog'", "гузар": "G'uzor", "камаши": "Qamashi", "чиракчи": "Chiroqchi", "касан": "Koson",
    "самарканд": "Samarqand", "бухара": "Buxoro", "термез": "Termiz", "кунчикар": "Kunchiqar",
}


def to_latin(text):
    """O'zbek/rus kirill yozuvini o'zbek lotiniga o'giradi (OSM ma'lumotlari ko'pincha kirillda)."""
    if not re.search("[А-Яа-яЁёЎўҚқҒғҲҳ]", text or ""):
        return text or ""
    t = text.strip()
    if t.lower() in _UZ_NAMES:
        return _UZ_NAMES[t.lower()]
    for pat, rep in _STREET_RU:
        t = re.sub(pat, rep, t, flags=re.I)
    out = []
    for i, ch in enumerate(t):
        lo = ch.lower()
        if lo in _CYR:
            v = _CYR[lo]
            if lo == "х" and i + 1 < len(t) and t[i + 1].lower() not in "кгх" and i > 0 and t[i - 1].lower() in "аоуэ":
                v = "h"  # Шахрисабз → Shahrisabz (unli orasidagi «х» — h)
            if lo == "е" and (i == 0 or not t[i - 1].isalpha()):
                v = "ye"
            out.append(v.capitalize() if ch.isupper() and v else v)
        else:
            out.append(ch)
    return "".join(out)


# Yandex: bitta kalit ("JavaScript API va HTTP Geocoder") — xarita (uy raqamlari bilan) va manzil aniqlash uchun.
YANDEX_API_KEY = (os.environ.get("YANDEX_API_KEY") or str(CFG.get("YANDEX_API_KEY") or "")).strip()
YANDEX_GEOCODER_KEY = (os.environ.get("YANDEX_GEOCODER_KEY") or str(CFG.get("YANDEX_GEOCODER_KEY") or "") or YANDEX_API_KEY).strip()
YANDEX_JS_KEY = (os.environ.get("YANDEX_JS_KEY") or YANDEX_API_KEY).strip()


def yandex_geocode(lat, lon, lang):
    """Yandex HTTP Geocoder — Shahrisabz uchun uy raqamigacha aniq. YANDEX_GEOCODER_KEY kerak."""
    for yl in (("uz_UZ", "ru_RU") if lang == "uz" else ("ru_RU",)):
        url = "https://geocode-maps.yandex.ru/1.x/?" + urllib.parse.urlencode(
            {"apikey": YANDEX_GEOCODER_KEY, "geocode": f"{lon},{lat}", "format": "json", "results": 1,
             "kind": "house", "lang": yl})
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "UlgurjiBot/1.0", "Referer": CFG["WEBAPP_URL"] + "/"})
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read())
            fm = d["response"]["GeoObjectCollection"]["featureMember"]
            if not fm:
                return ""
            comps = fm[0]["GeoObject"]["metaDataProperty"]["GeocoderMetaData"]["Address"]["Components"]
            get = lambda kind: [c["name"] for c in comps if c.get("kind") == kind]
            prov = get("province")
            region = prov[0] if prov else ""
            city = (get("locality") or [""])[0]
            street = (get("street") or get("district") or [""])[0]
            house = (get("house") or [""])[0]
            if lang == "uz":
                region, city, street = to_latin(region), to_latin(city), to_latin(street)
            parts = []
            for p in (region, city, street):
                if p and all(p.lower() not in q.lower() for q in parts):
                    parts.append(p)
            addr = ", ".join(parts)
            if house:
                addr += f", {house}" if street else (f", {house}" if addr else house)
            return addr
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                log.warning("Yandex geocoder: kalit noto'g'ri yoki bloklangan (%s)", e.code)
                return ""
            log.info("Yandex geocoder %s (%s)", e.code, yl)  # boshqa tilda qayta urinamiz
        except Exception as e:
            log.info("Yandex geocoder xato: %s", e)
            return ""
    return ""


def reverse_geocode(lat, lon, lang="uz"):
    """Koordinatadan manzil: «Qashqadaryo viloyati, Shahrisabz, Nasaf ko'chasi, 76».
    Uy raqami faqat OpenStreetMap'da bo'lsa chiqadi — bo'lmasa mijoz o'zi yozadi."""
    lang = "ru" if lang == "ru" else "uz"
    key = (round(lat, 5), round(lon, 5), lang)
    if key in _geo_cache:
        return _geo_cache[key]
    res = yandex_geocode(lat, lon, lang) if YANDEX_GEOCODER_KEY else ""
    if res:
        _geo_cache[key] = res
        return res
    with _geo_lock:
        wait = 1.05 - (time.time() - _geo_last[0])
        if wait > 0:
            time.sleep(wait)
        _geo_last[0] = time.time()
        url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
            {"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 18, "addressdetails": 1,
             "accept-language": "ru" if lang == "ru" else "uz,ru"})
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"UlgurjiBot/1.0 (+{CFG['WEBAPP_URL']})"})
            with urllib.request.urlopen(req, timeout=8) as r:
                a = json.loads(r.read()).get("address", {})
            res = format_address(a, lang)
        except Exception as e:
            log.info("geocode xato: %s", e)
    if res:
        _geo_cache[key] = res
        if len(_geo_cache) > 3000:
            _geo_cache.clear()
        return res
    return f"{lat:.5f}, {lon:.5f}"


def format_address(a, lang="uz"):
    region = a.get("state") or a.get("region") or ""
    city = a.get("city") or a.get("town") or a.get("village") or a.get("hamlet") or a.get("county") or ""
    road = a.get("road") or a.get("pedestrian") or a.get("residential") or a.get("footway") or ""
    hood = a.get("neighbourhood") or a.get("quarter") or a.get("suburb") or a.get("city_district") or ""
    house = a.get("house_number") or ""
    if lang == "uz":
        region, city, road, hood = (to_latin(x) for x in (region, city, road, hood))
    street = road or hood
    parts = []
    for p in (region, city, street):
        p = (p or "").strip()
        if p and all(p.lower() not in q.lower() for q in parts):
            parts.append(p)
    addr = ", ".join(parts)
    if house and street:
        addr += f", {house}"
    return addr


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
            req = urllib.request.Request(url, headers={"User-Agent": f"UlgurjiBot/1.0 (+{CFG['WEBAPP_URL']})",
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
NEXT = {"new": ["accepted", "cancelled"], "accepted": ["on_way", "cancelled"], "on_way": ["done", "cancelled"]}
BTN = {"accepted": "✅ Qabul qilish", "on_way": "🚚 Yo'lga chiqdi", "done": "🏁 Yetkazildi", "cancelled": "❌ Bekor qilish"}


def allowed_next(order):
    return list(NEXT.get(order["status"], []))


def admin_kb(order, private=True):
    row = [{"text": BTN[s], "callback_data": f"st:{order['id']}:{s}"} for s in allowed_next(order)]
    kb = [row[:2], row[2:]] if len(row) > 2 else ([row] if row else [])
    extra = []
    if order.get("lat"):
        extra.append({"text": "🗺 Xarita", "url": f"https://maps.google.com/?q={order['lat']},{order['lon']}"})
    if private:  # web_app tugmasi faqat shaxsiy chatda ishlaydi
        extra.append({"text": "📋 Panel", "web_app": {"url": app_url("admin.html") + f"#order-{order['id']}"}})
    if extra:
        kb.append(extra)
    return {"inline_keyboard": [r for r in kb if r]}


def items_text(items, lang):
    lines = []
    for it in items:
        name = it["name"].get(lang) or it["name"]["uz"]
        lines.append(f"• {esc(name)} — {esc(qty_text(it, lang))} = {fmt(it['price'] * it['qty'])}")
    return "\n".join(lines)


def zone_name(zid, lang="uz"):
    return ""


SHOP_FALLBACK = "Do'kon"


def admin_text(order, user):
    items = json.loads(order["items"]) if isinstance(order["items"], str) else order["items"]
    who = esc(user.get("name") or "—")
    if user.get("username"):
        who += f" (@{esc(user['username'])})"
    lines = [f"<b>🧾 Buyurtma #{order['id']}</b>  —  {T['uz']['status'][order['status']]}", "",
             f"🏪 <b>{esc(user.get('shop') or SHOP_FALLBACK)}</b>", f"👤 {who}", f"📞 <code>{esc(user.get('phone'))}</code>"]
    if order.get("address"):
        lines.append(f"📍 {esc(order['address'])}")
    lines.append(f"📅 <b>{date_label(order.get('delivery_date'))}</b>")
    lines.append(T["uz"]["pay"].get(order["payment"], order["payment"]))
    bal = db.balance(order["tg_id"])
    if bal > 0:
        lines.append(f"📒 Joriy qarzi: {fmt(bal)} so'm")
    lines += ["", items_text(items, "uz"), "", f"💰 <b>Jami: {fmt(order['total'])} so'm</b>"]
    if order["status"] == "done" and order.get("paid") is not None:
        lines.append(f"💵 To'landi: {fmt(order['paid'])}" + (f"  ·  📒 Nasiyaga: {fmt(order['debt'])}" if order.get("debt") else ""))
    if order["comment"]:
        lines.append(f"💬 {esc(order['comment'])}")
    lines.append(f"🕒 {time.strftime('%d.%m %H:%M', time.localtime(order['created']))}")
    return "\n".join(lines)


def build_items(raw):
    pm = product_map()
    if not isinstance(raw, list) or not raw or len(raw) > 100:
        raise ValueError("cart_empty")
    items, total = [], 0
    for it in raw:
        p = pm.get(str(it.get("pid")))
        unit = it.get("unit") if it.get("unit") in ("pack", "pc") else "pc"
        try:
            qty = int(it.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if not p or not is_available(p) or unit not in sell_units(p) or not (1 <= qty <= 9999):
            raise ValueError("item_unavailable")
        price = unit_price(p, unit)
        rec = {"pid": p["id"], "name": p["name"], "unit": unit, "price": price, "qty": qty,
               "base": UNITS.get(p.get("unit") or "pc", UNITS["pc"])}
        if unit == "pack":
            rec["pack_qty"] = int(p["pack"]["qty"])
            rec["unit_name"] = p["pack"]["name"]
        items.append(rec)
        total += price * qty
    return items, total


def create_order(user, body):
    if not setting("ACCEPTING"):
        raise ValueError("closed")
    if user.get("blocked"):
        raise ValueError("blocked")
    items, total = build_items(body.get("items"))
    if total < int(setting("MIN_ORDER") or 0):
        raise ValueError("min_order")
    date = str(body.get("date") or "")
    if date not in delivery_dates(shop_days(user), 7):
        raise ValueError("bad_date")
    payment = "debt" if body.get("payment") == "debt" and setting("ALLOW_DEBT") else "cash"
    if payment == "debt":
        lim = debt_limit(user)
        if lim and db.balance(user["tg_id"]) + total > lim:
            raise ValueError("debt_limit")
    if user.get("lat") is None:
        raise ValueError("no_address")
    address = user.get("address") or ""
    if user.get("landmark"):
        address += " — " + user["landmark"]
    now = int(time.time())
    oid = db.x("INSERT INTO orders(tg_id,items,total,delivery,payment,address,lat,lon,comment,status,created,updated,delivery_date,zone)"
               " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (user["tg_id"], json.dumps(items, ensure_ascii=False), total, "route", payment, address[:320],
                user["lat"], user["lon"], str(body.get("comment") or "")[:500], "new", now, now, date, None))
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
        tg(CLIENT, "sendMessage", chat_id=user["tg_id"], parse_mode="HTML",
           text=L["order_user"].format(id=order["id"], items=items_text(json.loads(order["items"]), lang),
                                       total=fmt(order["total"]), date=date_label(order["delivery_date"], lang),
                                       pay=L["pay"][order["payment"]]))
    except Exception:
        log.error("notify xato:\n%s", traceback.format_exc())


def refresh_admin_msgs(order, user):
    text = admin_text(order, user)
    for m in db.q("SELECT chat_id,msg_id FROM order_msgs WHERE order_id=?", (order["id"],)):
        tg(ADMIN, "editMessageText", chat_id=m["chat_id"], message_id=m["msg_id"], text=text,
           parse_mode="HTML", reply_markup=admin_kb(order, m["chat_id"] > 0))


def set_status(oid, status, paid=None, by_name=""):
    with db.lock:
        order = db.q("SELECT * FROM orders WHERE id=?", (oid,), one=True)
        if not order:
            return None, "Buyurtma topilmadi"
        if status not in allowed_next(order):
            return order, "Bu holatga o'tkazib bo'lmaydi"
        now = int(time.time())
        if status == "done":
            total = int(order["total"])
            if paid is None:
                paid = total if order["payment"] == "cash" else 0
            paid = max(0, min(total, int(paid)))
            debt = total - paid
            db.x("UPDATE orders SET status=?, paid=?, debt=?, updated=? WHERE id=?", (status, paid, debt, now, oid))
            if debt:
                db.ledger_add(order["tg_id"], debt, "order", oid, f"Buyurtma #{oid}", by_name)
            order.update(paid=paid, debt=debt)
        else:
            db.x("UPDATE orders SET status=?, updated=? WHERE id=?", (status, now, oid))
        order["status"] = status
    user = db.get_user(order["tg_id"]) or {"tg_id": order["tg_id"], "lang": "uz"}

    def _notify():
        refresh_admin_msgs(order, user)
        lang = user.get("lang") or "uz"
        L = T[lang]
        if status == "done":
            text = L["done_user"].format(id=oid, total=fmt(order["total"]), paid=fmt(order["paid"]),
                                         debt_line=L["debt_line"].format(debt=fmt(order["debt"])) if order["debt"] else "",
                                         bal_line=bal_line(db.balance(order["tg_id"]), lang))
        else:
            text = L["status_user"].format(id=oid, status=L["status"][status])
        tg(CLIENT, "sendMessage", chat_id=order["tg_id"], parse_mode="HTML", text=text)

    threading.Thread(target=_notify, daemon=True).start()
    return order, None


def record_payment(uid, amount, kind, note, by_name):
    """kind='payment' — do'kon pul berdi (qarz kamayadi); kind='adjust' — qo'lda tuzatish (+/-)."""
    user = db.get_user(uid)
    if not user:
        raise ValueError("not_found")
    amount = int(amount)
    if kind == "payment":
        if amount <= 0:
            raise ValueError("bad_amount")
        bal = db.ledger_add(uid, -amount, "payment", None, note, by_name)
    else:
        if amount == 0:
            raise ValueError("bad_amount")
        bal = db.ledger_add(uid, amount, "adjust", None, note, by_name)
    lang = user.get("lang") or "uz"
    L = T[lang]
    nt = ("💬 " + esc(note)) if note else ""
    text = (L["pay_user"] if kind == "payment" else L["adj_user"]).format(
        amount=fmt(abs(amount)) if kind == "payment" else ("+" if amount > 0 else "−") + fmt(abs(amount)),
        note=nt, bal_line=bal_line(bal, lang))
    threading.Thread(target=tg, args=(CLIENT, "sendMessage"), kwargs={"chat_id": uid, "text": re.sub(r"\n\n+", "\n\n", text),
                                                                      "parse_mode": "HTML"}, daemon=True).start()
    return bal


# ═════════════════════════════ Bot: do'konlar ═════════════════════════════
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
    tg(CLIENT, "sendMessage", chat_id=chat_id, text=tr(lang, "hello").format(company=esc(company())), parse_mode="HTML",
       reply_markup={"keyboard": [[{"text": tr(lang, "send_phone"), "request_contact": True}]],
                     "resize_keyboard": True, "one_time_keyboard": True})


def ask_shop(chat_id, lang):
    tg(CLIENT, "sendMessage", chat_id=chat_id, text=tr(lang, "ask_shop"), parse_mode="HTML", reply_markup={"remove_keyboard": True})


def ask_location(chat_id, lang):
    tg(CLIENT, "sendMessage", chat_id=chat_id, text=tr(lang, "ask_loc"), parse_mode="HTML",
       reply_markup={"keyboard": [[{"text": tr(lang, "send_loc"), "request_location": True}],
                                  [{"text": tr(lang, "skip_loc")}]], "resize_keyboard": True, "one_time_keyboard": True})


def send_ready(chat_id, lang, user=None):
    text = tr(lang, "ready")
    head = []
    if user and user.get("shop"):
        head.append(tr(lang, "shop_line").format(s=esc(user["shop"])))
    if user and user.get("address"):
        head.append(tr(lang, "addr").format(a=esc(user["address"])))
    if user and user.get("lat") is not None:
        head.append(tr(lang, "days_line").format(d=days_text(shop_days(user), lang)))
    if head:
        text = "\n".join(head) + "\n\n" + text
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
    u = db.get_user(uid)
    if u and u.get("blocked"):
        tg(CLIENT, "sendMessage", chat_id=chat, text=tr(lang, "blocked").format(phone=setting("SUPPORT_PHONE")))
        return

    if text.split("@")[0].split(" ")[0] == "/start":
        if not u:
            u = db.upsert_user(uid, name=name, username=frm.get("username"), lang=lang)
        elif u.get("username") != frm.get("username"):
            db.upsert_user(uid, username=frm.get("username"))
        if SINGLE_BOT and (is_owner(uid) or staff_by_tg(uid)):
            tg(CLIENT, "sendMessage", chat_id=chat, text="👨‍💼 Admin panel uchun /admin buyrug'ini yuboring.")
        if not u.get("phone"):
            ask_phone(chat, lang)
        elif not u.get("shop"):
            ask_shop(chat, lang)
        elif u.get("lat") is None:
            ask_location(chat, lang)
        else:
            send_ready(chat, lang, u)
        return

    if msg.get("contact"):
        c = msg["contact"]
        if c.get("user_id") != uid:
            tg(CLIENT, "sendMessage", chat_id=chat, text=tr(lang, "own_phone"))
            return
        phone = re.sub(r"[^\d+]", "", c.get("phone_number", ""))
        if not phone.startswith("+"):
            phone = "+" + phone
        u = db.upsert_user(uid, phone=phone, name=(u or {}).get("name") or name, username=frm.get("username"), lang=lang)
        if not u.get("shop"):
            ask_shop(chat, lang)
        else:
            ask_location(chat, lang)
        return

    if not u or not u.get("phone"):
        ask_phone(chat, lang)
        return

    if msg.get("location"):
        lat, lon = msg["location"]["latitude"], msg["location"]["longitude"]
        addr = reverse_geocode(lat, lon, lang)
        u = db.upsert_user(uid, lat=lat, lon=lon, address=addr)
        if not u.get("shop"):
            ask_shop(chat, lang)
        else:
            send_ready(chat, lang, u)
        return

    if not u.get("shop"):
        if text and not text.startswith("/") and 2 <= len(text) <= 60:
            u = db.upsert_user(uid, shop=re.sub(r"\s+", " ", text))
            if u.get("lat") is None:
                ask_location(chat, lang)
            else:
                send_ready(chat, lang, u)
        else:
            tg(CLIENT, "sendMessage", chat_id=chat, text=tr(lang, "bad_shop"))
        return

    if text in (T["uz"]["skip_loc"], T["ru"]["skip_loc"]):
        send_ready(chat, lang, u)
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
    admin_cmds = ("/admin", "/panel", "/stats", "/myid", "/yuk") if SINGLE_BOT else \
        ("/admin", "/panel", "/stats", "/myid", "/yuk", "/start", "")
    if cmd not in admin_cmds:
        return False
    if cmd == "/myid":
        tg(ADMIN, "sendMessage", chat_id=chat, text=f"Sizning Telegram ID: <code>{uid}</code>", parse_mode="HTML")
        return True
    owner, staff = is_owner(uid), staff_by_tg(uid)
    perms = PERMS if owner else (staff["perms"] if staff else [])
    if cmd == "/stats" and "stats" in perms:
        r = stats_today()
        d = debt_summary()
        tg(ADMIN, "sendMessage", chat_id=chat, parse_mode="HTML",
           text=f"📊 <b>Bugun</b>\nBuyurtmalar: {r['orders']}\nSavdo: {fmt(r['revenue'])} so'm\n\n"
                f"📒 Umumiy qarz: <b>{fmt(d['total'])} so'm</b> ({d['count']} ta do'kon)")
        return True
    if cmd == "/yuk" and "orders" in perms:
        dates = [r["d"] for r in db.q("SELECT DISTINCT delivery_date d FROM orders WHERE status IN ('new','accepted','on_way') "
                                      "AND delivery_date>=? ORDER BY delivery_date LIMIT 1", (_today()[0].isoformat(),))]
        if not dates:
            tg(ADMIN, "sendMessage", chat_id=chat, text="🚚 Yetkaziladigan faol buyurtma yo'q.")
        else:
            for part in split_msg(loadlist_text(loadlist(dates[0]))):
                tg(ADMIN, "sendMessage", chat_id=chat, text=part, parse_mode="HTML")
        return True
    kb = {"inline_keyboard": [[{"text": "📊 Admin panelni ochish", "web_app": {"url": app_url("admin.html")}}]]}
    if not SINGLE_BOT:
        tg(ADMIN, "setChatMenuButton", chat_id=chat,
           menu_button={"type": "web_app", "text": "Panel", "web_app": {"url": CFG["WEBAPP_URL"] + "/admin.html"}})
    if owner or staff:
        new = db.q("SELECT COUNT(*) c FROM orders WHERE status='new'", one=True)["c"]
        who = "Rahbar" if owner else esc(staff["name"] or staff["login"])
        tg(ADMIN, "sendMessage", chat_id=chat, parse_mode="HTML", reply_markup=kb,
           text=f"Salom, <b>{who}</b>! 👋\n\nDo'konlardan yangi buyurtmalar shu chatga keladi.\n"
                f"Hozir kutilayotgan: <b>{new}</b> ta.\n\n/yuk — eng yaqin kunning yuklash ro'yxati")
        if owner and BACKUP.on and (not BACKUP.msg_id or BACKUP.fails):
            BACKUP.fails = 0
            threading.Thread(target=BACKUP.run, kwargs={"force": True}, daemon=True).start()
    else:
        tg(ADMIN, "sendMessage", chat_id=chat, parse_mode="HTML", reply_markup=kb,
           text=f"👨‍💼 <b>{esc(company())} — xodimlar paneli</b>\n\nRahbar bergan login va parol bilan kiring. "
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
    who = "Rahbar" if is_owner(uid) else ((staff_by_tg(uid) or {}).get("name") or "")
    order, err = set_status(int(oid), st, None, who)
    msg = err or T["uz"]["status"][st]
    if not err and st == "done" and order.get("debt"):
        msg += f" · nasiyaga {fmt(order['debt'])}"
    tg(ADMIN, "answerCallbackQuery", callback_query_id=cq["id"], text=msg)


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
    log.info("%s bot: @%s", "Do'konlar" if bot == CLIENT else "Admin", (me or {}).get("username"))
    if bot == CLIENT:
        tg(bot, "setMyCommands", commands=[{"command": "start", "description": "Boshlash / Начать"}])
        if SINGLE_BOT:
            for oid in CFG["ADMIN_IDS"]:
                tg(bot, "setMyCommands", scope={"type": "chat", "chat_id": oid},
                   commands=[{"command": "start", "description": "Boshlash"}, {"command": "admin", "description": "Admin panel"},
                             {"command": "yuk", "description": "Yuklash ro'yxati"}, {"command": "stats", "description": "Bugungi statistika"}])
    else:
        tg(bot, "setMyCommands", commands=[{"command": "start", "description": "Admin panel"},
                                           {"command": "yuk", "description": "Yuklash ro'yxati"},
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


# ═════════════════════════════ Statistics / reports ═════════════════════════════
def day_start(ts=None):
    lt = time.localtime(ts or time.time())
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


def stats_today():
    r = db.q("SELECT COUNT(*) c, COALESCE(SUM(total),0) s FROM orders WHERE created>=? AND status!='cancelled'",
             (day_start(),), one=True)
    return {"orders": r["c"], "revenue": r["s"], "avg": (r["s"] // r["c"]) if r["c"] else 0}


def debt_summary():
    rows = db.q("SELECT tg_id, SUM(amount) b FROM ledger GROUP BY tg_id HAVING b>0")
    return {"total": sum(r["b"] for r in rows), "count": len(rows)}


def item_pcs(it):
    return it["qty"] * (it.get("pack_qty") or 1) if it.get("unit") == "pack" else it["qty"]


def dashboard(days=7):
    days = max(1, min(int(days), 31))
    start = day_start() - (days - 1) * 86400
    by_day = [{"day": start + i * 86400, "revenue": 0, "orders": 0} for i in range(days)]
    top, pay = {}, {"cash": 0, "debt": 0}
    for r in db.q("SELECT total,status,created,items,payment FROM orders WHERE created>=?", (start,)):
        if r["status"] == "cancelled":
            continue
        i = min(days - 1, (day_start(r["created"]) - start) // 86400)
        by_day[i]["revenue"] += r["total"]
        by_day[i]["orders"] += 1
        pay[r["payment"]] = pay.get(r["payment"], 0) + r["total"]
        for it in json.loads(r["items"]):
            t = top.setdefault(it["pid"], {"pid": it["pid"], "name": it["name"], "pcs": 0, "revenue": 0,
                                           "base": it.get("base") or UNITS["pc"]})
            t["pcs"] += item_pcs(it)
            t["revenue"] += it["qty"] * it["price"]
    yest = db.q("SELECT COALESCE(SUM(total),0) s, COUNT(*) c FROM orders WHERE created>=? AND created<? AND status!='cancelled'",
                (day_start() - 86400, day_start()), one=True)
    active = db.q("SELECT COUNT(*) c FROM orders WHERE status IN ('new','accepted','on_way')", one=True)["c"]
    up = db.q("SELECT delivery_date d, COUNT(*) c, COALESCE(SUM(total),0) s FROM orders WHERE status IN ('new','accepted','on_way') "
              "AND delivery_date>=? GROUP BY delivery_date ORDER BY delivery_date LIMIT 7", (_today()[0].isoformat(),))
    return {"today": stats_today(), "yesterday": {"revenue": yest["s"], "orders": yest["c"]}, "by_day": by_day,
            "top": sorted(top.values(), key=lambda x: -x["revenue"])[:5], "pay": pay, "active": active,
            "debt": debt_summary(), "upcoming": [{"date": r["d"], "orders": r["c"], "sum": r["s"], "label": date_label(r["d"])} for r in up],
            "shops": db.q("SELECT COUNT(*) c FROM users WHERE phone IS NOT NULL", one=True)["c"],
            "new_shops": db.q("SELECT COUNT(*) c FROM users WHERE phone IS NOT NULL AND created>=?", (day_start(),), one=True)["c"]}


def loadlist(date, zone=None):  # zone — eski parametr, ishlatilmaydi
    """Bir kunlik yuklash ro'yxati: qaysi mahsulotdan qancha ortish va qaysi do'konlarga borish."""
    sql = ("SELECT o.*, u.shop ushop, u.name uname, u.phone uphone, u.landmark ulandmark FROM orders o "
           "LEFT JOIN users u ON u.tg_id=o.tg_id WHERE o.delivery_date=? AND o.status IN ('new','accepted','on_way')")
    args = [date]
    rows = db.q(sql + " ORDER BY o.id", args)
    prods, pm = {}, product_map()
    tot = {"orders": len(rows), "sum": 0, "cash": 0, "debt": 0, "new": 0}
    out = []
    for r in rows:
        items = json.loads(r["items"])
        tot["sum"] += r["total"]
        tot["cash" if r["payment"] == "cash" else "debt"] += r["total"]
        tot["new"] += r["status"] == "new"
        for it in items:
            p = prods.setdefault(it["pid"], {"pid": it["pid"], "name": it["name"], "pcs": 0, "sum": 0,
                                             "base": it.get("base") or UNITS["pc"], "pack_qty": 0, "pack_name": None})
            p["pcs"] += item_pcs(it)
            p["sum"] += it["qty"] * it["price"]
            cur = pm.get(it["pid"]) or {}
            pk = (cur.get("pack") or {}) if cur else {}
            if it.get("pack_qty") or pk.get("qty"):
                p["pack_qty"] = int(it.get("pack_qty") or pk.get("qty"))
                p["pack_name"] = it.get("unit_name") or pk.get("name")
        out.append({"id": r["id"], "status": r["status"], "shop": r["ushop"], "name": r["uname"], "phone": r["uphone"],
                    "address": r["address"], "lat": r["lat"], "lon": r["lon"], "zone_name": "",
                    "total": r["total"], "payment": r["payment"], "comment": r["comment"], "items": items})
    for p in prods.values():
        if p["pack_qty"]:
            p["packs"], p["rest"] = divmod(p["pcs"], p["pack_qty"])
    return {"date": date, "label": date_label(date), "orders": out, "totals": tot,
            "products": sorted(prods.values(), key=lambda x: L_uz(x["name"]))}


def L_uz(n):
    return (n or {}).get("uz") or ""


def pcs_text(p):
    base = p["base"].get("uz", "dona")
    if p.get("pack_qty"):
        pk = (p.get("pack_name") or {}).get("uz", "qadoq")
        s = f"{p['packs']} {pk}" if p["packs"] else ""
        if p["rest"]:
            s += (" + " if s else "") + f"{p['rest']} {base}"
        return f"{s} ({p['pcs']} {base})"
    return f"{p['pcs']} {base}"


def loadlist_text(ll):
    t = ll["totals"]
    lines = [f"🚚 <b>Yuklash ro'yxati — {ll['label']}</b>",
             f"Buyurtmalar: {t['orders']} ta · Jami: <b>{fmt(t['sum'])} so'm</b>",
             f"💵 Naqd: {fmt(t['cash'])}  ·  📒 Nasiya: {fmt(t['debt'])}", "", "📦 <b>Mahsulotlar</b>"]
    lines += [f"• {esc(L_uz(p['name']))} — {esc(pcs_text(p))}" for p in ll["products"]]
    lines += ["", "🏪 <b>Do'konlar</b>"]
    zn = None
    for i, o in enumerate(ll["orders"], 1):
        if o["zone_name"] and o["zone_name"] != zn:
            zn = o["zone_name"]
            lines.append(f"\n🗺 <b>{esc(zn)}</b>")
        lines.append(f"{i}. <b>{esc(o['shop'] or '—')}</b> · #{o['id']} · {fmt(o['total'])} so'm · "
                     f"{'Naqd' if o['payment'] == 'cash' else 'Nasiya'}\n    📞 {esc(o['phone'] or '')}  📍 {esc(o['address'] or '')}")
    return "\n".join(lines)


def split_msg(text, n=3900):
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > n:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    return parts


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
                                  "landmark", "shop")} if u else None


def client_settings():
    s = all_settings()
    return {"company": company(), "base": {"lat": s["BASE_LAT"], "lon": s["BASE_LON"],
                                           "address": {"uz": s["BASE_ADDRESS_UZ"], "ru": s["BASE_ADDRESS_RU"]}},
            "support_phone": s["SUPPORT_PHONE"], "support_username": s["SUPPORT_USERNAME"],
            "work_hours": s["WORK_HOURS"] or "", "min_order": int(s["MIN_ORDER"] or 0),
            "accepting": bool(s["ACCEPTING"]), "closed_msg": {"uz": s["CLOSED_MSG_UZ"], "ru": s["CLOSED_MSG_RU"]},
            "allow_debt": bool(s["ALLOW_DEBT"]), "map_token": MAPBOX_TOKEN, "ymaps_key": YANDEX_JS_KEY}


def account(user):
    """Do'konchi uchun: qarz, chegara, yetkazish sanalari."""
    lang = user.get("lang") or "uz"
    days = shop_days(user)
    return {"balance": db.balance(user["tg_id"]), "limit": debt_limit(user), "days": days,
            "dates": [{"date": d, "label": date_label(d, lang)} for d in delivery_dates(days, 4)]}


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


def _int(v, lo, hi, err):
    try:
        n = int(float(str(v).replace(" ", "")))
    except (TypeError, ValueError):
        raise ValueError(err)
    if not lo <= n <= hi:
        raise ValueError(err)
    return n


def clean_product(p, existing=None):
    if p.get("cat") not in {c["id"] for c in MENU.data["categories"]}:
        raise ValueError("bad_category")
    out = dict(existing or {})
    out.pop("variants", None)
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
    if p.get("badge") in ("hit", "new", "sale"):
        out["badge"] = p["badge"]
    else:
        out.pop("badge", None)
    out["available"] = p.get("available", True) is not False
    bc = re.sub(r"\D", "", str(p.get("barcode") or ""))
    if bc:
        if not 6 <= len(bc) <= 14:
            raise ValueError("bad_barcode")
        if any(x.get("barcode") == bc and x.get("id") != (existing or {}).get("id") for x in MENU.data["products"]):
            raise ValueError("barcode_taken")
        out["barcode"] = bc
    else:
        out.pop("barcode", None)
    out["price"] = _int(p.get("price"), 1, 10 ** 10, "bad_price")
    out["unit"] = p.get("unit") if p.get("unit") in UNITS else "pc"
    pk = p.get("pack") or None
    if pk and pk.get("qty"):
        pack = {"name": clean_l10n(pk.get("name") or {"uz": "Korobka", "ru": "Коробка"}, limit=20),
                "qty": _int(pk.get("qty"), 2, 100000, "bad_pack")}
        if pk.get("price") not in (None, "", 0, "0"):
            pack["price"] = _int(pk.get("price"), 1, 10 ** 11, "bad_price")
        out["pack"] = pack
        out["sell"] = p.get("sell") if p.get("sell") in ("both", "pack", "pc") else "both"
    else:
        out.pop("pack", None)
        out.pop("sell", None)
    return out


OFF_API = os.environ.get("OFF_API") or "https://world.openfoodfacts.org"


def barcode_lookup(code):
    """Shtrix-kod bo'yicha mahsulot: avval o'z katalogimizdan, keyin Open Food Facts bazasidan."""
    p = next((x for x in MENU.data["products"] if x.get("barcode") == code), None)
    if p:
        return {"found": "local", "product_id": p["id"]}
    url = f"{OFF_API}/api/v2/product/{code}.json?fields=product_name,product_name_ru,product_name_uz,product_name_en,brands,quantity,image_front_url,image_url"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "UlgurjiBot/1.0 (wholesale catalog)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read(3 * 1024 * 1024))
    except Exception as e:
        log.info("barcode %s: %s", code, e)
        return {"found": None, "error": "lookup_failed"}
    pr = d.get("product") if d.get("status") == 1 else None
    if not pr:
        return {"found": None}
    nm_uz = (pr.get("product_name_uz") or pr.get("product_name") or pr.get("product_name_en") or pr.get("product_name_ru") or "").strip()
    nm_ru = (pr.get("product_name_ru") or pr.get("product_name") or nm_uz).strip()
    brand = (pr.get("brands") or "").split(",")[0].strip()
    qty = (pr.get("quantity") or "").strip()
    out = {"found": "off", "name": {"uz": nm_uz, "ru": nm_ru}, "brand": brand, "quantity": qty, "img": ""}
    img = pr.get("image_front_url") or pr.get("image_url")
    if img and img.startswith("https://"):
        try:
            with urllib.request.urlopen(urllib.request.Request(img, headers={"User-Agent": "UlgurjiBot/1.0"}), timeout=10) as r:
                data = r.read(4 * 1024 * 1024)
            ext = "jpg" if data[:3] == b"\xff\xd8\xff" else "png" if data[:4] == b"\x89PNG" else None
            if ext:
                os.makedirs(os.path.join(UPLOAD_DIR, "products"), exist_ok=True)
                src = f"uploads/products/{int(time.time())}_{code}.{ext}"
                with open(os.path.join(WEBAPP_DIR, src), "wb") as f:
                    f.write(data)
                BACKUP.mark()
                out["img"] = src
        except Exception as e:
            log.info("barcode rasm %s: %s", code, e)
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


def broadcast_targets(day=None):
    users = db.q("SELECT tg_id,address,landmark,days FROM users WHERE phone IS NOT NULL AND COALESCE(blocked,0)=0")
    if day not in (None, ""):
        rts = routes()
        users = [u for u in users if int(day) in (shop_days(u, rts) or WORKDAYS)]
    return users


def broadcast(text, notify_chat, day=None):
    users = broadcast_targets(day)
    ok = 0
    for u in users:
        if tg(CLIENT, "sendMessage", chat_id=u["tg_id"], text=text):
            ok += 1
        time.sleep(0.05)
    if notify_chat:
        tg(ADMIN, "sendMessage", chat_id=notify_chat, text=f"📣 Xabar yuborildi: {ok}/{len(users)} do'kon")


def public_staff(s):
    return {"id": s["id"], "login": s["login"], "name": s["name"],
            "perms": json.loads(s["perms"] or "[]") if isinstance(s["perms"], str) else s["perms"],
            "active": bool(s["active"]), "telegram": bool(s["tg_id"]), "last_login": s["last_login"]}


# admin marshrut → kerakli ruxsat (None = har qanday kirgan xodim; "owner" = faqat rahbar)
ROUTE_PERM = {
    "me": None, "logout": None, "settings": None, "menu": None, "routes": None,
    "dashboard": "stats", "orders": "orders", "order": "orders", "order/status": "orders",
    "loadlist": "orders", "loadlist/send": "orders",
    "product/toggle": ("stock", "menu"), "product/save": "menu", "product/delete": "menu",
    "category/save": "menu", "category/delete": "menu",
    "banner/save": "banners", "banner/delete": "banners",
    "settings/save": "settings", "geocode": "settings", "routes/save": "settings", "barcode": "menu",
    "shops": ("shops", "debts"), "shop": ("shops", "debts"), "shop/save": "shops",
    "debts": "debts", "payment": "debts", "adjust": "debts", "ledger/delete": "owner",
    "broadcast": "broadcast",
    "staff": "owner", "staff/save": "owner", "staff/delete": "owner",
}
UPLOAD_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "video/mp4": "mp4", "video/quicktime": "mov"}


class Handler(BaseHTTPRequestHandler):
    server_version = "Ulgurji/1.0"
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
            reg = bool(user and user.get("phone") and user.get("shop"))
            if user and user.get("blocked"):
                return self.send_json({"ok": False, "error": "blocked", "phone": setting("SUPPORT_PHONE")}, 403)
            return self.send_json({"ok": True, "user": public_user(user), "registered": reg,
                                   "account": account(user) if reg else None})
        if not user or not user.get("phone"):
            return self.send_json({"ok": False, "error": "not_registered"}, 403)
        if user.get("blocked"):
            return self.send_json({"ok": False, "error": "blocked"}, 403)
        if route == "account":
            return self.send_json({"ok": True, "user": public_user(user), "account": account(user)})
        if route == "profile":
            f = {}
            for k, lim in (("name", 60), ("shop", 60)):
                if k in body:
                    v = re.sub(r"\s+", " ", str(body[k])).strip()[:lim]
                    if len(v) < 2:
                        raise ValueError("bad_" + k)
                    f[k] = v
            if body.get("lang") in ("uz", "ru"):
                f["lang"] = body["lang"]
            u = db.upsert_user(uid, **f)
            if "lang" in f:
                set_client_menu(uid, f["lang"])
            return self.send_json({"ok": True, "user": public_user(u), "account": account(u)})
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
            land = re.sub(r"\s+", " ", str(body.get("landmark") or "")).strip()[:120]
            u = db.upsert_user(uid, lat=lat, lon=lon, address=addr, landmark=land)
            return self.send_json({"ok": True, "user": public_user(u), "account": account(u)})
        if route == "geocode":
            return self.send_json({"ok": True, "address": reverse_geocode(float(body["lat"]), float(body["lon"]),
                                                                          body.get("lang") or user.get("lang"))})
        if route == "order":
            if not user.get("shop"):
                return self.send_json({"ok": False, "error": "not_registered"}, 403)
            if not rate_ok(("order", uid), 10):
                return self.send_json({"ok": False, "error": "too_fast"}, 429)
            try:
                order = create_order(user, body)
            except ValueError as e:
                _rate.pop(("order", uid), None)
                extra = {}
                if str(e) == "debt_limit":
                    extra = {"balance": db.balance(uid), "limit": debt_limit(user)}
                return self.send_json({"ok": False, "error": str(e), **extra}, 400)
            return self.send_json({"ok": True, "order": {k: order[k] for k in ("id", "total", "status", "payment", "delivery_date")},
                                   "label": date_label(order["delivery_date"], user.get("lang") or "uz")})
        if route == "orders":
            rows = db.q("SELECT id,items,total,payment,address,status,created,delivery_date,paid,debt,comment FROM orders "
                        "WHERE tg_id=? ORDER BY id DESC LIMIT 40", (uid,))
            lang = user.get("lang") or "uz"
            for r in rows:
                r["items"] = json.loads(r["items"])
                r["date_label"] = date_label(r["delivery_date"], lang)
            return self.send_json({"ok": True, "orders": rows})
        if route == "ledger":
            rows = db.q("SELECT id,amount,kind,order_id,note,created FROM ledger WHERE tg_id=? ORDER BY id DESC LIMIT 60", (uid,))
            return self.send_json({"ok": True, "entries": rows, "balance": db.balance(uid), "limit": debt_limit(user)})
        if route == "logout":
            if db.balance(uid) > 0 or db.q("SELECT 1 FROM orders WHERE tg_id=? AND status IN ('new','accepted','on_way')", (uid,), one=True):
                # qarzi yoki faol buyurtmasi bor do'kon o'chirilmaydi — faqat bot menyusi yopiladi
                tg(CLIENT, "setChatMenuButton", chat_id=uid, menu_button={"type": "default"})
                return self.send_json({"ok": False, "error": "has_debt"}, 400)
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
            groups = {"active": ACTIVE, "done": ("done",), "cancelled": ("cancelled",)}
            sts = groups.get(st, (st,) if st in STATUS_ORDER else groups["active"])
            sql = (f"SELECT o.*, u.name uname, u.phone uphone, u.username uusername, u.shop ushop FROM orders o "
                   f"LEFT JOIN users u ON u.tg_id=o.tg_id WHERE o.status IN ({','.join('?' * len(sts))})")
            args = list(sts)
            if body.get("date"):
                sql += " AND o.delivery_date=?"
                args.append(str(body["date"]))
            if body.get("q"):
                qq = str(body["q"]).strip().lstrip("#")
                if qq.isdigit():
                    sql += " AND (o.id=? OR u.phone LIKE ?)"
                    args += [int(qq), f"%{qq}%"]
                else:
                    sql += " AND (u.name LIKE ? OR u.shop LIKE ?)"
                    args += [f"%{qq}%", f"%{qq}%"]
            rows = db.q(sql + " ORDER BY o.id DESC LIMIT 60", args)
            for r in rows:
                r["items"] = json.loads(r["items"])
                r["next"] = allowed_next(r)
                r["date_label"] = date_label(r["delivery_date"])
                r["zone_name"] = zone_name(r["zone"])
                r.pop("admin_msg_id", None)
            counts = {r["status"]: r["c"] for r in db.q("SELECT status, COUNT(*) c FROM orders WHERE status IN ('new','accepted','on_way') GROUP BY status")}
            return self.send_json({"ok": True, "orders": rows, "active_counts": counts})
        if route == "order":
            r = db.q("SELECT o.*, u.name uname, u.phone uphone, u.username uusername, u.shop ushop FROM orders o "
                     "LEFT JOIN users u ON u.tg_id=o.tg_id WHERE o.id=?", (int(body["id"]),), one=True)
            if not r:
                return self.send_json({"ok": False, "error": "not_found"}, 404)
            r["items"] = json.loads(r["items"])
            r["next"] = allowed_next(r)
            r["date_label"] = date_label(r["delivery_date"])
            r["zone_name"] = zone_name(r["zone"])
            r["balance"] = db.balance(r["tg_id"])
            return self.send_json({"ok": True, "order": r})
        if route == "order/status":
            paid = body.get("paid")
            order, err = set_status(int(body["id"]), str(body["status"]), None if paid in (None, "") else int(paid), me["name"])
            if err:
                return self.send_json({"ok": False, "error": err}, 400)
            return self.send_json({"ok": True, "status": order["status"], "next": allowed_next(order),
                                   "paid": order.get("paid"), "debt": order.get("debt")})
        if route == "loadlist":
            date = str(body.get("date") or _today()[0].isoformat())
            days = db.q("SELECT delivery_date d, COUNT(*) c FROM orders WHERE status IN ('new','accepted','on_way') AND delivery_date>=? "
                        "GROUP BY delivery_date ORDER BY delivery_date LIMIT 10", (_today()[0].isoformat(),))
            return self.send_json({"ok": True, **loadlist(date),
                                   "days": [{"date": d["d"], "orders": d["c"], "label": date_label(d["d"])} for d in days]})
        if route == "loadlist/send":
            chat = (tu or {}).get("id") or ((me.get("staff") or {}).get("tg_id")) or (CFG["ADMIN_IDS"][0] if me["owner"] and CFG["ADMIN_IDS"] else None)
            if not chat:
                raise ValueError("no_chat")
            for part in split_msg(loadlist_text(loadlist(str(body.get("date"))))):
                tg(ADMIN, "sendMessage", chat_id=chat, text=part, parse_mode="HTML")
            return self.send_json({"ok": True})

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
        if route == "barcode":
            code = re.sub(r"\D", "", str(body.get("code") or ""))
            if not 6 <= len(code) <= 14:
                raise ValueError("bad_barcode")
            return self.send_json({"ok": True, "code": code, **barcode_lookup(code)})
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
            return self.send_json({"ok": True, "settings": all_settings(), "map_token": MAPBOX_TOKEN, "ymaps_key": YANDEX_JS_KEY})
        if route == "geocode":
            lat, lon = float(body["lat"]), float(body["lon"])
            return self.send_json({"ok": True, "uz": reverse_geocode(lat, lon, "uz"), "ru": reverse_geocode(lat, lon, "ru")})
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
            if not (-90 <= float(s.get("BASE_LAT", setting("BASE_LAT")) or 0) <= 90):
                raise ValueError("bad_coords")
            db.kv_set("settings", s)
            return self.send_json({"ok": True, "settings": all_settings()})

        if route == "routes":
            rts = routes()
            cnt, none = {d: 0 for d in rts}, 0
            for u in db.q("SELECT address,landmark,days FROM users WHERE phone IS NOT NULL"):
                ds = shop_days(u, rts)
                none += not ds
                for d in ds:
                    cnt[str(d)] += 1
            return self.send_json({"ok": True, "routes": rts, "shops": cnt, "unassigned": none})
        if route == "routes/save":
            src = body.get("routes") or {}
            rts = routes()
            for d in WORKDAYS:
                if str(d) in src:
                    kws = []
                    for k in src[str(d)] or []:
                        k = re.sub(r"\s+", " ", str(k)).strip()[:60]
                        if k and norm_place(k) and k.lower() not in [x.lower() for x in kws]:
                            kws.append(k)
                    rts[str(d)] = kws[:80]
            db.kv_set("routes", rts)
            return self.send_json({"ok": True, "routes": rts})

        if route in ("shops", "debts"):
            sql = ("SELECT u.tg_id,u.shop,u.name,u.phone,u.username,u.address,u.landmark,u.days,u.created,u.blocked,u.debt_limit,"
                   " (SELECT COALESCE(SUM(amount),0) FROM ledger l WHERE l.tg_id=u.tg_id) balance,"
                   " (SELECT COUNT(*) FROM orders o WHERE o.tg_id=u.tg_id AND o.status!='cancelled') orders,"
                   " (SELECT COALESCE(SUM(total),0) FROM orders o WHERE o.tg_id=u.tg_id AND o.status='done') spent,"
                   " (SELECT MAX(created) FROM orders o WHERE o.tg_id=u.tg_id) last_order"
                   " FROM users u WHERE u.phone IS NOT NULL")
            args = []
            if body.get("q"):
                qq = f"%{str(body['q']).strip()}%"
                sql += " AND (u.shop LIKE ? OR u.name LIKE ? OR u.phone LIKE ?)"
                args += [qq, qq, qq]
            rows = db.q(sql, args)
            rts = routes()
            for r in rows:
                r["days_list"] = shop_days(r, rts)
                r["manual"] = manual_days(r) is not None
            if body.get("day") not in (None, ""):
                if body["day"] == "-":
                    rows = [r for r in rows if not r["days_list"]]
                else:
                    rows = [r for r in rows if int(body["day"]) in r["days_list"]]
            if route == "debts":
                rows = sorted([r for r in rows if r["balance"] > 0], key=lambda r: -r["balance"])
            else:
                rows.sort(key=lambda r: (-(r["last_order"] or 0), -r["created"]))
            for r in rows:
                r["limit"] = debt_limit(r)
                r.pop("days", None)
            return self.send_json({"ok": True, "shops": rows[:300], "total": len(rows), "debt": debt_summary()})
        if route == "shop":
            uid = int(body["id"])
            u = db.get_user(uid)
            if not u:
                return self.send_json({"ok": False, "error": "not_found"}, 404)
            u["days_list"] = shop_days(u)
            u["auto_days"] = matched_days(u)
            u["manual"] = manual_days(u) is not None
            u["balance"] = db.balance(uid)
            u["limit"] = debt_limit(u)
            led = db.q("SELECT * FROM ledger WHERE tg_id=? ORDER BY id DESC LIMIT 40", (uid,))
            ords = db.q("SELECT id,total,status,payment,delivery_date,paid,debt,created FROM orders WHERE tg_id=? ORDER BY id DESC LIMIT 15", (uid,))
            for o in ords:
                o["date_label"] = date_label(o["delivery_date"])
            return self.send_json({"ok": True, "shop": u, "ledger": led, "orders": ords})
        if route == "shop/save":
            uid = int(body["id"])
            if not db.get_user(uid):
                return self.send_json({"ok": False, "error": "not_found"}, 404)
            f = {}
            if "shop" in body:
                v = re.sub(r"\s+", " ", str(body["shop"] or "")).strip()[:60]
                if len(v) < 2:
                    raise ValueError("bad_shop")
                f["shop"] = v
            if "days" in body:
                ds = body["days"]
                f["days"] = json.dumps(sorted({int(d) for d in ds if int(d) in WORKDAYS})) if isinstance(ds, list) and ds else None
            if "debt_limit" in body:
                f["debt_limit"] = None if body["debt_limit"] in (None, "") else _int(body["debt_limit"], 0, 10 ** 12, "bad_amount")
            if "note" in body:
                f["note"] = str(body["note"] or "")[:300]
            if "blocked" in body:
                f["blocked"] = 1 if body["blocked"] else 0
            u = db.upsert_user(uid, **f)
            return self.send_json({"ok": True, "shop": u})
        if route in ("payment", "adjust"):
            amount = _int(body.get("amount"), -10 ** 12, 10 ** 12, "bad_amount")
            bal = record_payment(int(body["id"]), amount, route, str(body.get("note") or "").strip()[:200], me["name"])
            return self.send_json({"ok": True, "balance": bal})
        if route == "ledger/delete":
            e = db.q("SELECT * FROM ledger WHERE id=?", (int(body["id"]),), one=True)
            if not e or e["kind"] == "order":
                raise ValueError("cannot_delete")
            db.x("DELETE FROM ledger WHERE id=?", (e["id"],))
            return self.send_json({"ok": True, "balance": db.balance(e["tg_id"])})

        if route == "broadcast":
            text = str(body.get("text") or "").strip()[:3500]
            if not text:
                raise ValueError("empty")
            if not rate_ok(("bc", me["id"], me["owner"]), 60):
                return self.send_json({"ok": False, "error": "too_fast"}, 429)
            day = body.get("day")
            day = int(day) if str(day) in [str(d) for d in WORKDAYS] else None
            n = len(broadcast_targets(day))
            threading.Thread(target=broadcast, args=(text, (tu or {}).get("id"), day), daemon=True).start()
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
            urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ulgurji-keepalive"}), timeout=20).read()
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
    if db.kv_get("routes") is None and os.path.exists(os.path.join(seed, "routes.json")):
        with open(os.path.join(seed, "routes.json"), encoding="utf-8") as f:
            db.kv_set("routes", json.load(f))
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
