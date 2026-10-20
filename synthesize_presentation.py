import os
import sys
import json
import time
import base64
import urllib.request
import urllib.error
import subprocess
import shutil
from pathlib import Path

# Fix Windows console encoding for UTF-8 Persian characters
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Setup paths
OUT_DIR = Path("storage/presentations/issb")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# 1. Fetch Gemini API Key from Coolify
TOKEN = '8|oEmMMOGiSErWGMFtBnPLJcrOeDWLrGAT2qqGEqVG3dc75be2'
APP_UUID = '75glmxpk5jxiudgaa1jzsny9'
BASE_URL = 'http://209.145.63.253:8000/api/v1'

print("Fetching active Gemini API key from Coolify...")
req = urllib.request.Request(f'{BASE_URL}/applications/{APP_UUID}/envs', headers={'Authorization': f'Bearer {TOKEN}'})
with urllib.request.urlopen(req) as resp:
    envs = json.loads(resp.read().decode())

GEMINI_API_KEY = next((e['value'] for e in envs if e.get('key') == 'GEMINI_API_KEY'), None)
if not GEMINI_API_KEY:
    raise RuntimeError("Could not find GEMINI_API_KEY in environment!")

print(f"Gemini API key loaded (length: {len(GEMINI_API_KEY)}).")

