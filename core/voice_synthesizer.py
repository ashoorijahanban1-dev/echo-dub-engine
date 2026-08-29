"""
EchoDub Engine - High-Fidelity Persian Voice Synthesizer
Uses Microsoft Edge Neural Voices (Farid / Dilara) with async segment generation and exact timing sync.
"""

import os
import asyncio
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
import edge_tts
from pydub import AudioSegment

from config import settings

logger = logging.getLogger("EchoDub.VoiceSynthesizer")

class VoiceSynthesizer:
    @staticmethod
    async def synthesize_segment(text: str, voice: str, output_path: Path, rate: str = "+0%", pitch: str = "+0Hz", semaphore: Optional[asyncio.Semaphore] = None) -> Optional[Path]:
        """
        Synthesizes a single segment of Persian text into MP3 using Edge-TTS with retry & semaphore control.
        """
        clean_text = text.strip() if text else ""
        if not clean_text or not any(c.isalnum() for c in clean_text):
            logger.debug(f"Skipping empty or whitespace text segment: '{text}'")
            return None

        async def _do_synth():
            communicate = edge_tts.Communicate(
                text=clean_text,
                voice=voice,
                rate=rate,
                pitch=pitch
            )
            await communicate.save(str(output_path))
            return output_path

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                if semaphore:
                    async with semaphore:
                        return await _do_synth()
                else:
                    return await _do_synth()
            except Exception as e:
                logger.warning(f"Edge-TTS segment attempt {attempt}/{max_attempts} failed for '{clean_text[:30]}...': {e}")
                if attempt == max_attempts:
                    logger.error(f"Failed to synthesize segment after {max_attempts} attempts: '{clean_text[:40]}'")
                    return None
                await asyncio.sleep(1.0 * attempt)
        return None

    @classmethod
    async def generate_full_dubbed_audio(
        cls,
        segments: List[Dict[str, Any]],
        total_duration: float,
        output_wav_path: Path,
        temp_dir: Path,
        voice_gender: str = "male"
    ) -> Path:
        """
        Synthesizes all translated segments and places them on an exact timeline matching original video timestamps.
        """
        os.makedirs(temp_dir, exist_ok=True)
        os.makedirs(output_wav_path.parent, exist_ok=True)
        
        voice_name = settings.DEFAULT_PERSIAN_VOICE_MALE if voice_gender == "male" else settings.DEFAULT_PERSIAN_VOICE_FEMALE
        logger.info(f"Synthesizing {len(segments)} segments using voice: {voice_name}")

        # Create base silent audio track matching video total duration
        full_track = AudioSegment.silent(duration=int(total_duration * 1000) + 1500)

        # Control concurrency with a Semaphore (max 3 concurrent connections to avoid Microsoft rate limits)
        semaphore = asyncio.Semaphore(3)

        tasks = []
        for seg in segments:
            seg_file = temp_dir / f"seg_{seg['id']}.mp3"
            text_to_speak = seg.get("translated_text", seg.get("text", ""))
            tasks.append(cls.synthesize_segment(
                text=text_to_speak,
                voice=voice_name,
                output_path=seg_file,
                rate=settings.TTS_SPEECH_RATE,
                pitch=settings.TTS_SPEECH_PITCH,
                semaphore=semaphore
            ))

        logger.info("Executing rate-limited Edge-TTS audio synthesis batch...")
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("All segment audio files processed.")

        # Overlay segments onto full timeline naturally
        for seg in segments:
            seg_file = temp_dir / f"seg_{seg['id']}.mp3"
            if not seg_file.exists() or seg_file.stat().st_size == 0:
                continue

            try:
                seg_audio = AudioSegment.from_file(str(seg_file))
                start_ms = int(seg["start"] * 1000)
                full_track = full_track.overlay(seg_audio, position=start_ms)
            except Exception as audio_err:
                logger.warning(f"Failed to overlay segment {seg['id']}: {audio_err}")

        # Export mastered 48kHz WAV
        full_track = full_track.set_frame_rate(48000).set_channels(2)
        full_track.export(str(output_wav_path), format="wav")
        
        logger.info(f"Master dubbed voice track successfully assembled: {output_wav_path}")
        return output_wav_path
