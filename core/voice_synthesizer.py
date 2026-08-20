"""
EchoDub Engine - High-Fidelity Persian Voice Synthesizer
Uses Microsoft Edge Neural Voices (Farid / Dilara) with async segment generation and exact timing sync.
"""

import os
import asyncio
import logging
from pathlib import Path
from typing import List, Dict, Any
import edge_tts
from pydub import AudioSegment

from config import settings

logger = logging.getLogger("EchoDub.VoiceSynthesizer")

class VoiceSynthesizer:
    @staticmethod
    async def synthesize_segment(text: str, voice: str, output_path: Path, rate: str = "+0%", pitch: str = "+0Hz") -> Path:
        """
        Synthesizes a single segment of Persian text into MP3 using Edge-TTS.
        """
        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=rate,
            pitch=pitch
        )
        await communicate.save(str(output_path))
        return output_path

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
        full_track = AudioSegment.silent(duration=int(total_duration * 1000) + 1000)

        # Generate audio clips in parallel with calm rate
        tasks = []
        for seg in segments:
            seg_file = temp_dir / f"seg_{seg['id']}.mp3"
            text_to_speak = seg.get("translated_text", seg.get("text", ""))
            tasks.append(cls.synthesize_segment(
                text=text_to_speak,
                voice=voice_name,
                output_path=seg_file,
                rate=settings.TTS_SPEECH_RATE,
                pitch=settings.TTS_SPEECH_PITCH
            ))

        logger.info("Executing async Edge-TTS audio synthesis batch...")
        await asyncio.gather(*tasks)
        logger.info("All segment audio files generated.")

        # Overlay segments onto full timeline naturally without robotic pitch distortion
        for seg in segments:
            seg_file = temp_dir / f"seg_{seg['id']}.mp3"
            if not seg_file.exists():
                continue

            seg_audio = AudioSegment.from_file(str(seg_file))
            start_ms = int(seg["start"] * 1000)

            full_track = full_track.overlay(seg_audio, position=start_ms)

        # Export mastered 48kHz WAV
        full_track = full_track.set_frame_rate(48000).set_channels(2)
        full_track.export(str(output_wav_path), format="wav")
        
        logger.info(f"Master dubbed voice track successfully assembled: {output_wav_path}")
        return output_wav_path
