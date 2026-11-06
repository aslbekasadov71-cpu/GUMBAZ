# Ulgurji savdo — distribyutor uchun Telegram botlar + Mini App

Agent har bir do'konga kirib buyurtma yig'ib yurmaydi — do'konchi o'zi telefonidan buyurtma beradi.

| | Kim uchun | Nima qiladi |
|---|---|---|
| **Do'konlar boti** | Do'konchilar | /start → telefon → do'kon nomi → joylashuv → **Katalog** (Mini App): savat, yetkazish kuni, naqd/nasiya, buyurtmalar, qayta buyurtma, hisob-kitob (qarz tarixi) |
| **Admin bot** | Rahbar (distribyutor) va xodimlar | Yangi buyurtmalar, holat tugmalari, `/yuk` — yuklash ro'yxati, `/stats`, **Admin panel** |

Faqat **Python 3.9+** — hech qanday kutubxona o'rnatilmaydi.

## Asosiy imkoniyatlar
- **Dona + qadoq**: har bir mahsulotning dona narxi va qadog'i (korobka, blok, yashik…): nechta dona, qadoq narxi (chegirmali bo'lishi mumkin), "faqat qadoqda" / "faqat donada" / "ikkalasi".
- **Yetkazish kunlari (Du–Sh)**: admin har bir kunga ko'cha/mahalla nomlarini yozadi (masalan, Bog'ishamol — Dushanba, Juma). Do'kon manzilida shu nom bo'lsa, kunlar avtomatik biriktiriladi; kerak bo'lsa do'konga qo'lda belgilanadi. Do'konchi faqat o'z kunlarining eng yaqin sanalarini tanlaydi.
- **Shtrix-kod**: admin panelda kamera bilan skanerlash — mahsulot nomi, grammi va rasmi Open Food Facts bazasidan avtomatik to'ldiriladi; katalogda bor bo'lsa, o'sha mahsulot ochiladi. USB/Bluetooth skaner ham ishlaydi (maydonga yozib Enter).
- **Yandex xaritasi** (ixtiyoriy): `YANDEX_API_KEY` berilsa, xarita Yandex'dan (uylar va uy raqamlari bilan) va manzil Yandex Geocoder orqali aniqlanadi. Kalit bo'lmasa — Mapbox (`MAPBOX_TOKEN`) yoki OpenStreetMap.
- **Nasiya (qarz daftari)**: buyurtma "Yetkazildi" qilinganda olingan pul kiritiladi, qolgani do'kon qarziga yoziladi. To'lov qabul qilish, qo'lda tuzatish (eski daftar qarzi), qarz chegarasi (umumiy yoki har bir do'konga alohida). Do'konchi o'z qarzini va tarixini ilovada ko'radi, har o'zgarishda botga xabar keladi.
- **Yuklash ro'yxati**: tanlangan kun (va hudud) uchun mashinaga nima ortish kerak (masalan, "5 korobka + 3 dona") va qaysi do'konlarga borish, naqd/nasiya summasi. Botga yuborish yoki nusxalash.
- **Do'konlar**: ro'yxat, hudud, chegara, ichki izoh, bloklash.

## Rahbar va xodimlar
- **Rahbar** — `ADMIN_IDS` dagi Telegram ID. Admin botga `/start` yozadi va panelni ochadi (barcha ruxsatlar).
  Kompyuterdan kirish: `admin.html` sahifasi, login `rahbar`, parol — `OWNER_PASSWORD`.
- **Xodimlar** — rahbar panelda **Boshqaruv → Xodimlar** bo'limida login va parol yaratadi va ruxsatlarni belgilaydi:
  Statistika · Buyurtmalar · Tugagan/sotuvda · Katalog tahriri · Bannerlar · Do'konlar · Qarz daftari · Xabar yuborish · Sozlamalar.
  Tayyor shablonlar: Haydovchi (buyurtmalar + to'lov qabul qilish), Operator, Menejer, Hammasi.
- Xodim admin botni ochib login/parol bilan kiradi. Shundan keyin "Buyurtmalar" ruxsati bo'lsa, yangi buyurtmalar unga ham keladi.
- Ruxsat server tomonida tekshiriladi: xodim o'ziga berilmagan amalni bajara olmaydi. Parol xeshlangan holda saqlanadi, 6 marta noto'g'ri urinishdan keyin 10 daqiqa bloklanadi.

## Render'ga o'rnatish (bepul tarif)
1. Render → New → Web Service → shu GitHub repozitoriyasi.
2. Runtime: **Python**, Build: `echo ok`, Start: `python3 bot.py`, Region: Frankfurt.
3. Environment:

| Kalit | Qiymat |
|---|---|
| `CLIENT_BOT_TOKEN` | do'konlar boti tokeni |
| `ADMIN_BOT_TOKEN` | admin bot tokeni |
| `ADMIN_IDS` | rahbarning Telegram ID si (bir nechta bo'lsa vergul bilan) |
| `OWNER_PASSWORD` | rahbarning brauzerdan kirish paroli |
| `YANDEX_API_KEY` | (ixtiyoriy) Yandex "JavaScript API va HTTP Geocoder" kaliti — xarita va aniq manzil |
| `MAPBOX_TOKEN` | (ixtiyoriy) Mapbox public token — Yandex kaliti bo'lmasa xarita uchun |

`WEBAPP_URL` Render manzilidan avtomatik olinadi. Render'da bot **webhook** rejimida ishlaydi: server uxlab qolsa ham Telegram xabari uni uyg'otadi (birinchi javob 30–60 soniya kechikishi mumkin).

### Ma'lumotlar qayerda saqlanadi
Bepul tarifda disk har qayta ishga tushganda tozalanadi. Shuning uchun baza, katalog va rasmlar avtomatik ravishda rahbarning **admin bot chatidagi qadalgan "💾 Zaxira" fayliga (`ulgurji_backup.zip`)** yoziladi (har o'zgarishdan ~15 soniya keyin va server to'xtashidan oldin). Server qayta yonganda shu fayldan tiklanadi.
- Bu xabarni **o'chirmang va qadalgan holda qoldiring**.
- Rahbar admin botga kamida bir marta `/start` yozgan bo'lishi shart.
- Banner videolari (2 MB dan katta) alohida fayl sifatida shu chatga saqlanadi; video hajmi 19 MB gacha.
- Kafolatli va tezkor ishlash uchun Render **Starter** tarifi ($7/oy) tavsiya qilinadi — shunda server uxlamaydi.

## O'z serveringizda (VPS)
```bash
cp config.example.json config.json   # tokenlar, ADMIN_IDS, WEBAPP_URL (https)
python3 bot.py                         # polling rejimi, ma'lumot data/ papkasida
```
HTTPS uchun Caddy: `ulgurji.uz { reverse_proxy 127.0.0.1:8080 }`. systemd: `deploy/gumbaz.service`.

## Fayllar
```
bot.py               ikkala bot + web server + API + zaxira
config.example.json  sozlamalar namunasi (Render'da environment orqali)
data/menu.json       boshlang'ich katalog (keyin paneldan o'zgaradi)
data/routes.json     boshlang'ich yetkazish kunlari (ko'chalar)
data/banners.json    boshlang'ich bannerlar
webapp/index.html    do'konchilar Mini App'i
webapp/admin.html    admin panel
src/, tools/build.py Mini App manba kodi va yig'uvchi
```
