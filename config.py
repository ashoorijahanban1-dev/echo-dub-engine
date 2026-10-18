"""
RPIM Pipeline Central Configuration
Defines settings, limits, backpressure thresholds, security tokens, and provider bindings.
"""

import os
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent

# Environment & Security Mode
ENV = os.environ.get("ENV", "development").lower() # "production", "staging", "development", "test"
CORS_ALLOWED_ORIGINS = [
    orig.strip() for orig in os.environ.get(
        "CORS_ALLOWED_ORIGINS",
        "https://rpim.ir,https://admin.rpim.ir,https://n8n.rpim.ir"
    ).split(",") if orig.strip()
]

# Database Configs (Supports dual PostgreSQL connections in prod, SQLite in dev/testing)
DISCOVERY_DB_URL = os.environ.get("DISCOVERY_DB_URL", "sqlite:///storage/discovery.db")
PROCESSING_DB_URL = os.environ.get("PROCESSING_DB_URL", "sqlite:///storage/processing.db")

# Server & Network Endpoints
IRAN_RELAY_BASE_URL = os.environ.get("IRAN_RELAY_BASE_URL", "https://rpim.ir")
US_ENGINE_BASE_URL = os.environ.get("US_ENGINE_BASE_URL", "http://localhost:8000")
RELAY_AUTH_SECRET = os.environ.get("RELAY_AUTH_SECRET", "")
INTERNAL_API_SECRET = os.environ.get("INTERNAL_API_SECRET", "")
CALLBACK_SIGNING_SECRET = os.environ.get("CALLBACK_SIGNING_SECRET", INTERNAL_API_SECRET or "rpim_callback_secret")

# HTTP & Proxy Configuration
# Internal clients ignore ambient system proxies (trust_env=False) by default
# to protect against SOCKS proxy missing socksio and prevent localhost interception.
PROXY_URL: Optional[str] = os.environ.get("PROXY_URL", "").strip() or None


def get_httpx_client_kwargs(
    timeout: float = 30.0,
    follow_redirects: bool = False,
    proxy: Optional[str] = None,
    **extra
) -> dict:
    """
    Returns standard safe kwargs for httpx.Client and httpx.AsyncClient.
    - trust_env is always False by default so ambient HTTP_PROXY/ALL_PROXY do NOT leak.
    - Explicit proxy is applied ONLY if proxy or config.PROXY_URL is set.
    """
    kwargs = {
        "trust_env": False,
        "timeout": timeout,
        "follow_redirects": follow_redirects,
    }
    effective_proxy = proxy or PROXY_URL
    if effective_proxy:
        kwargs["proxy"] = effective_proxy
    kwargs.update(extra)
    return kwargs


def validate_production_credentials():
    """
    Fail-fast check: If ENV == 'production', verify that required production secrets
    are present and not insecure dummy strings.
    """
    if ENV != "production":
        return

    required = {
        "INTERNAL_API_SECRET": INTERNAL_API_SECRET,
        "RELAY_AUTH_SECRET": RELAY_AUTH_SECRET,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHANNEL_ID": TELEGRAM_CHANNEL_ID,
        "RPIM_API_KEY": RPIM_API_KEY,
    }
    missing = []
    for k, val in required.items():
        if not val or "change_me" in val or "generate_a_secure" in val or val.startswith("mock_"):
            missing.append(k)

    if missing:
        raise ValueError(f"CRITICAL: Production deployment aborted! Insecure or missing credentials: {', '.join(missing)}")


# Google AI Studio / Gemini TTS Config (Mandatory Primary Provider)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_KEY_POOL = [
    k.strip() for k in os.environ.get(
        "GEMINI_KEY_POOL",
        GEMINI_API_KEY
    ).split(",") if k.strip()
]
GEMINI_TTS_MODEL = os.environ.get("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts")
DEFAULT_VOICE_MALE = os.environ.get("DEFAULT_VOICE_MALE", "Puck")
DEFAULT_VOICE_FEMALE = os.environ.get("DEFAULT_VOICE_FEMALE", "Aoede")
# Note: Google Gemini TTS Puck is the gold standard voice.
# Key Pool ensures continuous zero-cost synthesis across multi-account quota buckets.

# TTS Engine Configuration: "google_gemini", "edge_tts_farid", or "self_hosted_piper"
TTS_ENGINE = os.environ.get("TTS_ENGINE", "google_gemini")
EDGE_TTS_VOICE = os.environ.get("EDGE_TTS_VOICE", "fa-IR-FaridNeural")
EDGE_TTS_RATE = os.environ.get("EDGE_TTS_RATE", "+14%")
EDGE_TTS_PITCH = os.environ.get("EDGE_TTS_PITCH", "+3Hz")
PIPER_MODEL_PATH = os.environ.get("PIPER_MODEL_PATH", "/app/storage/models/piper/fa_IR-amir-medium.onnx")
PIPER_CONFIG_PATH = os.environ.get("PIPER_CONFIG_PATH", "/app/storage/models/piper/fa_IR-amir-medium.onnx.json")

