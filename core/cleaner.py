"""
EchoDub Engine - Automated Disk Cleaner
Frees up server storage immediately after job completion to prevent disk exhaustion.
"""

import shutil
import logging
from pathlib import Path

logger = logging.getLogger("EchoDub.Cleaner")

class DiskCleaner:
    @staticmethod
    def cleanup_job_directory(job_dir: Path):
        """
        Safely removes temporary job files (raw audio chunks, stems, intermediate wavs).
        """
        if not job_dir or not job_dir.exists():
            return
            
        try:
            shutil.rmtree(str(job_dir), ignore_errors=True)
            logger.info(f"Cleaned up temporary job workspace: {job_dir}")
        except Exception as e:
            logger.warning(f"Error during job cleanup: {e}")
