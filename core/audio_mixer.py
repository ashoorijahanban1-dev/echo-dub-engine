"""
EchoDub Engine - Audio & Video Mastering Mixer
Muxes synthesized Persian voiceover with background ambience, applies EBU R128 loudness normalization, and packages MP4.
"""

import os
import asyncio
import logging
from pathlib import Path

from config import settings

logger = logging.getLogger("EchoDub.AudioMixer")

class AudioVideoMixer:
    @staticmethod
    async def mix_and_render(
        original_video: Path,
        dubbed_voiceover_wav: Path,
        output_video_path: Path,
        preserve_bgm: bool = False
    ) -> Path:
        """
        Combines original video with dubbed voiceover using FFmpeg.
        For educational tutorials:
        - By default (preserve_bgm=False), mutes original English speech completely for 100% clean Persian dubbing.
        - If preserve_bgm=True, reduces original background to a subtle 4% whisper so it never clashes with Persian dubbing.
        """
        os.makedirs(output_video_path.parent, exist_ok=True)
        
        bg_vol = settings.ORIGINAL_AUDIO_VOLUME
        
        if preserve_bgm and bg_vol > 0:
            # Subtle background mix with sidechain ducking
            filter_complex = (
                f"[0:a]volume={bg_vol}[bg];"
                "[1:a]volume=1.3[vox];"
                "[bg][vox]amix=inputs=2:duration=first:dropout_transition=2,"
                f"loudnorm=I={settings.AUDIO_TARGET_LUFS}:LRA=9:TP=-1.0[aout]"
            )
            
            cmd = [
                "ffmpeg",
                "-y",
                "-i", str(original_video),
                "-i", str(dubbed_voiceover_wav),
                "-filter_complex", filter_complex,
                "-map", "0:v:0",
                "-map", "[aout]",
                "-c:v", "copy",          # Fast zero-loss video stream copy!
                "-c:a", "aac",           # High quality AAC audio
                "-b:a", "192k",
                "-movflags", "+faststart", # Enable instant web & Telegram streaming
                str(output_video_path)
            ]
        else:
            # Pure clean Persian dubbing (standard for educational coding tutorials)
            cmd = [
                "ffmpeg",
                "-y",
                "-i", str(original_video),
                "-i", str(dubbed_voiceover_wav),
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-af", f"volume=1.25,loudnorm=I={settings.AUDIO_TARGET_LUFS}:LRA=9:TP=-1.0",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-movflags", "+faststart",
                str(output_video_path)
            ]

        logger.info(f"Running FFmpeg audio-video muxer on {original_video.name}...")
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            raise RuntimeError(f"FFmpeg muxing failed: {stderr.decode()}")

        logger.info(f"Final mastered dubbed video rendered at: {output_video_path}")
        return output_video_path

    @staticmethod
    async def generate_thumbnail(video_path: Path, output_thumb_path: Path) -> Path:
        """
        Extracts high-resolution poster frame from video at 1.5s mark.
        """
        os.makedirs(output_thumb_path.parent, exist_ok=True)
        cmd = [
            "ffmpeg",
            "-y",
            "-ss", "00:00:02",
            "-i", str(video_path),
            "-vframes", "1",
            "-q:v", "2",
            str(output_thumb_path)
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await process.communicate()
        return output_thumb_path
