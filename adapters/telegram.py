"""
Telegram Storage Gateway & Invisible Delivery Adapter
Handles:
  1. Real Telegram Bot API media uploads using pre-assigned asset_id (no secondary IDs)
  2. Verified retrieval check before marking VERIFIED
  3. Support for TELEGRAM_LOCAL_API_SERVER for large files (>50MB up to 2GB)
  4. Automatic fetching & caching from Telegram if local file is missing (never 404 for verified assets)
  5. Timeout reconciliation
  6. RFC 7233 HTTP Range streaming (200, 206, 416)
"""

import os
import re
import json
import uuid
import hashlib
from pathlib import Path
from typing import Dict, Any, Optional, Generator, Tuple

import config

class TelegramAPIException(Exception):
    pass

class RangeNotSatisfiableException(Exception):
    pass


class TelegramStorageAdapter:
    def __init__(
        self,
        bot_token: Optional[str] = None,
        channel_id: Optional[str] = None,
        api_base_url: Optional[str] = None,
        local_api_server: Optional[str] = None,
        proxy_url: Optional[str] = None,
        test_double: bool = False
    ):
        self.bot_token = bot_token or config.TELEGRAM_BOT_TOKEN
        self.channel_id = channel_id or config.TELEGRAM_CHANNEL_ID
        self.local_api_server = local_api_server or config.TELEGRAM_LOCAL_API_SERVER
        self.proxy_url = proxy_url or config.PROXY_URL
        self.test_double = test_double
        # If local API server is specified, prefer it for bot API calls
        if self.local_api_server:
            self.api_base_url = self.local_api_server.rstrip('/')
        else:
            self.api_base_url = (api_base_url or config.TELEGRAM_API_BASE_URL).rstrip('/')

    def _create_http_client(self, timeout: float = 30.0, follow_redirects: bool = False):
        """
        Creates an httpx.Client with trust_env=False to prevent ambient environment
        proxies (e.g. SOCKS) from breaking internal/local requests unless PROXY_URL is set.
        """
        import httpx
        kwargs = {
            "trust_env": False,
            "timeout": timeout,
            "follow_redirects": follow_redirects
        }
        if self.proxy_url:
            kwargs["proxy"] = self.proxy_url
        return httpx.Client(**kwargs)


    def upload_media_asset(
        self,
        media_path: Path,
        asset_id: str,
        caption: str = "",
        duration_seconds: float = 0.0,
        width: int = 1920,
        height: int = 1080,
        test_double: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        Uploads a video asset to the Telegram storage channel and indexes permanent metadata.
        CRITICAL RULE: Uses pre-assigned asset_id passed in from caller. Does NOT generate a second ID!
        """
        if not asset_id:
            raise ValueError("asset_id must be pre-assigned before calling upload_media_asset; adapter cannot generate a secondary asset_id.")

        media_path = Path(media_path).resolve()
        if not media_path.exists():
            raise FileNotFoundError(f"Media file not found: {media_path}")

        file_size = media_path.stat().st_size
        
        hasher = hashlib.sha256()
        with open(media_path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        sha256 = hasher.hexdigest()

        is_td = self.test_double if test_double is None else test_double

        # Test double mode (allowed only via explicit dependency injection or parameter)
        if is_td:
            return {
                "asset_id": asset_id,
                "storage_provider": "TELEGRAM_CDN",
                "chat_id": self.channel_id,
                "message_id": 99990001,
                "file_id": f"BAACAgQAAxkBAAE_{sha256[:20]}",
                "file_unique_id": f"AgAD_{sha256[20:30]}",
                "file_size": file_size,
                "sha256": sha256,
                "mime_type": "video/mp4",
                "duration_seconds": duration_seconds,
                "status": "VERIFIED",
                "playback_url": f"/api/v1/stream/{asset_id}",
                "is_test_double": True
            }

        if not self.bot_token or self.bot_token.startswith("mock_") or "Your" in self.bot_token:
            raise TelegramAPIException("Telegram bot token is not configured and test_double is False.")

        import httpx
        url = f"{self.api_base_url}/bot{self.bot_token}/sendVideo"
        
        try:
            with open(media_path, "rb") as f:
                files = {"video": (media_path.name, f, "video/mp4")}
                data = {
                    "chat_id": self.channel_id,
                    "caption": caption[:1024],
                    "supports_streaming": "true",
                    "duration": int(duration_seconds),
                    "width": width,
                    "height": height
                }
                resp = httpx.post(url, data=data, files=files, timeout=300.0, trust_env=False, proxy=self.proxy_url)

            if resp.status_code != 200:
                raise TelegramAPIException(f"Telegram API upload error {resp.status_code}: {resp.text}")

            res_json = resp.json()
            if not res_json.get("ok"):
                raise TelegramAPIException(f"Telegram API error response: {res_json.get('description')}")

            result = res_json["result"]
            video_meta = result.get("video") or result.get("document", {})
            message_id = result.get("message_id")
            file_id = video_meta.get("file_id")
            file_unique_id = video_meta.get("file_unique_id")

            # Retrieval verification: Call getFile to verify accessibility
            self.verify_file_retrieval(file_id)

            return {
                "asset_id": asset_id,
                "storage_provider": "TELEGRAM_CDN",
                "chat_id": self.channel_id,
                "message_id": message_id,
                "file_id": file_id,
                "file_unique_id": file_unique_id,
                "file_size": file_size,
                "sha256": sha256,
                "mime_type": "video/mp4",
                "duration_seconds": duration_seconds,
                "status": "VERIFIED",
                "playback_url": f"/api/v1/stream/{asset_id}",
                "is_test_double": False
            }
        except TelegramAPIException:
            raise
        except Exception as e:
            raise TelegramAPIException(f"Failed to upload media to Telegram: {e}")

    def verify_file_retrieval(self, file_id: str, test_double: Optional[bool] = None) -> str:
        """
        Verifies that file_id exists and can be retrieved via getFile API.
        Returns file_path.
        """
        is_td = self.test_double if test_double is None else test_double
        if is_td:
            return f"videos/{file_id}.mp4"

        if not self.bot_token or self.bot_token.startswith("mock_"):
            raise TelegramAPIException("Telegram bot token is not configured and test_double is False.")

        import httpx
        url = f"{self.api_base_url}/bot{self.bot_token}/getFile?file_id={file_id}"
        resp = httpx.get(url, timeout=30.0, trust_env=False, proxy=self.proxy_url)
        if resp.status_code != 200:
            raise TelegramAPIException(f"Retrieval verification failed for file_id {file_id}: status {resp.status_code}")
        data = resp.json()
        if not data.get("ok"):
            raise TelegramAPIException(f"getFile returned false: {data.get('description')}")
        return data["result"].get("file_path", "")

    def download_file_from_telegram(
        self,
        file_id: str,
        destination_path: Path,
        test_double: Optional[bool] = None
    ) -> Path:
        """
        Fetches a file from Telegram / Local Bot API into the local cache.
        Ensures local file deletion never results in a 404 for verified assets.
        In production, requires a valid token and streams real bytes from Telegram or Local Bot API.
        """
        destination_path = Path(destination_path).resolve()
        destination_path.parent.mkdir(parents=True, exist_ok=True)

        is_td = self.test_double if test_double is None else test_double
        if is_td:
            if not destination_path.exists():
                destination_path.write_bytes(b"TELEGRAM_STREAMING_DATA_" + b"1" * 2048)
            return destination_path

        if not self.bot_token or self.bot_token.startswith("mock_"):
            raise TelegramAPIException("Cannot download from Telegram: bot_token is not configured and test_double is False.")

        file_path = self.verify_file_retrieval(file_id, test_double=False)
        
        import httpx
        # If local bot API server, file might be accessible locally on disk or via HTTP
        download_url = f"{self.api_base_url}/file/bot{self.bot_token}/{file_path}"
        with httpx.stream("GET", download_url, timeout=300.0, trust_env=False, proxy=self.proxy_url) as resp:
            if resp.status_code != 200:
                raise TelegramAPIException(f"Failed to download file from Telegram: HTTP {resp.status_code}")
            with open(destination_path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=65536):
                    f.write(chunk)

        return destination_path

    @staticmethod
    def handle_range_request(file_size_or_path: Any, range_header: Optional[str] = None) -> Dict[str, Any]:
        """
        Parses Range header according to RFC 7233:
        Accepts either an integer file_size or a Path/str to a media file.
        Supports:
          - None: 200 OK (Full content)
          - bytes=start-end (e.g. bytes=100-200)
          - bytes=start- (e.g. bytes=500-)
          - bytes=-suffix (e.g. bytes=-300, meaning last 300 bytes)
        Returns status_code (200, 206, or 416), headers dictionary, and slice (start, end, length).
        """
        if isinstance(file_size_or_path, (int, float)):
            file_size = int(file_size_or_path)
        else:
            p = Path(file_size_or_path)
            file_size = p.stat().st_size if p.exists() else 0

        if file_size == 0:
            return {
                "status_code": 200,
                "headers": {
                    "Content-Type": "video/mp4",
                    "Content-Length": "0",
                    "Accept-Ranges": "bytes"
                },
                "start": 0,
                "end": 0,
                "length": 0
            }

        if not range_header or not range_header.strip().startswith("bytes="):
            return {
                "status_code": 200,
                "headers": {
                    "Content-Type": "video/mp4",
                    "Content-Length": str(file_size),
                    "Accept-Ranges": "bytes"
                },
                "start": 0,
                "end": file_size - 1,
                "length": file_size
            }

        raw_range = range_header.strip().replace("bytes=", "")
        spec = raw_range.split(",")[0].strip()

        start: int = 0
        end: int = file_size - 1

        if spec.startswith("-"):
            # Suffix range: bytes=-500 (last 500 bytes)
            try:
                suffix_len = int(spec[1:])
            except ValueError:
                suffix_len = 0
            if suffix_len <= 0:
                return TelegramStorageAdapter._make_416(file_size)
            start = max(0, file_size - suffix_len)
            end = file_size - 1
        elif spec.endswith("-"):
            # Open-ended range: bytes=500-
            try:
                start = int(spec[:-1])
            except ValueError:
                start = 0
            end = file_size - 1
        else:
            # Full range: bytes=100-200
            parts = spec.split("-")
            if len(parts) != 2:
                return TelegramStorageAdapter._make_416(file_size)
            try:
                start = int(parts[0])
                end = int(parts[1])
            except ValueError:
                return TelegramStorageAdapter._make_416(file_size)

        # Validation per RFC 7233
        if start >= file_size or end >= file_size or start > end or start < 0:
            return TelegramStorageAdapter._make_416(file_size)

        length = (end - start) + 1
        return {
            "status_code": 206,
            "headers": {
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(length),
                "Accept-Ranges": "bytes"
            },
            "start": start,
            "end": end,
            "length": length
        }

    @staticmethod
    def _make_416(file_size: int) -> Dict[str, Any]:
        return {
            "status_code": 416,
            "headers": {
                "Content-Range": f"bytes */{file_size}",
                "Accept-Ranges": "bytes"
            },
            "start": 0,
            "end": 0,
            "length": 0
        }

    @staticmethod
    def stream_file_range(file_path: Path, start: int, length: int, chunk_size: int = 65536) -> Generator[bytes, None, None]:
        with open(file_path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                read_len = min(chunk_size, remaining)
                chunk = f.read(read_len)
                if not chunk:
                    break
                yield chunk
                remaining -= len(chunk)
