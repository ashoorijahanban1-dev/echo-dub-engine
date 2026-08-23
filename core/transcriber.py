"""
EchoDub Engine - Fast CPU-Optimized Speech Recognition (Faster-Whisper)
Extracts audio from video and generates word-level timestamped transcription segments.
"""

import os
import asyncio
import logging
from pathlib import Path
from typing import List, Dict, Any
from faster_whisper import WhisperModel

from config import settings

logger = logging.getLogger("EchoDub.Transcriber")

class AudioTranscriber:
    _model_instance = None

    @classmethod
    def get_model(cls):
        """
        Singleton initialization of Faster-Whisper to reuse loaded memory weights.
        """
        if cls._model_instance is None:
            logger.info(f"Loading Faster-Whisper ({settings.WHISPER_MODEL_SIZE}) on CPU with {settings.WHISPER_CPU_THREADS} threads...")
            cls._model_instance = WhisperModel(
                model_size_or_path=settings.WHISPER_MODEL_SIZE,
                device=settings.WHISPER_DEVICE,
                compute_type=settings.WHISPER_COMPUTE_TYPE,
                cpu_threads=settings.WHISPER_CPU_THREADS,
                download_root=str(settings.MODELS_DIR)
            )
            logger.info("Faster-Whisper model loaded successfully.")
        return cls._model_instance

    @staticmethod
    async def extract_audio_track(video_path: Path, output_audio_path: Path) -> Path:
        """
        Extracts 16kHz mono audio track for Whisper processing via FFmpeg.
        """
        os.makedirs(output_audio_path.parent, exist_ok=True)
        cmd = [
            "ffmpeg",
            "-y",
            "-i", str(video_path),
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            str(output_audio_path)
        ]
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            raise RuntimeError(f"FFmpeg audio extraction failed: {stderr.decode()}")
            
        logger.info(f"Extracted audio track to: {output_audio_path}")
        return output_audio_path

    @classmethod
    async def transcribe(cls, audio_path: Path, language: str = "en") -> List[Dict[str, Any]]:
        """
        Runs speech-to-text with Voice Activity Detection (VAD) and word-level timing.
        """
        loop = asyncio.get_event_loop()
        
        def _run_whisper():
            model = cls.get_model()
            segments, info = model.transcribe(
                str(audio_path),
                language=language,
                beam_size=5,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
                word_timestamps=True
            )
            
            results = []
            for seg in segments:
                results.append({
                    "id": len(results) + 1,
                    "start": round(seg.start, 2),
                    "end": round(seg.end, 2),
                    "duration": round(seg.end - seg.start, 2),
                    "text": seg.text.strip(),
                    "words": [{"word": w.word, "start": round(w.start, 2), "end": round(w.end, 2)} for w in seg.words] if seg.words else []
                })
            return results, info

        logger.info(f"Transcribing audio file: {audio_path}")
        segments, info = await loop.run_in_executor(None, _run_whisper)
        logger.info(f"Transcription complete. Detected {len(segments)} segments. Duration: {info.duration:.1f}s")
        return segments