SLIDES_DATA = [
    {
        "slide_num": 1,
        "title": "شروع ارائه و معرفی موضوع",
        "text": """سلام و عرض ادب خدمت استاد محترم و دوستان عزیز.
موضوع ارائه امروز من، استانداردهای بین‌المللی گزارشگری پایداری یا ISSB هست.
قبل از شروع، می‌خوام با یه مثال بانکی بحث رو باز کنم.
فرض کنید بانکی به یک شرکت صنعتی تسهیلات پرداخت کرده. صورت‌های مالی اون شرکت در حال حاضر وضعیت مناسبی داره، اما فعالیت شرکت به‌شدت وابسته به منابع آب و انرژیه.
حالا اگر در آینده با خشکسالی یا محدودیت‌های زیست‌محیطی مواجه بشه، ممکنه توان بازپرداخت تسهیلاتش کاهش پیدا کنه.
سؤال اینجاست: آیا صورت‌های مالی سنتی به‌تنهایی می‌تونن تمام این ریسک‌ها رو به سرمایه‌گذاران و اعتباردهندگان نشون بدن؟
اینجاست که اهمیت گزارشگری پایداری مشخص میشه.
در این ارائه بررسی می‌کنیم ISSB چرا ایجاد شده، دو استاندارد اصلی اون یعنی IFRS S1 و IFRS S2 چه الزاماتی دارن و چه کاربردی در صنعت بانکداری پیدا می‌کنن."""
    },
    {
        "slide_num": 2,
        "title": "تاریخچه و دلایل تأسیس ISSB",
        "text": """برای اینکه متوجه بشیم چرا ISSB شکل گرفته، باید کمی به گذشته برگردیم.
تا قبل از تأسیس ISSB، سازمان‌ها برای گزارشگری پایداری از چارچوب‌های متفاوتی مثل GRI، SASB و TCFD استفاده می‌کردن.
هرکدوم از این چارچوب‌ها اهداف و ویژگی‌های خودشون رو داشتن، اما یک مشکل اساسی وجود داشت: مقایسه اطلاعات پایداری شرکت‌ها برای سرمایه‌گذاران دشوار بود.
به همین دلیل، بنیاد IFRS در نوامبر سال ۲۰۲۱، همزمان با اجلاس اقلیمی گلاسکو، هیئت استانداردهای بین‌المللی پایداری یا ISSB رو تأسیس کرد.
هدف اصلی این هیئت، ایجاد یک مبنای جهانی برای افشای اطلاعات مالی مرتبط با پایداری بود.
نکته مهم اینه که ISSB بیشتر از زاویه دید سرمایه‌گذار به پایداری نگاه می‌کنه؛ یعنی بررسی می‌کنه مسائل محیط‌زیستی، اجتماعی و سایر موضوعات پایداری چطور می‌تونن بر آینده مالی یک شرکت تأثیر بذارن."""
    },
    {
        "slide_num": 3,
        "title": "چارچوب‌های تشکیل‌دهنده ISSB",
        "text": """ISSB فعالیت خودش رو از صفر شروع نکرد؛ بلکه از تجربیات چند چارچوب معتبر بین‌المللی استفاده کرد.
اولین مورد، TCFD هست که بر افشای ریسک‌ها و فرصت‌های مرتبط با تغییرات اقلیمی تمرکز داشت.
دومین مورد SASB هست که استانداردهای تخصصی برای صنایع مختلف ارائه می‌کنه.
برای مثال، موضوعات مهم گزارشگری پایداری در یک بانک با یک شرکت فولادسازی یکسان نیست. بنابراین، به شاخص‌های متفاوتی نیاز دارن.
سومین مورد هم چارچوب گزارشگری یکپارچه هست که تلاش می‌کنه اطلاعات مالی و غیرمالی رو به هم مرتبط کنه و نشون بده سازمان چطور در طول زمان ارزش ایجاد می‌کنه.
در واقع ISSB از این تجربیات استفاده کرده تا اطلاعات پایداری، ساختارمندتر، مرتبط‌تر و قابل‌مقایسه‌تر بشن."""
    },
    {
        "slide_num": 4,
        "title": "استاندارد IFRS S1",
        "text": """حالا وارد اولین استاندارد اصلی ISSB می‌شیم که IFRS S1 نام داره.
این استاندارد در ژوئن سال ۲۰۲۳ منتشر شد و الزامات عمومی افشای اطلاعات مالی مرتبط با پایداری رو مشخص می‌کنه.
مفهوم اصلی S1 اینه که شرکت‌ها باید ریسک‌ها و فرصت‌های مرتبط با پایداری رو که انتظار می‌ره بر جریان‌های نقدی، دسترسی به منابع مالی یا هزینه سرمایه اون‌ها در کوتاه‌مدت، میان‌مدت یا بلندمدت اثر بذارن، شناسایی و افشا کنن.
یکی از مهم‌ترین مفاهیم این استاندارد، اهمیت مالی یا Materiality هست.
یعنی لازم نیست هر اطلاعاتی درباره پایداری گزارش بشه؛ بلکه اطلاعاتی اهمیت دارن که حذف یا ارائه نادرست اون‌ها بتونه بر تصمیم سرمایه‌گذاران و اعتباردهندگان تأثیر بذاره.
برای مثال، اگر بانکی به شرکت‌های فعال در مناطق کم‌آب تسهیلات زیادی داده باشه، ریسک خشکسالی می‌تونه به افزایش احتمال نکول مشتریان منجر بشه.
بنابراین این ریسک فقط یک مسئله محیط‌زیستی نیست، بلکه می‌تونه یک ریسک مالی بااهمیت برای بانک هم باشه."""
    },
    {
        "slide_num": 5,
        "title": "چهار محور اصلی افشا",
        "text": """یکی از مهم‌ترین بخش‌های استانداردهای ISSB، ساختار چهارگانه افشای اطلاعاته.
این ساختار از توصیه‌های TCFD الهام گرفته و شامل چهار محور اصلیه.
اول، حاکمیت شرکتی؛ یعنی هیئت‌مدیره و مدیران چطور بر ریسک‌ها و فرصت‌های پایداری نظارت می‌کنن.
دوم، راهبرد؛ یعنی این ریسک‌ها چه تأثیری بر مدل کسب‌وکار، تصمیمات مدیریتی و آینده مالی سازمان دارن.
سوم، مدیریت ریسک؛ یعنی سازمان چطور این ریسک‌ها رو شناسایی، ارزیابی و کنترل می‌کنه.
و چهارم، شاخص‌ها و اهداف؛ یعنی عملکرد سازمان با چه معیارهایی اندازه‌گیری میشه.
برای مثال، یک بانک ممکنه در محور راهبرد تصمیم بگیره سهم تأمین مالی صنایع پرکربن رو مدیریت کنه، در محور مدیریت ریسک، آسیب‌پذیری مشتریانش رو بررسی کنه و در محور شاخص‌ها، میزان انتشارهای تأمین مالی‌شده رو گزارش بده.
پس این چهار محور در کنار هم مشخص می‌کنن که سازمان صرفاً درباره پایداری صحبت می‌کنه یا برای اون ساختار مدیریتی و معیار سنجش هم داره."""
    },
    {
        "slide_num": 6,
        "title": "استاندارد IFRS S2",
        "text": """استاندارد دوم، IFRS S2 هست که به‌طور اختصاصی بر افشای ریسک‌ها و فرصت‌های مرتبط با اقلیم تمرکز داره.
در این استاندارد، ریسک‌های اقلیمی رو می‌تونیم به دو گروه اصلی تقسیم کنیم.
گروه اول، ریسک‌های فیزیکی هستن؛ مثل سیل، خشکسالی یا افزایش دما که ممکنه به دارایی‌ها و فعالیت اقتصادی آسیب وارد کنن.
گروه دوم، ریسک‌های گذار هستن؛ یعنی ریسک‌هایی که در مسیر حرکت اقتصاد به سمت فعالیت‌های کم‌کربن ایجاد میشن، مثل مقررات جدید انتشار کربن یا تغییر فناوری.
موضوع مهم بعدی، انتشار گازهای گلخانه‌ای در سه دامنه است.
دامنه یک، انتشار مستقیم خود سازمانه.
دامنه دو، انتشار غیرمستقیم ناشی از انرژی خریداری‌شده، مثل برق مصرفی شعب بانکه.
و دامنه سه، سایر انتشارهای غیرمستقیم در زنجیره ارزش رو شامل میشه.
برای بانک‌ها، یکی از مهم‌ترین بخش‌های دامنه سه، انتشارهای مرتبط با تسهیلات و سرمایه‌گذاری‌هاست که بهش Financed Emissions گفته میشه.
S2 همچنین از سازمان می‌خواد تاب‌آوری راهبرد خودش رو در سناریوهای مختلف اقلیمی بررسی کنه."""
    },
    {
        "slide_num": 7,
        "title": "کاربرد ISSB در صنعت بانکداری",
        "text": """به نظر من این اسلاید یکی از مهم‌ترین بخش‌های بحث ماست؛ چون استانداردهای ISSB رو مستقیماً به صنعت بانکداری ارتباط میده.
وقتی از پایداری یک بانک صحبت می‌کنیم، نباید فقط به مصرف برق شعب، میزان استفاده از کاغذ یا ساختمان‌های بانک توجه کنیم.
بخش مهمی از اثرگذاری بانک از طریق تصمیمات اعتباری و سرمایه‌گذاری اون اتفاق می‌افته.
اجازه بدید یک مثال بزنم.
فرض کنید بانکی به یک کارخانه سیمان تسهیلات بلندمدت پرداخت کرده.
اگر در آینده هزینه انرژی یا الزامات مرتبط با انتشار کربن افزایش پیدا کنه، ممکنه سودآوری اون کارخانه کاهش پیدا کنه و در نتیجه، ریسک اعتباری بانک بالا بره.
اینجاست که ریسک اقلیمی می‌تونه به ریسک اعتباری تبدیل بشه.
از طرف دیگه، تأمین مالی پروژه‌های انرژی تجدیدپذیر هم می‌تونه فرصت‌های اقتصادی جدیدی برای بانک ایجاد کنه.
بنابراین کاربرد ISSB در بانکداری صرفاً گزارش‌دادن درباره محیط‌زیست نیست؛ بلکه می‌تونه به شناخت بهتر ریسک پرتفوی تسهیلات، تصمیم‌گیری اعتباری و شفافیت مالی کمک کنه."""
    },
    {
        "slide_num": 8,
        "title": "تفاوت ISSB با GRI و ESRS",
        "text": """در این بخش می‌خوایم یکی از تفاوت‌های مهم استانداردهای گزارشگری پایداری رو بررسی کنیم.
تفاوت اصلی ISSB و GRI در زاویه نگاه اون‌هاست.
در ISSB، تمرکز بر اهمیت مالیه؛ یعنی مسائل پایداری چطور بر وضعیت و چشم‌انداز مالی شرکت تأثیر می‌ذارن.
اما GRI بیشتر بر آثار فعالیت شرکت بر محیط‌زیست، اقتصاد و مردم تمرکز داره.
برای روشن‌شدن تفاوت، فرض کنید یک کارخانه باعث آلودگی منابع آب اطراف خودش شده.
در رویکرد GRI، اثر این آلودگی بر محیط‌زیست و جامعه محلی اهمیت داره.
اما در ISSB، بررسی می‌کنیم که این موضوع چطور ممکنه از طریق جریمه‌های قانونی، توقف تولید یا افزایش هزینه‌ها بر وضعیت مالی شرکت اثر بذاره.
در استانداردهای اروپایی ESRS، مفهومی به نام اهمیت دوگانه یا Double Materiality مطرح میشه که هر دو جهت رو در نظر می‌گیره.
پس این چارچوب‌ها لزوماً رقیب هم نیستن؛ بلکه می‌تونن مکمل یکدیگر باشن."""
    },
    {
        "slide_num": 9,
        "title": "پذیرش جهانی و تحولات جدید",
        "text": """پس از انتشار IFRS S1 و IFRS S2 در سال ۲۰۲۳، کشورهای مختلف شروع به بررسی، پذیرش یا تدوین مقررات مبتنی بر این استانداردها کردن.
طبق گزارش‌های بنیاد IFRS در سال ۲۰۲۶، بیش از ۴۰ حوزه قضایی تصمیم به استفاده از این استانداردها گرفتن یا در حال فراهم‌کردن مقدمات ورود اون‌ها به چارچوب‌های قانونی و نظارتی خودشون هستن.
این موضوع اهمیت ISSB رو در گزارشگری مالی بین‌المللی نشون میده.
البته باید توجه کنیم انتشار استاندارد به معنای الزام خودکار اون در همه کشورها نیست.
ISSB برای آسان‌ترشدن اجرای استانداردها، تسهیلات انتقالی هم پیش‌بینی کرده؛ از جمله امکان تمرکز بر اطلاعات اقلیمی در سال اول و معافیت موقت از برخی الزامات دامنه سه.
نکته جدیدتر هم اصلاحات منتشرشده در دسامبر ۲۰۲۵ درباره الزامات افشای انتشار گازهای گلخانه‌ایه که از ابتدای سال ۲۰۲۷ لازم‌الاجرا میشن، مگر اینکه زودتر اعمال بشن."""
    },
    {
        "slide_num": 10,
        "title": "جمع‌بندی و پایان",
        "text": """در پایان اگر بخوام ارائه رو در سه نکته خلاصه کنم:
نکته اول اینکه ISSB تلاش می‌کنه یک مبنای جهانی و قابل‌مقایسه برای افشای اطلاعات مالی مرتبط با پایداری ایجاد کنه.
نکته دوم اینکه IFRS S1 الزامات عمومی گزارشگری پایداری رو مشخص می‌کنه و IFRS S2 به‌طور ویژه به ریسک‌ها و فرصت‌های اقلیمی می‌پردازه.
و نکته سوم، که در صنعت بانکداری بسیار مهمه، اینه که ریسک‌های پایداری فقط به فعالیت‌های داخلی بانک محدود نمیشن؛ بلکه از طریق تسهیلات و سرمایه‌گذاری‌ها هم می‌تونن بر وضعیت مالی بانک تأثیر بذارن.
در واقع پیام اصلی این استانداردها اینه که پایداری صرفاً یک موضوع محیط‌زیستی یا مسئولیت اجتماعی نیست؛ بلکه می‌تونه بخشی از مدیریت ریسک، گزارشگری مالی و تصمیم‌گیری اقتصادی باشه.
از استاد محترم و دوستان عزیز بابت توجهشون سپاسگزارم. خوشحال می‌شم اگر پرسشی وجود داره پاسخ بدم."""
    }
]


def pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 24000, channels: int = 1, bit_depth: int = 16) -> bytes:
    import struct
    byte_rate = sample_rate * channels * (bit_depth // 8)
    block_align = channels * (bit_depth // 8)
    data_size = len(pcm_bytes)
    chunk_size = 36 + data_size
    header = bytearray()
    header.extend(b'RIFF')
    header.extend(struct.pack('<I', chunk_size))
    header.extend(b'WAVE')
    header.extend(b'fmt ')
    header.extend(struct.pack('<I', 16))
    header.extend(struct.pack('<H', 1))
    header.extend(struct.pack('<H', channels))
    header.extend(struct.pack('<I', sample_rate))
    header.extend(struct.pack('<I', byte_rate))
    header.extend(struct.pack('<H', block_align))
    header.extend(struct.pack('<H', bit_depth))
    header.extend(b'data')
    header.extend(struct.pack('<I', data_size))
    return bytes(header) + pcm_bytes


def clean_gemini_pcm(pcm_bytes: bytes, sample_rate: int = 24000) -> bytes:
    import struct
    bytes_per_sample = 2
    if len(pcm_bytes) < sample_rate * bytes_per_sample * 0.2:
        return pcm_bytes
    
    start_skip = int(sample_rate * 0.015) * bytes_per_sample
    pcm = pcm_bytes[start_skip:]

    num_samples = len(pcm) // 2
    samples = struct.unpack(f'<{num_samples}h', pcm[:num_samples * 2])
    
    # Simple fade-in / fade-out
    fade_len = int(sample_rate * 0.02)
    s_list = list(samples)
    for i in range(min(fade_len, len(s_list))):
        s_list[i] = int(s_list[i] * (i / fade_len))
    for i in range(min(fade_len, len(s_list))):
        idx = len(s_list) - 1 - i
        s_list[idx] = int(s_list[idx] * (i / fade_len))

    return struct.pack(f'<{len(s_list)}h', *s_list)


def synthesize_text_gemini(text: str, voice_name: str = "Puck") -> bytes:
    models = ["gemini-2.5-flash-preview-tts", "gemini-3.1-flash-tts-preview", "gemini-2.5-pro-preview-tts"]
    payload_dict = {
        "contents": [{"parts": [{"text": text.strip()}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice_name}
                }
            }
        }
    }
    payload = json.dumps(payload_dict).encode('utf-8')

    for attempt in range(6):
        for model in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                    for p in parts:
                        if "inlineData" in p:
                            return base64.b64decode(p["inlineData"]["data"])
            except urllib.error.HTTPError as e:
                err_msg = e.read().decode('utf-8', errors='ignore')
                print(f"Model {model} returned HTTP {e.code}: {err_msg[:120]}")
                continue
            except Exception as e:
                print(f"Error calling {model}: {e}")
                continue
        
        print(f"All models returned 429 quota in attempt {attempt+1}/6. Waiting 12 seconds...")
        time.sleep(12)

    raise RuntimeError(f"Failed to synthesize text chunk ({len(text)} chars) after 6 attempts")


def chunk_text(text: str, max_chars: int = 400) -> list:
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks = []
    current_chunk = []
    current_len = 0

    for p in paragraphs:
        if current_len + len(p) > max_chars and current_chunk:
            chunks.append("\n".join(current_chunk))
            current_chunk = [p]
            current_len = len(p)
        else:
            current_chunk.append(p)
            current_len += len(p) + 1

    if current_chunk:
        chunks.append("\n".join(current_chunk))
    return chunks


def generate_all_slides():
    print("\n=======================================================")
    print(" شروع فرآیند دوبله رسمی ۱۰ اسلاید ارائه با صدای Google Gemini Puck ")
    print("=======================================================\n")

    slide_audio_files = []

    for s in SLIDES_DATA:
        s_num = s["slide_num"]
        s_title = s["title"]
        s_text = s["text"]

        slide_wav = OUT_DIR / f"slide_{s_num:02d}.wav"
        slide_mp3 = OUT_DIR / f"slide_{s_num:02d}.mp3"

        if slide_mp3.exists() and slide_mp3.stat().st_size > 10000:
            print(f"✓ اسلاید {s_num}: {s_title} قبلاً تولید شده است ({slide_mp3.name}).")
            slide_audio_files.append(slide_mp3)
            continue

        print(f"\n🎙️ در حال سنتز اسلاید {s_num}: {s_title}...")
        chunks = chunk_text(s_text, max_chars=450)
        print(f"   تعداد بخش‌های متنی (Chunks): {len(chunks)}")

        slide_pcm = bytearray()
        for i, ch in enumerate(chunks, 1):
            print(f"   - بخش {i}/{len(chunks)} ({len(ch)} کاراکتر)...", end="", flush=True)
            pcm_chunk = synthesize_text_gemini(ch, voice_name="Puck")
            pcm_chunk = clean_gemini_pcm(pcm_chunk)
            slide_pcm.extend(pcm_chunk)
            print(" ✓ دریافت شد.")
            time.sleep(0.5)

        # Write WAV file
        slide_wav = OUT_DIR / f"slide_{s_num:02d}.wav"
        slide_mp3 = OUT_DIR / f"slide_{s_num:02d}.mp3"

        wav_bytes = pcm_to_wav(bytes(slide_pcm), sample_rate=24000)
        with open(slide_wav, "wb") as f:
            f.write(wav_bytes)

        # Convert to high quality MP3 with ffmpeg mastering
        cmd = [
            "ffmpeg", "-y",
            "-i", str(slide_wav),
            "-af", "highpass=f=70,equalizer=f=220:t=q:w=1.2:g=1.5,loudnorm=I=-16:TP=-1.5",
            "-b:a", "192k",
            str(slide_mp3)
        ]
        subprocess.run(cmd, capture_output=True, check=True)

        size_kb = slide_mp3.stat().st_size / 1024
        print(f"   🌟 فایل اسلاید {s_num} آماده شد: {slide_mp3.name} ({size_kb:.1f} KB)")
        slide_audio_files.append(slide_mp3)

    # 4. Generate Full Presentation Concatenation with 1.5s Pause
    print("\n=======================================================")
    print(" در حال تجمیع تمام اسلایدها در فایل یکپارچه نهایی... ")
    print("=======================================================")

    # Create 1.5s silence file
    silence_mp3 = OUT_DIR / "silence_1500ms.mp3"
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "anullsrc=r=24000:cl=mono",
        "-t", "1.5",
        "-b:a", "192k",
        str(silence_mp3)
    ], capture_output=True, check=True)

    concat_list = OUT_DIR / "concat_list.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        for i, f_path in enumerate(slide_audio_files):
            f.write(f"file '{f_path.resolve()}'\n")
            if i < len(slide_audio_files) - 1:
                f.write(f"file '{silence_mp3.resolve()}'\n")

    full_mp3 = OUT_DIR / "issb_full_presentation.mp3"
    full_wav = OUT_DIR / "issb_full_presentation.wav"

    subprocess.run([
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        str(full_mp3)
    ], capture_output=True, check=True)

    subprocess.run([
        "ffmpeg", "-y",
        "-i", str(full_mp3),
        "-ar", "24000",
        "-ac", "1",
        str(full_wav)
    ], capture_output=True, check=True)

    print("\n🎉 تمام فایل‌های صوتی با کیفیت استودیویی با موفقیت تولید شدند:")
    print(f"   - فایل صوتی کامل: {full_mp3.resolve()} ({full_mp3.stat().st_size / (1024*1024):.2f} MB)")
    print(f"   - فایل WAV بدون افت کیفیت: {full_wav.resolve()} ({full_wav.stat().st_size / (1024*1024):.2f} MB)")
    print(f"   - فایل‌های مجزای ۱۰ اسلاید در پوشه: {OUT_DIR.resolve()}")

if __name__ == "__main__":
    generate_all_slides()
