"""
EchoDub Engine - Telegram High-Speed CDN Uploader
Uploads 1080p dubbed videos to personal Telegram Channel and generates streamable CDN links.
"""

import os
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from pyrogram import Client

from config import settings

logger = logging.getLogger("EchoDub.TelegramUploader")

class TelegramCDNUploader:
    _client_instance = None

    @classmethod
    async def get_client(cls) -> Optional[Client]:
        """
        Initializes Pyrogram MTProto Client session for high-speed Telegram uploads.
        """
        if cls._client_instance is None:
            if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_API_ID:
                logger.warning("Telegram credentials not provided. Telegram CDN uploading will be skipped.")
                return None

            cls._client_instance = Client(
                name=settings.TELEGRAM_SESSION_NAME,
                api_id=settings.TELEGRAM_API_ID,
                api_hash=settings.TELEGRAM_API_HASH,
                bot_token=settings.TELEGRAM_BOT_TOKEN,
                workdir=str(settings.STORAGE_DIR)
            )
            await cls._client_instance.start()
            logger.info("Pyrogram MTProto client started successfully.")
            
        return cls._client_instance

    @classmethod
    async def upload_dubbed_video(
        cls,
        video_path: Path,
        thumbnail_path: Optional[Path],
        title: str,
        duration: int,
        source_url: str
    ) -> Dict[str, Any]:
        """
        Uploads video to target Telegram Channel with metadata, thumbnail, and streaming flag.
        Returns post link and message details for website embedding.
        """
        client = await cls.get_client()
        if not client or not settings.TELEGRAM_CHANNEL_ID:
            logger.warning("Telegram upload skipped: Credentials or TELEGRAM_CHANNEL_ID missing.")
            return {
                "uploaded": False,
                "telegram_link": None,
                "message_id": None,
                "channel_id": None
            }

        logger.info(f"Starting Telegram MTProto upload for: {video_path.name} ({video_path.stat().st_size / (1024*1024):.2f} MB)")

        # Prepare rich HTML caption
        caption = (
            f"🎬 <b>{title}</b>\n\n"
            f"🎙️ <i>دوبله اختصاصی و هوشمند به زبان فارسی (EchoDub AI)</i>\n"
            f"⏱️ <b>مدت زمان:</b> {duration // 60}:{duration % 60:02d}\n"
            f"🌐 <b>منبع اصلی:</b> <a href='{source_url}'>Downloadly.ir</a>\n\n"
            f"⚡ <i>پخش مستقیم با سرعت بالا بدون قطعی</i>"
        )

        try:
            msg = await client.send_video(
                chat_id=settings.TELEGRAM_CHANNEL_ID,
                video=str(video_path),
                caption=caption,
                duration=duration,
                thumb=str(thumbnail_path) if (thumbnail_path and thumbnail_path.exists()) else None,
                supports_streaming=True
            )

            # Generate public or private channel post link
            channel_username = str(settings.TELEGRAM_CHANNEL_ID).replace("@", "")
            if channel_username.startswith("-100"):
                clean_id = channel_username.replace("-100", "")
                post_link = f"https://t.me/c/{clean_id}/{msg.id}"
            else:
                post_link = f"https://t.me/{channel_username}/{msg.id}"

            logger.info(f"Video uploaded successfully to Telegram! Post link: {post_link}")

            return {
                "uploaded": True,
                "telegram_link": post_link,
                "message_id": msg.id,
                "channel_id": settings.TELEGRAM_CHANNEL_ID,
                "file_id": msg.video.file_id if msg.video else None
            }

        except Exception as e:
            logger.error(f"Error during Telegram upload: {e}")
            return {
                "uploaded": False,
                "error": str(e),
                "telegram_link": None
            }
