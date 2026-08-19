"""
EchoDub Engine - Master AI Dubbing Pipeline Orchestrator
Coordinates all 8 processing stages with progress tracking and error resilience.
"""

import os
import time
import uuid
import logging
from pathlib import Path
from typing import Dict, Any, Callable, Optional

from config import settings
from core.downloader import VideoDownloader
from core.transcriber import AudioTranscriber
from core.translator import TechTranslator
from core.voice_synthesizer import VoiceSynthesizer
from core.audio_mixer import AudioVideoMixer
from core.telegram_uploader import TelegramCDNUploader
from core.cleaner import DiskCleaner

logger = logging.getLogger("EchoDub.Pipeline")

class DubbingJobPipeline:
    def __init__(self, job_id: Optional[str] = None):
        self.job_id = job_id or f"job_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        self.job_dir = settings.TEMP_DIR / self.job_id
        os.makedirs(self.job_dir, exist_ok=True)
        
        self.translator = TechTranslator()

    async def run(
        self,
        video_url: str,
        title: Optional[str] = None,
        voice_gender: str = "male",
        preserve_bgm: bool = True,
        progress_callback: Optional[Callable[[int, str], Any]] = None
    ) -> Dict[str, Any]:
        """
        Executes full pipeline by downloading video from URL.
        """
        raw_video_path = await VideoDownloader.download_video(video_url, self.job_dir)
        return await self._process_video_pipeline(
            raw_video_path=raw_video_path,
            video_url=video_url,
            title=title,
            voice_gender=voice_gender,
            preserve_bgm=preserve_bgm,
            progress_callback=progress_callback
        )

    async def run_from_local_file(
        self,
        local_video_path: Path,
        title: Optional[str] = None,
        voice_gender: str = "male",
        preserve_bgm: bool = True,
        progress_callback: Optional[Callable[[int, str], Any]] = None
    ) -> Dict[str, Any]:
        """
        Executes pipeline on a video file directly uploaded from Iran Server (Geo-IP Bypass).
        """
        return await self._process_video_pipeline(
            raw_video_path=local_video_path,
            video_url=local_video_path.name,
            title=title,
            voice_gender=voice_gender,
            preserve_bgm=preserve_bgm,
            progress_callback=progress_callback
        )

    async def _process_video_pipeline(
        self,
        raw_video_path: Path,
        video_url: str,
        title: Optional[str],
        voice_gender: str,
        preserve_bgm: bool,
        progress_callback: Optional[Callable[[int, str], Any]]
    ) -> Dict[str, Any]:
        start_time = time.time()
        logger.info(f"Processing Dubbing Job [{self.job_id}] for file: {raw_video_path.name}")

        async def update_progress(percent: int, message: str):
            logger.info(f"[{self.job_id}] [{percent}%] {message}")
            if progress_callback:
                if asyncio.iscoroutinefunction(progress_callback):
                    await progress_callback(percent, message)
                else:
                    progress_callback(percent, message)

        try:
            video_title = title or raw_video_path.stem.replace("_", " ").replace("-", " ").title()

            # Stage 2: Audio Extraction
            await update_progress(25, "Extracting audio track for transcription...")
            extracted_wav = self.job_dir / "audio_16k.wav"
            await AudioTranscriber.extract_audio_track(raw_video_path, extracted_wav)

            # Stage 3: Faster-Whisper Transcription & Timing
            await update_progress(40, "Running Faster-Whisper speech recognition & timing...")
            segments = await AudioTranscriber.transcribe(extracted_wav, language="en")
            total_duration = segments[-1]["end"] if segments else 10.0

            # Stage 4: Technical & Educational Translation (Gemini)
            await update_progress(60, "Translating dialogue into natural Persian (IT Glossary enabled)...")
            translated_segments = await self.translator.translate_segments(segments)

            # Stage 5: Neural Persian Voice Synthesis (Edge-TTS)
            await update_progress(75, f"Synthesizing fluent Persian speech ({voice_gender} voice)...")
            dubbed_voice_wav = self.job_dir / "master_dubbed_voice.wav"
            await VoiceSynthesizer.generate_full_dubbed_audio(
                segments=translated_segments,
                total_duration=total_duration,
                output_wav_path=dubbed_voice_wav,
                temp_dir=self.job_dir / "voice_segments",
                voice_gender=voice_gender
            )

            # Stage 6: Master Audio-Video Muxing & EBU R128 Loudnorm
            await update_progress(85, "Muxing final video with audio ducking & volume mastering...")
            final_video_path = settings.OUTPUT_DIR / f"{self.job_id}_{raw_video_path.name}"
            await AudioVideoMixer.mix_and_render(
                original_video=raw_video_path,
                dubbed_voiceover_wav=dubbed_voice_wav,
                output_video_path=final_video_path,
                preserve_bgm=preserve_bgm
            )

            # Generate thumbnail
            thumb_path = self.job_dir / "thumbnail.jpg"
            await AudioVideoMixer.generate_thumbnail(final_video_path, thumb_path)

            # Stage 7: Upload to Telegram Channel CDN
            await update_progress(95, "Uploading 1080p dubbed video to Telegram Channel CDN...")
            tg_result = await TelegramCDNUploader.upload_dubbed_video(
                video_path=final_video_path,
                thumbnail_path=thumb_path,
                title=video_title,
                duration=int(total_duration),
                source_url=video_url
            )

            # Stage 8: Completed & Cleanup
            elapsed_time = round(time.time() - start_time, 2)
            await update_progress(100, f"Dubbing complete in {elapsed_time}s! Video ready on Telegram CDN.")

            if settings.AUTO_DELETE_TEMP_FILES:
                DiskCleaner.cleanup_job_directory(self.job_dir)

            return {
                "success": True,
                "job_id": self.job_id,
                "title": video_title,
                "duration_seconds": total_duration,
                "elapsed_time_seconds": elapsed_time,
                "output_video_path": str(final_video_path),
                "telegram": tg_result,
                "segments_count": len(translated_segments)
            }

        except Exception as e:
            logger.error(f"Pipeline failed for job {self.job_id}: {e}", exc_info=True)
            await update_progress(0, f"Error in pipeline: {str(e)}")
            return {
                "success": False,
                "job_id": self.job_id,
                "error": str(e)
            }