# Audio Track Mode: "solo_persian" (100% Persian, no original background voice) or "dual_audio"
AUDIO_TRACK_MODE = os.environ.get("AUDIO_TRACK_MODE", "solo_persian")

# Dubbing Style & Conversational Rewriting Configuration
DUBBING_STYLE = os.environ.get("DUBBING_STYLE", "conversational_professional") # "conversational_professional" or "formal"
DUBBING_STYLE_VERSION = os.environ.get("DUBBING_STYLE_VERSION", "fa_tehrani_puck_v1")

# Linguistic Quality Control Thresholds
QC_MAX_WORDS_PER_SENTENCE = int(os.environ.get("QC_MAX_WORDS_PER_SENTENCE", "25"))
QC_MAX_CHARS_PER_SECOND = float(os.environ.get("QC_MAX_CHARS_PER_SECOND", "18.0"))
QC_MIN_CHARS_PER_SECOND = float(os.environ.get("QC_MIN_CHARS_PER_SECOND", "4.0"))

# Telegram Storage & Gateway
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "-1001234567890")
TELEGRAM_API_BASE_URL = os.environ.get("TELEGRAM_API_BASE_URL", "https://api.telegram.org")
TELEGRAM_LOCAL_API_SERVER = os.environ.get("TELEGRAM_LOCAL_API_SERVER", "") # e.g. http://telegram-bot-api:8081 for >50MB files

# RPIM.ir Website Publishing Adapter
RPIM_API_BASE_URL = os.environ.get("RPIM_API_BASE_URL", "https://rpim.ir/api")
RPIM_API_KEY = os.environ.get("RPIM_API_KEY", "")
PUBLISH_DEFAULT_VISIBILITY = os.environ.get("PUBLISH_DEFAULT_VISIBILITY", "DRAFT") # DRAFT or PUBLISHED
AUTO_PUBLISH_ON_COMPLETE = os.environ.get("AUTO_PUBLISH_ON_COMPLETE", "false").lower() in ("true", "1", "yes")

# Storage & Cache Directories
STORAGE_DIR = Path(os.environ.get("STORAGE_DIR", str(BASE_DIR / "storage")))
TRANSFER_SPOOL_DIR = Path(os.environ.get("TRANSFER_SPOOL_DIR", str(STORAGE_DIR / "spool")))
CACHE_STORAGE_DIR = Path(os.environ.get("CACHE_STORAGE_DIR", str(STORAGE_DIR / "cache")))

# Transfer System
TRANSFER_DEFAULT_ADAPTER = os.environ.get("TRANSFER_DEFAULT_ADAPTER", "STREAM") # STREAM or SPOOL

# Worker & Resource Caps (Backpressure & Limits)
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "1"))
MAX_CONCURRENT_DUBBING_JOBS = int(os.environ.get("MAX_CONCURRENT_DUBBING_JOBS", "1"))
MIN_FREE_DISK_GB = float(os.environ.get("MIN_FREE_DISK_GB", "10.0"))
MAX_ARCHIVE_EXTRACT_GB = float(os.environ.get("MAX_ARCHIVE_EXTRACT_GB", "30.0"))
JOB_LEASE_SECONDS = int(os.environ.get("JOB_LEASE_SECONDS", "300"))
JOB_HEARTBEAT_INTERVAL_SECONDS = int(os.environ.get("JOB_HEARTBEAT_INTERVAL_SECONDS", "30"))

# Quality Control (QC) Thresholds
QC_MAX_DURATION_DRIFT_RATIO = float(os.environ.get("QC_MAX_DURATION_DRIFT_RATIO", "0.15"))
QC_MAX_SILENCE_RATIO = float(os.environ.get("QC_MAX_SILENCE_RATIO", "0.25"))

# Taxonomy Engine
TAXONOMY_DEFAULT_TAG_COUNT = int(os.environ.get("TAXONOMY_DEFAULT_TAG_COUNT", "5"))
TAXONOMY_AUTO_CREATE_ENABLED = os.environ.get("TAXONOMY_AUTO_CREATE_ENABLED", "true").lower() in ("true", "1", "yes")
TAXONOMY_MAX_CATEGORY_DEPTH = int(os.environ.get("TAXONOMY_MAX_CATEGORY_DEPTH", "3"))
