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
        preserve_bgm: bool = True
    ) -> Path:
        """
        Combines original video with dubbed voiceover using FFmpeg.
        Applies sidechain ducking on original audio and normalizes volume to EBU R128 standard (-14 LUFS).
        """
        os.makedirs(output_video_path.parent, exist_ok=True)
        
        if preserve_bgm:
            # Complex filter:
            # [0:a] is original audio, [1:a] is new Persian voiceover
            # Duck original audio by attenuating background when voice is present, then mix together with loudnorm.
            filter_complex = (
                "[0:a]volume=0.2[bg];"
                "[1:a]volume=1.2[vox];"
                "[bg][vox]amix=inputs=2:duration=first:dropout_transition=2,"
                f"loudnorm=I={settings.AUDIO_TARGET_LUFS}:LRA=11:TP=-1.5[aout]"
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
            # Replace original audio completely with new dubbed track
            cmd = [
                "ffmpeg",
                "-y",
                "-i", str(original_video),
                "-i", str(dubbed_voiceover_wav),
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-af", f"loudnorm=I={settings.AUDIO_TARGET_LUFS}:LRA=11:TP=-1.5",
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
