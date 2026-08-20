"""
EchoDub Engine - Context-Aware Technical & Educational Translator
Translates English video transcriptions into natural Persian matching Iranian tech community idioms.
"""

import json
import logging
import asyncio
from typing import List, Dict, Any
from google import genai
from google.genai import types

from config import settings

logger = logging.getLogger("EchoDub.Translator")

TECH_GLOSSARY_INSTRUCTIONS = """
شما یک مدرس ارشد، باحوصله و خوش‌بیان برنامه‌نویسی و فناوری در ایران هستید که در حال دوبله فارسی یک ویدیوی آموزشی تخصصی می‌باشید.
هدف شما ترجمه دیالوگ‌های انگلیسی به فارسی بسیار روان، گرم، خوش‌آوا و قابل فهم (محاوره‌ای محترمانه آموزشی) برای برنامه‌نویسان ایرانی است.

قوانین حیاتی برای تلفظ صحیح و لحن آرام:
۱. روان‌خوانی و ویرگول‌گذاری برای تنفس طبیعی گوینده:
   - حتماً در بین جملات و بعد از عبارات مقدماتی از ویرگول فارسی («،») استفاده کنید تا صدای هوش مصنوعی با آرامش، مکث مناسب و بدون عجله کلمات را ادا کند.
   - از ساختن جملات طولانی و سنگین پرهیز کنید؛ جملات باید کوتاه، دلنشین و شمرده باشند.

۲. اصطلاحات فنی به سبک توسعه‌دهندگان ایرانی با نگارش صوتی تمیز:
   - کلمات را به شکلی بنویسید که موتور صوتی هیچ‌گونه خطای تلفظی نداشته باشد:
     - Function -> تابع یا فانکشن
     - Async / Asynchronous -> اِی‌سینک یا ناهمگام
     - API -> اِی‌پی‌آی
     - Python -> پایتون
     - User / Users -> یوزِر / یوزِرها یا کاربر
     - Endpoint -> اندپوینت
     - Frontend / Backend -> فرانت‌اند / بک‌اند
     - Database -> دیتابیس یا پایگاه‌داده
     - Response / Request -> ریسپانس / ریکوئست

۳. تناسب زمانی و روانی:
   - طول جمله فارسی باید دقیقاً متناسب با ثانیه‌های داده شده باشد تا گوینده با آرامش و ریتم طبیعی صحبت کند و نیازی به تند صحبت کردن نباشد.
   - لحن باید مانند یک استاد صمیمی و مسلط باشد (مثال: «سلام به همراهان عزیز»، «توی این جلسه»، «همان‌طور که می‌بینید»).

۴. فرمت خروجی:
   - خروجی فقط و فقط باید یک JSON معتبر شامل فیلدهای "id", "start", "end", "original_text", "translated_text" باشد.
"""

class TechTranslator:
    def __init__(self):
        if settings.GEMINI_API_KEY:
            self.client = genai.Client(api_key=settings.GEMINI_API_KEY)
        else:
            self.client = None
            logger.warning("GEMINI_API_KEY is not set. Translation will operate in fallback mock mode.")

    async def translate_segments(self, segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Translates a list of timestamped segments in optimized batches using Gemini 1.5 Flash.
        """
        if not self.client:
            logger.warning("No Gemini API client configured. Returning raw transcription.")
            for s in segments:
                s["translated_text"] = s["text"]
            return segments

        # Process in batches of 20 segments to avoid token limit while preserving context
        batch_size = 20
        all_translated_segments = []

        for i in range(0, len(segments), batch_size):
            batch = segments[i:i + batch_size]
            prompt_input = [
                {
                    "id": seg["id"],
                    "start": seg["start"],
                    "end": seg["end"],
                    "duration": seg["duration"],
                    "original_text": seg["text"]
                }
                for seg in batch
            ]

            logger.info(f"Translating batch {i // batch_size + 1}/{(len(segments) - 1) // batch_size + 1} ({len(batch)} segments)...")
            
            user_prompt = f"Translate the following timestamped dialogue segments into natural Persian educational dubbing script:\n\n{json.dumps(prompt_input, ensure_ascii=False, indent=2)}"

            loop = asyncio.get_event_loop()
            
            def _call_gemini():
                response = self.client.models.generate_content(
                    model=settings.GEMINI_MODEL,
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=TECH_GLOSSARY_INSTRUCTIONS,
                        response_mime_type="application/json",
                        temperature=0.3
                    )
                )
                return response.text

            try:
                raw_json = await loop.run_in_executor(None, _call_gemini)
                translated_batch = json.loads(raw_json)
                
                # Merge translated text back with original timing data
                for orig, trans in zip(batch, translated_batch):
                    orig["translated_text"] = trans.get("translated_text", orig["text"])
                    all_translated_segments.append(orig)
            except Exception as e:
                logger.error(f"Error in Gemini translation batch: {e}. Fallback to direct text.")
                for orig in batch:
                    orig["translated_text"] = orig["text"]
                    all_translated_segments.append(orig)

        logger.info(f"All {len(all_translated_segments)} segments successfully translated into natural Persian.")
        return all_translated_segments
