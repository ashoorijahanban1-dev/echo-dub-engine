"""
EchoDub Engine - High-Speed Resilient Video Downloader
Supports multi-connection chunked download via aria2c or aiohttp fallback.
"""

import os
import shutil
import asyncio
import logging
from pathlib import Path
from typing import Optional
import aiohttp
import aiofiles

logger = logging.getLogger("EchoDub.Downloader")

class VideoDownloader:
    @staticmethod
    async def download_video(url: str, output_dir: Path, custom_filename: Optional[str] = None) -> Path:
        """
        Downloads a video file from a given URL using aria2c (if available) or async streaming chunks.
        """
        os.makedirs(output_dir, exist_ok=True)
        
        # Determine filename
        if custom_filename:
            filename = custom_filename
        else:
            raw_name = url.split("?")[0].split("/")[-1]
            filename = raw_name if raw_name.endswith(('.mp4', '.mkv', '.webm', '.mov')) else "source_video.mp4"
            
        target_path = output_dir / filename
        
        # Check if aria2c is installed on server (preferred for multi-thread speed)
        has_aria2 = shutil.which("aria2c") is not None
        
        if has_aria2:
            logger.info(f"Downloading with aria2c (8 connections): {url}")
            cmd = [
                "aria2c",
                "-x", "8",
                "-s", "8",
                "-k", "1M",
                "--continue=true",
                "--allow-overwrite=true",
                "--dir", str(output_dir),
                "--out", filename,
                url
            ]
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                logger.warning(f"aria2c failed with code {process.returncode}, falling back to aiohttp. Error: {stderr.decode()}")
                return await VideoDownloader._download_with_aiohttp(url, target_path)
            
            logger.info(f"Video downloaded successfully via aria2c: {target_path}")
            return target_path
        else:
            logger.info(f"aria2c not found, downloading via aiohttp: {url}")
            return await VideoDownloader._download_with_aiohttp(url, target_path)

    @staticmethod
    async def _download_with_aiohttp(url: str, target_path: Path) -> Path:
        timeout = aiohttp.ClientTimeout(total=1800)  # 30 min max for large educational videos
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    raise Exception(f"Failed to download video. HTTP Status: {response.status}")
                
                async with aiofiles.open(target_path, 'wb') as f:
                    while True:
                        chunk = await response.content.read(1024 * 1024)  # 1MB chunks
                        if not chunk:
                            break
                        await f.write(chunk)
                        
        logger.info(f"Video downloaded successfully via aiohttp: {target_path}")
        return target_path
