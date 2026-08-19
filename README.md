# 🎙️ EchoDub AI Engine (موتور بک‌اند دوبله خودکار ویدیو و CDN تلگرام)

موتور ماژولار و مقیاس‌پذیر برای دانلود خودکار ویدیوهای آموزشی انگلیسی از **Downloadly.ir**، استخراج متن با Whisper، ترجمه تخصصی با هوش مصنوعی و دیکشنری اصطلاحات IT، سنتز صدای طبیعی فارسی با Edge-TTS، میکس و مسترینگ صدا با FFmpeg، و آپلود مستقیم به کانال تلگرام به عنوان CDN نامحدود.

---

## 🏗️ معماری ماژولار پروژه

```
echo-dub-engine/
├── config.py                # مدیریت متمرکز متغیرها و مسیرهای ذخیره‌سازی
├── pipeline.py              # ارکستریتور اصلی و هماهنگ‌کننده پایپ‌لاین
├── api.py                   # سرور FastAPI با وب‌سوکت زنده و Swagger Docs
├── core/
│   ├── downloader.py        # دانلودر پرسرعت چندبخشی aria2c
│   ├── transcriber.py       # استخراج صوت و Faster-Whisper بهینه برای CPU
│   ├── translator.py        # ترجمه کانتکست‌یار Gemini با دیکشنری اصطلاحات IT
│   ├── voice_synthesizer.py # سنتز صدای طبیعی فارسی با زمان‌بندی صدم ثانیه
│   ├── audio_mixer.py       # داکینگ و مسترینگ صدا با استاندارد EBU R128 (-14 LUFS)
│   ├── telegram_uploader.py # آپلودر پرسرعت MTProto به کانال تلگرام (تا ۲ گیگابایت)
│   └── cleaner.py           # پاک‌سازی خودکار فایل‌های موقت دیسک
├── Dockerfile               # داکرایز شده برای دیپلوی در Coolify
├── docker-compose.yml       # پیکربندی اجرای استک در Coolify
├── requirements.txt         # لیست پکیج‌های پایتون
└── .env.example             # نمونه متغیرهای محیطی
```

---

## 🚀 راهنمای راه‌اندازی سریع روی Coolify (سرور آمریکا)

### روش ۱: دیپلوی مستقیم از طریق Coolify Dashboard
1. وارد پنل Coolify روی سرور آمریکا شوید.
2. روی **+ New Resource** کلیک کرده و گزینه **GitHub / Git Repository** را انتخاب کنید (یا پوشه را آپلود کنید).
3. در بخش Environment Variables، متغیرهای فایل `.env.example` را پر کنید:
   - `GEMINI_API_KEY`: کلید رایگان از [Google AI Studio](https://aistudio.google.com).
   - `TELEGRAM_API_ID` و `TELEGRAM_API_HASH`: از [my.telegram.org](https://my.telegram.org).
   - `TELEGRAM_BOT_TOKEN`: از @BotFather.
   - `TELEGRAM_CHANNEL_ID`: آیدی کانال شما (مثلا `@my_cdn_channel`).
4. دکمه **Deploy** را بزنید! Coolify کانتینر را می‌سازد و پورت ۸۰۰۰ را باز می‌کند.

---

## 📡 نحوه ارسال ویدیو برای دوبله خودکار (API Usage)

### ۱. ارسال درخواست دوبله با cURL یا پایتون:
```bash
curl -X POST "http://YOUR_SERVER_IP:8000/api/v1/dub/submit" \
     -H "Content-Type: application/json" \
     -d '{
       "video_url": "https://downloadly.ir/sample-course/lesson-01.mp4",
       "title": "آموزش مقدماتی داکر و کانتینرها - جلسه اول",
       "voice_gender": "male",
       "preserve_bgm": true
     }'
```

**پاسخ دریافتی:**
```json
{
  "job_id": "job_1724089200_a1b2c3",
  "status": "QUEUED",
  "progress": 0,
  "current_stage": "Job queued in background worker..."
}
```

---

### ۲. استعلام وضعیت و دریافت لینک نهایی تلگرام:
```bash
curl -X GET "http://YOUR_SERVER_IP:8000/api/v1/dub/status/job_1724089200_a1b2c3"
```

**پاسخ پس از اتمام دوبله و آپلود در تلگرام:**
```json
{
  "job_id": "job_1724089200_a1b2c3",
  "status": "COMPLETED",
  "progress": 100,
  "current_stage": "Dubbing complete! Video ready on Telegram CDN.",
  "result": {
    "title": "آموزش مقدماتی داکر و کانتینرها - جلسه اول",
    "duration_seconds": 642.5,
    "elapsed_time_seconds": 45.2,
    "telegram": {
      "uploaded": true,
      "telegram_link": "https://t.me/my_cdn_channel/142",
      "message_id": 142
    }
  }
}
```

---

## 🛡️ ویژگی‌های کلیدی مهندسی‌شده

1. **دیکشنری اصطلاحات فنی IT**: کلماتی مثل `Function`، `Container`، `State` و `Deploy` با اصطلاحات رایج برنامه‌نویسان ایرانی ترجمه می‌شوند.
2. **پخش روان بدون قطعی در تلگرام**: ویدیوها با تگ `supports_streaming=True` و `-movflags +faststart` آپلود می‌شوند تا کاربر بدون نیاز به دانلود کامل، ویدیو را آنلاین ببیند.
3. **مسترینگ صدا (EBU R128)**: خروجی صدای فارسی استاندارد -14 LUFS دارد و صدای گوینده کاملاً شفاف و رسا است.
4. **حفظ دیسک سرور**: فایل‌های موقت صوتی و ویدیوهای میانی بلافاصله پس از آپلود حذف می‌شوند.
