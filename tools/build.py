#!/usr/bin/env python3
"""Mini app'larni yig'ish:
    src/client.html + src/base.css  ->  webapp/index.html  (mijozlar)
    src/admin.html  + src/base.css  ->  webapp/admin.html  (admin panel)
Solar ikonkalari SVG sprite sifatida ichiga joylanadi.

Ishlatish:  python3 tools/build.py /path/to/solar.json [--demo DEMO_DIR]
Solar to'plami: https://github.com/iconify/icon-sets/blob/master/json/solar.json
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ICONS = {  # ilova nomi -> Solar ikonka
    "home": "home-smile-bold-duotone", "cart": "cart-large-2-bold-duotone", "orders": "bill-list-bold-duotone",
    "user": "user-rounded-bold-duotone", "users": "users-group-rounded-bold-duotone", "search": "magnifer-linear",
    "delivery": "delivery-bold-duotone", "down": "alt-arrow-down-linear", "up": "alt-arrow-up-linear",
    "left": "alt-arrow-left-linear", "right": "alt-arrow-right-linear", "pin": "map-point-bold-duotone",
    "gps": "gps-bold-duotone", "shop": "shop-2-bold-duotone", "scooter": "scooter-bold-duotone",
    "check": "check-circle-bold", "refresh": "refresh-linear", "camera": "camera-bold", "globe": "global-bold-duotone",
    "phone": "phone-calling-rounded-bold-duotone", "chat": "chat-round-dots-bold-duotone", "doc": "document-text-bold-duotone",
    "clock": "clock-circle-bold-duotone", "logout": "logout-2-bold-duotone", "pen": "pen-new-square-linear",
    "fire": "fire-bold", "close": "close-circle-bold", "danger": "danger-triangle-bold-duotone", "info": "info-circle-bold-duotone",
    "play": "play-bold", "trash": "trash-bin-trash-bold-duotone", "minus": "minus-linear", "plus": "add-linear",
    "chart": "chart-2-bold-duotone", "box": "box-minimalistic-bold-duotone", "widget": "widget-5-bold-duotone",
    "chef": "chef-hat-bold-duotone", "flag": "flag-bold-duotone", "add": "add-circle-bold", "sort": "sort-vertical-linear",
    "gallery": "gallery-bold-duotone", "upload": "upload-minimalistic-bold", "list": "list-bold-duotone",
    "bell": "bell-bold-duotone", "settings": "settings-bold-duotone", "shield": "shield-check-bold-duotone",
    "crown": "crown-bold-duotone", "send": "plain-bold",
}
FALLBACK_BODY = {
    "add-linear": '<path fill="none" stroke="currentColor" stroke-linecap="round" stroke-width="2" d="M12 5v14m7-7H5"/>',
    "minus-linear": '<path fill="none" stroke="currentColor" stroke-linecap="round" stroke-width="2" d="M19 12H5"/>',
}


def sprite(solar_path, used):
    data = json.load(open(solar_path, encoding="utf-8"))
    icons, aliases = data["icons"], data.get("aliases", {})
    out = []
    for key in sorted(used):
        if key not in ICONS:
            raise SystemExit(f"ICONS ro'yxatida yo'q: {key}")
        name = ICONS[key]
        src = aliases[name]["parent"] if name in aliases and name not in icons else name
        body = icons[src]["body"] if src in icons else FALLBACK_BODY.get(name)
        if not body:
            raise SystemExit(f"Solar'da topilmadi: {name}")
        out.append(f'<symbol id="i-{key}" viewBox="0 0 24 24">{body}</symbol>')
    return '<svg width="0" height="0" style="position:absolute" aria-hidden="true">' + "".join(out) + "</svg>"


def used_icons(src):
    return set(re.findall(r'#i-([a-z]+)', src)) | set(re.findall(r'\bic\("([a-z]+)"', src)) | \
        set(re.findall(r'\[\s*"[^"]*",\s*"([a-z]+)"\s*\]', src)) | set(re.findall(r'\["[a-z_]+", "([a-z]+)"', src)) | \
        set(re.findall(r'\bic\(([a-z]+) ?\?', src))


def build(name, out, solar, data=None, demo=False):
    base = open(os.path.join(ROOT, "src", "base.css"), encoding="utf-8").read()
    src = open(os.path.join(ROOT, "src", name), encoding="utf-8").read().replace("/*__BASE__*/", base)
    missing = set(re.findall(r'#i-([a-z]+)', src)) - set(ICONS)
    if missing:
        raise SystemExit(f"ICONS ro'yxatida yo'q: {missing}")
    used = set(ICONS)  # hammasi (nomlar dinamik ham bo'ladi), ~15 KB
    src = src.replace("<!--__ICONS__-->", sprite(solar, used))
    if demo:
        src = src.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False))
        src = src.replace('<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">\n', "")
        html = src
    else:
        html = '<!doctype html>\n<html lang="uz">\n<head>\n<meta charset="utf-8">\n' + src + "\n</html>\n"
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"{out}: {len(html):,} bayt, {len(used)} ikonka")


def demo_data():
    menu = json.load(open(os.path.join(ROOT, "data", "menu.json"), encoding="utf-8"))
    banners = json.load(open(os.path.join(ROOT, "data", "banners.json"), encoding="utf-8"))
    cfg = json.load(open(os.path.join(ROOT, "config.example.json"), encoding="utf-8"))
    return {"categories": menu["categories"], "products": menu["products"], "banners": banners,
            "settings": {"restaurant": {"name": cfg["RESTAURANT_NAME"], "lat": cfg["RESTAURANT_LAT"], "lon": cfg["RESTAURANT_LON"],
                                        "address": {"uz": cfg["RESTAURANT_ADDRESS_UZ"], "ru": cfg["RESTAURANT_ADDRESS_RU"]}},
                         "support_phone": cfg["SUPPORT_PHONE"], "support_username": cfg["SUPPORT_USERNAME"], "work_hours": cfg["WORK_HOURS"],
                         "min_order": cfg["MIN_ORDER"], "accepting": True, "closed_msg": {"uz": cfg["CLOSED_MSG_UZ"], "ru": cfg["CLOSED_MSG_RU"]},
                         "yandex_tariff": "express", "yandex_price_api": False}}


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    solar = sys.argv[1]
    build("client.html", os.path.join(ROOT, "webapp", "index.html"), solar)
    build("admin.html", os.path.join(ROOT, "webapp", "admin.html"), solar)
    if "--demo" in sys.argv:
        d = sys.argv[sys.argv.index("--demo") + 1]
        os.makedirs(d, exist_ok=True)
        data = demo_data()
        build("client.html", os.path.join(d, "client.html"), solar, data, demo=True)
        build("admin.html", os.path.join(d, "admin.html"), solar, data, demo=True)


if __name__ == "__main__":
    main()
