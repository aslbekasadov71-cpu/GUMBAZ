# Gumbaz Express — Telegram botlar + Mini App

| | Kim uchun | Nima qiladi |
|---|---|---|
| **Mijozlar boti** | Mijozlar | /start → telefon → joylashuv → Menyu (Mini App): buyurtma, savat, yetkazish, profil |
| **Admin bot** | Rahbar va xodimlar | Yangi buyurtmalar, holat tugmalari, **Admin panel** |

Faqat **Python 3.9+** — hech qanday kutubxona o'rnatilmaydi.

## Yetkazish
- **Gumbaz Express kuryeri** — narxni kuryer yetkazganda aytadi.
- **Yandex** — buyurtmada belgilanadi; narxni Yandex kuryeri yetib borganda aytadi.
- **Olib ketish**.

## Rahbar va xodimlar
- **Rahbar** — `ADMIN_IDS` dagi Telegram ID. Admin botga `/start` yozadi va panelni ochadi (barcha ruxsatlar).
  Kompyuterdan kirish: `admin.html` sahifasi, login `rahbar`, parol — `OWNER_PASSWORD`.
- **Xodimlar** — rahbar panelda **Boshqaruv → Xodimlar** bo'limida login va parol yaratadi va ruxsatlarni belgilaydi:
  Statistika · Buyurtmalar · Tugagan/sotuvda · Menyu tahriri · Bannerlar · Mijozlar · Xabar yuborish · Sozlamalar.
  Tayyor shablonlar: Kassir, Oshpaz, Menejer, Hammasi.
- Xodim admin botni ochib login/parol bilan kiradi. Shundan keyin "Buyurtmalar" ruxsati bo'lsa, yangi buyurtmalar unga ham keladi.
- Ruxsat server tomonida tekshiriladi: xodim o'ziga berilmagan amalni bajara olmaydi. Parol xeshlangan holda saqlanadi, 6 marta noto'g'ri urinishdan keyin 10 daqiqa bloklanadi.

## Render'ga o'rnatish (bepul tarif)
1. Render → New → Web Service → shu GitHub repozitoriyasi.
2. Runtime: **Python**, Build: `echo ok`, Start: `python3 bot.py`, Region: Frankfurt.
3. Environment:

| Kalit | Qiymat |
|---|---|
| `CLIENT_BOT_TOKEN` | mijozlar boti tokeni |
| `ADMIN_BOT_TOKEN` | admin bot tokeni |
| `ADMIN_IDS` | rahbarning Telegram ID si (bir nechta bo'lsa vergul bilan) |
| `OWNER_PASSWORD` | rahbarning brauzerdan kirish paroli |

`WEBAPP_URL` Render manzilidan avtomatik olinadi. Render'da bot **webhook** rejimida ishlaydi: server uxlab qolsa ham Telegram xabari uni uyg'otadi (birinchi javob 30–60 soniya kechikishi mumkin).

### Ma'lumotlar qayerda saqlanadi
Bepul tarifda disk har qayta ishga tushganda tozalanadi. Shuning uchun baza, menyu va rasmlar avtomatik ravishda rahbarning **admin bot chatidagi qadalgan "💾 Gumbaz zaxira" fayliga** yoziladi (har o'zgarishdan ~15 soniya keyin va server to'xtashidan oldin). Server qayta yonganda shu fayldan tiklanadi.
- Bu xabarni **o'chirmang va qadalgan holda qoldiring**.
- Rahbar admin botga kamida bir marta `/start` yozgan bo'lishi shart.
- Banner videolari (2 MB dan katta) alohida fayl sifatida shu chatga saqlanadi; video hajmi 19 MB gacha.
- Kafolatli va tezkor ishlash uchun Render **Starter** tarifi ($7/oy) tavsiya qilinadi — shunda server uxlamaydi.

## O'z serveringizda (VPS)
```bash
cp config.example.json config.json   # tokenlar, ADMIN_IDS, WEBAPP_URL (https)
python3 bot.py                         # polling rejimi, ma'lumot data/ papkasida
```
HTTPS uchun Caddy: `gumbaz.uz { reverse_proxy 127.0.0.1:8080 }`. systemd: `deploy/gumbaz.service`.

## Fayllar
```
bot.py               ikkala bot + web server + API + zaxira
config.example.json  sozlamalar namunasi (Render'da environment orqali)
data/menu.json       boshlang'ich menyu (keyin paneldan o'zgaradi)
data/banners.json    boshlang'ich bannerlar
webapp/index.html    mijozlar Mini App'i
webapp/admin.html    admin panel
src/, tools/build.py Mini App manba kodi va yig'uvchi
```
