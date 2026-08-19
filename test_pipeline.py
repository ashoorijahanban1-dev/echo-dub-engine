"""
EchoDub Engine - Local Pipeline CLI & Dry-Run Tester
Allows testing the dubbing engine directly from the command line.
"""

import sys
import asyncio
import argparse
import logging
from pathlib import Path

from config import settings
from pipeline import DubbingJobPipeline

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("EchoDub.CLI")

async def main():
    parser = argparse.ArgumentParser(description="EchoDub AI Video Dubbing Pipeline CLI Tester")
    parser.add_argument("--url", type=str, default="https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4", help="Video URL to dub")
    parser.add_argument("--title", type=str, default="آموزش نمونه هوش مصنوعی و کانتینرها", help="Video Title")
    parser.add_argument("--voice", type=str, choices=["male", "female"], default="male", help="Voice gender (Farid / Dilara)")
    parser.add_argument("--keep-bgm", action="store_true", default=True, help="Preserve background music & ambient sounds")

    args = parser.parse_args()

    print("\n" + "="*70)
    print("🎙️ EchoDub AI - Automated Video Dubbing Pipeline Runner")
    print(f"🎬 Video Source: {args.url}")
    print(f"📌 Title:        {args.title}")
    print(f"🗣️ Voice:        {args.voice} (Persian Neural)")
    print(f"🎵 Preserve BGM: {args.keep_bgm}")
    print("="*70 + "\n")

    pipeline = DubbingJobPipeline()
    
    async def cli_progress(percent: int, message: str):
        bar = ("█" * (percent // 5)).ljust(20, "░")
        print(f"\r[{bar}] {percent}% | {message}", end="", flush=True)

    result = await pipeline.run(
        video_url=args.url,
        title=args.title,
        voice_gender=args.voice,
        preserve_bgm=args.keep_bgm,
        progress_callback=cli_progress
    )

    print("\n\n" + "="*70)
    if result.get("success"):
        print("✅ Dubbing Job Completed Successfully!")
        print(f"⏱️ Total Time:  {result.get('elapsed_time_seconds')} seconds")
        print(f"📁 Output Video: {result.get('output_video_path')}")
        if result.get("telegram", {}).get("uploaded"):
            print(f"🌐 Telegram CDN: {result['telegram']['telegram_link']}")
        else:
            print("ℹ️ Telegram upload skipped (Configure TELEGRAM_BOT_TOKEN in .env to enable CDN)")
    else:
        print(f"❌ Dubbing Job Failed: {result.get('error')}")
    print("="*70 + "\n")

if __name__ == "__main__":
    asyncio.run(main())
