"""
Worker Pipeline Sequential 6-Job Integration Test
Validates the full sequential lifecycle:
  1. TRANSFER -> Downloads real archive
  2. EXTRACTION -> Extracts archive into course filesystem
  3. INVENTORY -> Inspects extracted videos and builds sealed manifest, registers lessons
  4. DUBBING ->
     - Negative test: QC_FAILED halts execution, marks lesson QC_FAILED, stops pipeline
     - Recovery test: Re-executing with passing QC proceeds to audio timeline assembly & muxing
  5. UPLOAD -> Pre-assigns asset_id, uploads to storage, verifies storage_objects
  6. FINALIZE -> Evaluates completeness, generates release, marks course READY
"""

import os
import zipfile
import subprocess
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
import pytest

import config
from database import DualDatabaseManager
from workers.job_runner import PipelineJobWorker


def create_sample_mp4(destination: Path, duration: int = 2):
    """Creates a real minimal MP4 file using ffmpeg if available, or structured media bytes."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("ffmpeg"):
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=1000",
            "-t", str(duration), "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-c:a", "aac", str(destination)
        ]
        res = subprocess.run(cmd, capture_output=True)
        if res.returncode == 0 and destination.exists():
            return
    destination.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"A" * 1024)


def test_full_sequential_worker_pipeline(tmp_path):
    """
    End-to-End Worker Pipeline Integration Test covering:
    TRANSFER -> EXTRACTION -> INVENTORY -> DUBBING -> UPLOAD -> FINALIZE
    """
    storage_root = tmp_path / "storage"
    storage_root.mkdir(parents=True, exist_ok=True)

    orig_storage = config.STORAGE_DIR
    config.STORAGE_DIR = storage_root

    db_path = str(tmp_path / "test_worker_pipeline.db")
    db = DualDatabaseManager(discovery_db_url=f"sqlite:///{db_path}", processing_db_url=f"sqlite:///{db_path}")

    # Create worker with injected test database and test_double enabled for Telegram
    worker = PipelineJobWorker(worker_id="test_runner_01", db_manager=db)
    worker.telegram.test_double = True
    # Use mock TTS key so synthesize_voice bypasses real Gemini API in test environment
    from workers.dubbing import PersianDubbingWorker
    worker.dubber = PersianDubbingWorker(gemini_api_key="mock_test_key_pipeline_e2e")

    try:
        # Step 0: Setup discovered & processing course
        course_id = db.create_or_update_course(
            discovery_id="disc_seq_001",
            canonical_url="https://downloadly.ir/elearning/full-stack-python/",
            slug="full-stack-python",
            title_en="Full Stack Python Mastery",
            title_fa="دوره جامع فول‌استک پایتون",
            fingerprint="fp_seq_123",
            total_parts=1
        )
        assert course_id is not None

        # Prepare real archive containing a real video file
        downloads_dir = storage_root / "downloads" / course_id
        downloads_dir.mkdir(parents=True, exist_ok=True)
        archive_path = downloads_dir / "full_course.zip"

        sample_video_src = tmp_path / "lesson01.mp4"
        create_sample_mp4(sample_video_src, duration=2)

        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.write(sample_video_src, arcname="Chapter 1/01 - Introduction.mp4")

        # ---------------------------------------------------------------------
        # 1. TRANSFER JOB
        # ---------------------------------------------------------------------
        db.processing_backend.execute("""
            INSERT INTO archive_parts (id, course_id, part_number, file_name, expected_size, download_url, status)
            VALUES ('part_seq_01', ?, 1, 'full_course.zip', ?, 'https://dl.downloadly.ir/full_course.zip', 'PENDING')
        """, (course_id, archive_path.stat().st_size))

        transfer_job_id = db.enqueue_job(course_id=course_id, job_type="TRANSFER")

        with patch.object(worker.transfer, "download_with_resume", return_value={
            "status": "DOWNLOADED",
            "file_path": str(archive_path),
            "size": archive_path.stat().st_size,
            "sha256": "dummy_sha256",
            "adapter_used": "DIRECT"
        }):
            ran_transfer = worker.process_one_job("TRANSFER")
            assert ran_transfer is True

        transfer_job = db.fetchone_processing("SELECT * FROM dubbing_jobs WHERE id = ?", (transfer_job_id,))
        assert transfer_job["status"] == "COMPLETED"

        # ---------------------------------------------------------------------
        # 2. EXTRACTION JOB
        # ---------------------------------------------------------------------
        extract_job_id = db.enqueue_job(course_id=course_id, job_type="EXTRACTION")
        ran_extraction = worker.process_one_job("EXTRACTION")
        assert ran_extraction is True

        extract_job = db.fetchone_processing("SELECT * FROM dubbing_jobs WHERE id = ?", (extract_job_id,))
        assert extract_job["status"] == "COMPLETED"

        extracted_dir = storage_root / "extracted" / course_id
        extracted_files = list(extracted_dir.rglob("*.mp4"))
        assert len(extracted_files) >= 1
        assert extracted_files[0].name == "01 - Introduction.mp4"

        # ---------------------------------------------------------------------
        # 3. INVENTORY JOB (Triggered automatically by EXTRACTION)
        # ---------------------------------------------------------------------
        ran_inventory = worker.process_one_job("INVENTORY")
        assert ran_inventory is True

        lessons = db.fetchall_processing("SELECT * FROM lessons WHERE course_id = ? ORDER BY lesson_number ASC", (course_id,))
        assert len(lessons) >= 1
        lesson = lessons[0]
        lesson_id = lesson["id"]
        assert lesson["title_fa"] != ""
        assert lesson["status"] == "PENDING"
        assert (extracted_dir / lesson["video_source_path"]).exists()

        # ---------------------------------------------------------------------
        # 4. DUBBING JOB - NEGATIVE TEST: QC Fails -> Halts pipeline & marks QC_FAILED
        # ---------------------------------------------------------------------
        dub_job_fail_id = db.enqueue_job(course_id=course_id, lesson_id=lesson_id, job_type="DUBBING")

        failing_qc_result = {
            "passed": False,
            "metrics": {"duration_drift_ratio": 0.45, "silence_ratio": 0.50},
            "failure_reasons": ["Duration drift 45% exceeds threshold 15%", "Excessive silence 50%"]
        }

        with patch.object(worker.dubber, "run_qc_check", return_value=failing_qc_result):
            ran_dub_fail = worker.process_one_job("DUBBING")
            assert ran_dub_fail is False # Job failed due to QC error

        failed_job = db.fetchone_processing("SELECT * FROM dubbing_jobs WHERE id = ?", (dub_job_fail_id,))
        assert failed_job["status"] in ("FAILED", "DEAD_LETTER", "RETRY_WAIT")
        
        lesson_record = db.fetchone_processing("SELECT * FROM lessons WHERE id = ?", (lesson_id,))
        assert lesson_record["status"] == "QC_FAILED"

        # Verify no UPLOAD job was enqueued
        pending_uploads = db.fetchall_processing("SELECT * FROM dubbing_jobs WHERE job_type = 'UPLOAD' AND lesson_id = ?", (lesson_id,))
        assert len(pending_uploads) == 0

        # ---------------------------------------------------------------------
        # 4b. RECOVERY / RETRY: Passing QC proceeds and enqueues UPLOAD
        # ---------------------------------------------------------------------
        assert failed_job["attempt_count"] == 1
        assert failed_job["status"] == "RETRY_WAIT"

        # Advance retry time so worker claims the job again on retry
        past_time = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        db.processing_backend.execute(
            "UPDATE dubbing_jobs SET next_retry_at = ? WHERE id = ?",
            (past_time, dub_job_fail_id)
        )

        passing_qc_result = {
            "passed": True,
            "metrics": {"duration_drift_ratio": 0.02, "silence_ratio": 0.05},
            "failure_reasons": []
        }

        with patch.object(worker.dubber, "run_qc_check", return_value=passing_qc_result):
            ran_dub_success = worker.process_one_job("DUBBING")
            assert ran_dub_success is True

        dub_job_success = db.fetchone_processing("SELECT * FROM dubbing_jobs WHERE id = ?", (dub_job_fail_id,))
        assert dub_job_success["status"] == "COMPLETED"
        assert dub_job_success["attempt_count"] == 2

        lesson_record_ok = db.fetchone_processing("SELECT * FROM lessons WHERE id = ?", (lesson_id,))
        assert lesson_record_ok["status"] == "QC_PASSED"

        # ---------------------------------------------------------------------
        # 5. UPLOAD JOB (Auto-enqueued by Dubbing on QC pass)
        # ---------------------------------------------------------------------
        ran_upload = worker.process_one_job("UPLOAD")
        assert ran_upload is True

        stg = db.fetchone_processing("SELECT * FROM storage_objects WHERE lesson_id = ?", (lesson_id,))
        assert stg is not None
        assert stg["status"] == "VERIFIED"
        assert stg["asset_id"].startswith("asset_")

        # ---------------------------------------------------------------------
        # 6. FINALIZE JOB
        # ---------------------------------------------------------------------
        finalize_job_id = db.enqueue_job(course_id=course_id, job_type="FINALIZE")
        ran_finalize = worker.process_one_job("FINALIZE")
        assert ran_finalize is True

        finalize_job = db.fetchone_processing("SELECT * FROM dubbing_jobs WHERE id = ?", (finalize_job_id,))
        assert finalize_job["status"] == "COMPLETED"

        course_record = db.fetchone_processing("SELECT * FROM courses WHERE id = ?", (course_id,))
        assert course_record["status"] == "READY"

        release = db.fetchone_processing("SELECT * FROM course_releases WHERE course_id = ?", (course_id,))
        assert release is not None
        assert release["release_hash"] != ""
        assert release["status"] == "READY"

    finally:
        config.STORAGE_DIR = orig_storage


def test_job_retry_and_dead_letter(tmp_path):
    """
    Tests job retry backoff, attempt counting, and eventual DEAD_LETTER transition
    when maximum attempts are exceeded.
    """
    db_path = str(tmp_path / "test_retry.db")
    db = DualDatabaseManager(discovery_db_url=f"sqlite:///{db_path}", processing_db_url=f"sqlite:///{db_path}")

    course_id = db.create_or_update_course(
        discovery_id="disc_retry_01",
        canonical_url="https://downloadly.ir/retry-test/",
        slug="retry-test",
        title_en="Retry Test",
        title_fa="تست تلاش مجدد",
        fingerprint="fp_retry_1"
    )

    job_id = db.enqueue_job(course_id=course_id, job_type="TRANSFER")

    # Attempt 1: Claim and fail
    job = db.claim_job("w1", "TRANSFER")
    assert job is not None
    assert job["id"] == job_id
    assert job["attempt_count"] == 1

    db.fail_job(job_id, job["fencing_token"], error_code="NET_ERR", error_details="Timeout", retry_delay_sec=60)
    j1 = db.fetchone_processing("SELECT * FROM dubbing_jobs WHERE id = ?", (job_id,))
    assert j1["status"] == "RETRY_WAIT"
    assert j1["attempt_count"] == 1

    # Before next_retry_at: Cannot be claimed
    assert db.claim_job("w1", "TRANSFER") is None

    # Advance time to allow Attempt 2
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    db.processing_backend.execute("UPDATE dubbing_jobs SET next_retry_at = ? WHERE id = ?", (past, job_id))

    job2 = db.claim_job("w1", "TRANSFER")
    assert job2 is not None
    assert job2["attempt_count"] == 2
    db.fail_job(job_id, job2["fencing_token"], error_code="NET_ERR", error_details="Timeout 2", retry_delay_sec=60)
    j2 = db.fetchone_processing("SELECT * FROM dubbing_jobs WHERE id = ?", (job_id,))
    assert j2["status"] == "RETRY_WAIT"
    assert j2["attempt_count"] == 2

    # Advance time to allow Attempt 3 (Final attempt, max_attempts=3)
    db.processing_backend.execute("UPDATE dubbing_jobs SET next_retry_at = ? WHERE id = ?", (past, job_id))
    job3 = db.claim_job("w1", "TRANSFER")
    assert job3 is not None
    assert job3["attempt_count"] == 3
    db.fail_job(job_id, job3["fencing_token"], error_code="FATAL_ERR", error_details="Max retries reached")

    # Verify transition to DEAD_LETTER
    j3 = db.fetchone_processing("SELECT * FROM dubbing_jobs WHERE id = ?", (job_id,))
    assert j3["status"] == "DEAD_LETTER"
    assert j3["attempt_count"] == 3
    assert j3["error_code"] == "FATAL_ERR"
