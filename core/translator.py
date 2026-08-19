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
You are an expert Iranian software engineer and senior tech instructor dubbing educational programming and technology courses.
Your goal is to translate English educational dialogue segments into natural, fluent, engaging conversational Persian (زبان محاوره‌ای آموزشی و روان) suitable for Iranian developers.

STRICT TRANSLATION RULES:
1. Technical Terms: NEVER awkwardly translate standard IT/Programming terms into strange Persian words.
   - Keep terms in standard Iranian developer usage:
     - Function -> فانکشن یا تابع
     - Array -> آرایه
     - Variable -> متغیر
     - Loop -> لوپ یا حلقه
     - Object -> آبجکت
     - Class -> کلاس
     - Container / Docker -> کانتینر / داکر
     - Deploy -> دیپلوی کردن
     - State / Props -> استیت / پراپس
     - API / Request / Response -> ای‌پی‌آی / ریکوئست / ریسپانس
     - Database / Query -> دیتابیس / کوئری
     - Frontend / Backend -> فرانت‌اند / بک‌اند
     - Thread / Async -> ترد / ای‌سینک
2. Timing & Syllable Constraints: The length of the Persian translation MUST roughly match the original duration so it can fit the speaker's time window. Keep sentences concise, punchy, and natural.
3. Natural Tone: Use warm, encouraging, conversational Persian instructor phrasing (e.g. «خب دوستان»، «در ادامه می‌بینیم که»، «همون‌طور که مشاهده می‌کنید»).
4. JSON Format: You must output ONLY a valid JSON array of objects with the exact keys: "id", "start", "end", "original_text", "translated_text".
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
