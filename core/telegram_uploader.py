"""
EchoDub Engine - Telegram High-Speed CDN Uploader
Uploads 1080p dubbed videos to personal Telegram Channel and generates streamable CDN links.
"""

import os
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional
import httpx
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

            try:
                cls._client_instance = Client(
                    name=settings.TELEGRAM_SESSION_NAME,
                    api_id=settings.TELEGRAM_API_ID,
                    api_hash=settings.TELEGRAM_API_HASH,
                    bot_token=settings.TELEGRAM_BOT_TOKEN,
                    workdir=str(settings.STORAGE_DIR)
                )
                await cls._client_instance.start()
                logger.info("Pyrogram MTProto client started successfully.")
            except Exception as e:
                logger.warning(f"Pyrogram start warning: {e}. Will fallback to Bot API.")
                cls._client_instance = None
            
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
        if not settings.TELEGRAM_CHANNEL_ID or not settings.TELEGRAM_BOT_TOKEN:
            logger.warning("Telegram upload skipped: Credentials or TELEGRAM_CHANNEL_ID missing.")
            return {
                "uploaded": False,
                "telegram_link": None,
                "message_id": None,
                "channel_id": None
            }

        file_size_mb = video_path.stat().st_size / (1024*1024)
        logger.info(f"Starting Telegram upload for: {video_path.name} ({file_size_mb:.2f} MB)")

        client = await cls.get_client()
        
        if not client:
            if file_size_mb > 50:
                logger.warning(f"File size is {file_size_mb:.2f}MB (>50MB) and Pyrogram is not available. Cannot upload via standard Bot API.")
            else:
                logger.warning("Pyrogram client is not available. Upload failed.")
            return {"uploaded": False, "error": "Pyrogram unavailable", "telegram_link": None}

        # Prepare rich HTML caption
        caption = (
            f"🎬 <b>{title}</b>\n\n"
            f"🎙️ <i>دوبله اختصاصی و هوشمند به زبان فارسی (EchoDub AI)</i>\n"
            f"⏱️ <b>مدت زمان:</b> {duration // 60}:{duration % 60:02d}\n"
            f"🌐 <b>منبع اصلی:</b> <a href='{source_url}'>EchoDub</a>\n\n"
            f"⚡ <i>پخش مستقیم با سرعت بالا بدون قطعی</i>"
        )

        raw_channel_id = str(settings.TELEGRAM_CHANNEL_ID).strip()
        target_chat_id = int(raw_channel_id) if (raw_channel_id.startswith("-") and raw_channel_id[1:].isdigit()) or raw_channel_id.isdigit() else raw_channel_id

        # 1. Try Pyrogram MTProto first
        try:
            client = await cls.get_client()
            if client:
                msg = await client.send_video(
                    chat_id=target_chat_id,
                    video=str(video_path),
                    caption=caption,
                    duration=duration,
                    thumb=str(thumbnail_path) if (thumbnail_path and thumbnail_path.exists()) else None,
                    supports_streaming=True
                )

                channel_username = str(raw_channel_id).replace("@", "")
                if channel_username.startswith("-100"):
                    clean_id = channel_username.replace("-100", "")
                    post_link = f"https://t.me/c/{clean_id}/{msg.id}"
                else:
                    post_link = f"https://t.me/{channel_username}/{msg.id}"

                logger.info(f"Video uploaded successfully via MTProto! Link: {post_link}")
                return {
                    "uploaded": True,
                    "telegram_link": post_link,
                    "message_id": msg.id,
                    "channel_id": str(target_chat_id),
                    "file_id": msg.video.file_id if msg.video else None
                }
        except Exception as pyrogram_err:
            logger.warning(f"Pyrogram MTProto upload notice ({pyrogram_err}). Falling back to Telegram Bot API...")

        # 2. Resilient Fallback to Telegram Bot HTTP API
        try:
            async with httpx.AsyncClient(timeout=300.0) as http_client:
                with open(video_path, "rb") as vf:
                    files = {"video": (video_path.name, vf, "video/mp4")}
                    data = {
                        "chat_id": str(target_chat_id),
                        "caption": caption,
                        "parse_mode": "HTML",
                        "supports_streaming": "true"
                    }
                    if thumbnail_path and thumbnail_path.exists():
                        with open(thumbnail_path, "rb") as tf:
                            files["thumbnail"] = (thumbnail_path.name, tf, "image/jpeg")
                    res = await http_client.post(
                        f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendVideo",
                        data=data,
                        files=files
                    )
                    res_json = res.json()
                    if res_json.get("ok"):
                        msg_data = res_json["result"]
                        msg_id = msg_data["message_id"]
                        channel_username = str(raw_channel_id).replace("@", "")
                        if channel_username.startswith("-100"):
                            clean_id = channel_username.replace("-100", "")
                            post_link = f"https://t.me/c/{clean_id}/{msg_id}"
                        else:
                            post_link = f"https://t.me/{channel_username}/{msg_id}"

                        logger.info(f"Video uploaded successfully via Bot API! Link: {post_link}")
                        return {
                            "uploaded": True,
                            "telegram_link": post_link,
                            "message_id": msg_id,
                            "channel_id": str(target_chat_id),
                            "file_id": msg_data.get("video", {}).get("file_id")
                        }
                    else:
                        logger.error(f"Bot API upload error: {res_json}")
                        return {"uploaded": False, "error": str(res_json), "telegram_link": None}
        except Exception as http_err:
            logger.error(f"Error during Telegram Bot API fallback upload: {http_err}")
            return {"uploaded": False, "error": str(http_err), "telegram_link": None}
