"""
EchoDub Engine - Central Configuration & Environment Settings
"""

import os
import sys
from pathlib import Path
from pydantic_settings import BaseSettings

# Automatically ensure FFmpeg is in PATH
ffmpeg_win_path = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages" / "Gyan.FFmpeg.Essentials_Microsoft.Winget.Source_8wekyb3d8bbwe" / "ffmpeg-8.1.1-essentials_build" / "bin"
if ffmpeg_win_path.exists() and str(ffmpeg_win_path) not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"{ffmpeg_win_path};" + os.environ.get("PATH", "")

# HuggingFace Fast Mirror
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"



class Settings(BaseSettings):
    # App Settings
    APP_NAME: str = "EchoDub AI Engine"
    DEBUG: bool = False
    API_PREFIX: str = "/api/v1"
    
    # Workspace & Storage Paths
    BASE_DIR: Path = Path(__file__).resolve().parent
    STORAGE_DIR: Path = BASE_DIR / "storage"
    TEMP_DIR: Path = STORAGE_DIR / "temp"
    OUTPUT_DIR: Path = STORAGE_DIR / "output"
    
    # Google Gemini AI Settings
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3-flash-preview"
    
    # Faster-Whisper Settings (Optimized for CPU)
    WHISPER_MODEL_SIZE: str = "tiny"  # or 'large-v3', 'base', 'small'
    WHISPER_DEVICE: str = "cpu"
    WHISPER_COMPUTE_TYPE: str = "int8"
    WHISPER_CPU_THREADS: int = 8
    
    # Edge-TTS Settings (Free High-Quality Persian Neural Voices)
    DEFAULT_PERSIAN_VOICE_MALE: str = "fa-IR-FaridNeural"
    DEFAULT_PERSIAN_VOICE_FEMALE: str = "fa-IR-DilaraNeural"
    DEFAULT_VOICE_RATE: str = "+0%"
    DEFAULT_VOICE_PITCH: str = "+0Hz"
    
    # Telegram CDN Settings
    TELEGRAM_API_ID: int = 0
    TELEGRAM_API_HASH: str = ""
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHANNEL_ID: str = ""  # e.g. "@my_dubbed_cdn_channel" or -1001234567890
    TELEGRAM_SESSION_NAME: str = "echodub_cdn_session"
    
    # Downloadly / Webhook Integration
    WORDPRESS_WEBHOOK_URL: str = ""
    WORDPRESS_API_TOKEN: str = ""
    
    # Audio Mastering Settings
    AUDIO_TARGET_LUFS: float = -14.0
    AUDIO_DUCKING_ATTENUATION_DB: float = -24.0
    ORIGINAL_AUDIO_VOLUME: float = 0.04   # Subtle background whisper (0.0 = completely muted, 0.04 = soft whisper)
    PRESERVE_ORIGINAL_AUDIO: bool = False # For educational courses, False = crystal-clear 100% Persian dubbing without English voice clash
    
    # Cleanup Setting
    AUTO_DELETE_TEMP_FILES: bool = True

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

settings = Settings()

# Ensure directories exist
os.makedirs(settings.STORAGE_DIR, exist_ok=True)
os.makedirs(settings.TEMP_DIR, exist_ok=True)
os.makedirs(settings.OUTPUT_DIR, exist_ok=True)
