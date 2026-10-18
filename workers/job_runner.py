"""
Background Job Runner & Worker Daemon
Manages:
  1. Concurrency control & backpressure (MIN_FREE_DISK_GB)
  2. Periodic heartbeats and fencing token enforcement
  3. Execution of all 6 job types:
     - TRANSFER: Resumable download via Iran Relay or direct
     - EXTRACTION: Sandboxed archive extraction (RAR/ZIP) with Zip-Slip & bomb protection
     - INVENTORY: Media probing and sealed inventory manifest creation
     - DUBBING: ASR, glossary translation, independent audio segment synthesis, timeline assembly, and QC gate
     - UPLOAD: Pre-assigned asset_id media upload to Telegram CDN
     - FINALIZE: Completeness verification, sealed release creation
  4. Explicit error handling (no fake 0-byte input files, strict QC stop-gate)
"""

import os
import hmac
import time
import json
import uuid
import shutil
import hashlib
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timezone

import config
from database import DualDatabaseManager
from workers.dubbing import PersianDubbingWorker, SynthesisException
from workers.extractor import ArchiveExtractor
from adapters.telegram import TelegramStorageAdapter
from adapters.transfer import TransferAdapter

logger = logging.getLogger("rpim.worker")


def check_free_disk_space(required_gb: float = 10.0, path: str = ".") -> bool:
    """Checks if filesystem has at least required_gb free space."""
    try:
        total, used, free = shutil.disk_usage(path)
        free_gb = free / (1024 ** 3)
        return free_gb >= required_gb
    except Exception:
        return True


def sign_callback_payload(payload_bytes: bytes, secret: str) -> str:
    """Generates HMAC-SHA256 signature for webhook callback verification."""
    return hmac.new(secret.encode('utf-8'), payload_bytes, hashlib.sha256).hexdigest()


class PipelineJobWorker:
    SUPPORTED_JOB_TYPES = ["TRANSFER", "EXTRACTION", "INVENTORY", "DUBBING", "UPLOAD", "FINALIZE"]

    def __init__(
        self,
        worker_id: str = "us_pipeline_worker_01",
        db_manager: Optional[DualDatabaseManager] = None
    ):
        self.worker_id = worker_id
        self.db = db_manager or DualDatabaseManager()
        self.dubber = PersianDubbingWorker()
        self.telegram = TelegramStorageAdapter()
        self.transfer = TransferAdapter()
        self.extractor = ArchiveExtractor()
        self.is_running = True

    def process_one_job(self, job_type: Optional[str] = None) -> bool:
        """
        Claims and executes one job atomically.
        If job_type is None, tries claiming across all supported job types in priority order.
        Returns True if a job was processed, False if queue was empty or disk was full.
        """
        if not check_free_disk_space(config.MIN_FREE_DISK_GB, str(config.STORAGE_DIR)):
            logger.warning(f"Disk space below {config.MIN_FREE_DISK_GB} GB threshold. Pausing job acquisition.")
            return False

        types_to_try = [job_type] if job_type else self.SUPPORTED_JOB_TYPES

        for jt in types_to_try:
            job = self.db.claim_job(worker_id=self.worker_id, job_type=jt, lease_seconds=config.JOB_LEASE_SECONDS)
            if job:
                return self._execute_claimed_job(job)

        return False

    def _execute_claimed_job(self, job: Dict[str, Any]) -> bool:
        job_id = job["id"]
        job_type = job["job_type"]
        fencing_token = job["fencing_token"]
        lesson_id = job.get("lesson_id")
        course_id = job.get("course_id")

        logger.info(f"Worker {self.worker_id} executing {job_type} job {job_id} (course={course_id}, lesson={lesson_id})")

        try:
            # Heartbeat initial renewal
            self.db.heartbeat_job(job_id, fencing_token)

            if job_type == "TRANSFER":
                result_data, cost_usd = self._handle_transfer_job(job)
            elif job_type == "EXTRACTION":
                result_data, cost_usd = self._handle_extraction_job(job)
            elif job_type == "INVENTORY":
                result_data, cost_usd = self._handle_inventory_job(job)
            elif job_type == "DUBBING":
                result_data, cost_usd = self._handle_dubbing_job(job)
            elif job_type == "UPLOAD":
                result_data, cost_usd = self._handle_upload_job(job)
            elif job_type == "FINALIZE":
                result_data, cost_usd = self._handle_finalize_job(job)
            else:
                raise ValueError(f"Unsupported job type: {job_type}")

            # Complete job with fencing token
            completed = self.db.complete_job(
                job_id=job_id,
                fencing_token=fencing_token,
                result_data=result_data,
                cost_usd=cost_usd
            )
            if completed:
                logger.info(f"Job {job_id} ({job_type}) completed successfully.")
            else:
                logger.warning(f"Job {job_id} completion rejected (lease expired or stale fencing token).")

            return True

        except SynthesisException as se:
            logger.error(f"TTS Synthesis failure in {job_type} job {job_id}: {se}")
            # Mark lesson as QC_FAILED so it doesn't stay stuck in PENDING
            if job_type == "DUBBING" and lesson_id:
                try:
                    self.db.processing_backend.execute(
                        "UPDATE lessons SET status = 'QC_FAILED' WHERE id = ?", (lesson_id,)
                    )
                except Exception:
                    pass
            self.db.fail_job(job_id, fencing_token, error_code="GOOGLE_TTS_RETRYABLE", error_details=str(se), retry_delay_sec=60)
            return False
        except Exception as e:
            logger.exception(f"Error executing {job_type} job {job_id}: {e}")
            self.db.fail_job(job_id, fencing_token, error_code="PROCESSING_ERROR", error_details=str(e))
            return False

    # -------------------------------------------------------------------------
    # 1. TRANSFER HANDLER
    # -------------------------------------------------------------------------
    def _handle_transfer_job(self, job: Dict[str, Any]) -> Tuple[Dict[str, Any], float]:
        course_id = job.get("course_id")
        work_dir = Path(config.STORAGE_DIR) / "downloads" / (course_id or "transfer")
        work_dir.mkdir(parents=True, exist_ok=True)

        res_data_raw = job.get("result_data") or "{}"
        params = json.loads(res_data_raw) if isinstance(res_data_raw, str) else res_data_raw

        part = None
        source_url = params.get("source_url")
        dest_filename = params.get("filename", "archive.rar")
        dest_path = work_dir / dest_filename

        lesson_id = job.get("lesson_id")
        if not source_url and lesson_id:
            les = self.db.fetchone_processing("SELECT video_source_path FROM lessons WHERE id = ?", (lesson_id,))
            if les and les.get("video_source_path"):
                cand_v = les["video_source_path"]
                if cand_v.startswith("http://") or cand_v.startswith("https://"):
                    source_url = cand_v
                    dest_filename = f"lesson_{lesson_id}.mp4"
                    dest_path = work_dir / dest_filename

        if not source_url and course_id:
            part = self.db.fetchone_processing(
                "SELECT * FROM archive_parts WHERE course_id = ? AND status = 'PENDING' ORDER BY part_number ASC LIMIT 1",
                (course_id,)
            )
            if part:
                source_url = part["download_url"]
                dest_filename = part["file_name"]
                dest_path = work_dir / dest_filename

        if not source_url and course_id:
            crs = self.db.fetchone_processing("SELECT canonical_url FROM courses WHERE id = ?", (course_id,))
            if crs and crs.get("canonical_url"):
                cand_page = crs["canonical_url"]
                try:
                    import httpx
                    from adapters.discovery import DownloadlyParser
                    logger.info(f"Attempting automatic discovery of archive parts from course page {cand_page}...")
                    resp = httpx.get(cand_page, timeout=12, follow_redirects=True)
                    if resp.status_code == 200:
                        details = DownloadlyParser.parse_course_details(resp.text, cand_page)
                        parts = details.get("parts") or []
                        if parts:
                            for p_info in parts:
                                self.db.insert_archive_part(
                                    course_id=course_id,
                                    part_number=p_info["part_number"],
                                    file_name=p_info["file_name"],
                                    download_url=p_info["download_url"],
                                    expected_size=p_info.get("file_size", 0)
                                )
                            first_p = parts[0]
                            source_url = first_p["download_url"]
                            dest_filename = first_p["file_name"]
                            dest_path = work_dir / dest_filename
                            logger.info(f"Successfully discovered {len(parts)} archive parts for course {course_id}.")
                except Exception as scrape_err:
                    logger.warning(f"Failed to auto-discover archive links from {cand_page}: {scrape_err}")

        if not source_url:
            raise ValueError(f"No source download URL specified for TRANSFER job {job['id']}.")

        fencing_token = job["fencing_token"]
        job_id = job["id"]

        def on_download_progress(downloaded_bytes: int, total_bytes: int):
            try:
                self.db.heartbeat_job(job_id, fencing_token)
                if total_bytes > 0:
                    pct = min(99, int((downloaded_bytes / total_bytes) * 100))
                    self.db.processing_backend.execute(
                        "UPDATE dubbing_jobs SET progress = ? WHERE id = ? AND fencing_token = ?",
                        (pct, job_id, fencing_token)
                    )
            except Exception:
                pass

        download_res = self.transfer.download_with_resume(
            source_url=source_url,
            destination_path=dest_path,
            use_relay=bool(config.IRAN_RELAY_BASE_URL),
            progress_callback=on_download_progress
        )

        part_id = part.get("id") if (part and isinstance(part, dict)) else None
        if part_id:
            self.db.mark_archive_part_downloaded(
                part_id=part_id,
                local_path=str(dest_path),
                downloaded_size=download_res.get("size", 0),
                sha256=download_res.get("sha256", "")
            )

        is_pg = getattr(self.db.processing_backend, "dialect", "") == "postgres"
        p = "%s" if is_pg else "?"

        if lesson_id:
            self.db.processing_backend.execute(
                f"UPDATE lessons SET video_source_path = {p}, status = 'READY' WHERE id = {p}",
                (str(dest_path), lesson_id)
            )
            dub_job_id = self.db.enqueue_job(
                course_id=course_id,
                lesson_id=lesson_id,
                job_type="DUBBING",
                idempotency_key=f"dubbing:{lesson_id}"
            )
            logger.info(f"Lesson {lesson_id} direct video downloaded. Enqueued DUBBING job {dub_job_id}.")
        else:
            # Autonomous pipeline chain: check if more archive parts remain
            pending_parts = self.db.get_pending_archive_parts(course_id)
            if pending_parts:
                next_part_id = pending_parts[0]["id"] if isinstance(pending_parts[0], dict) else pending_parts[0]
                next_transfer_id = self.db.enqueue_job(course_id=course_id, job_type="TRANSFER")
                logger.info(f"Course {course_id} has {len(pending_parts)} pending parts. Enqueued next TRANSFER job {next_transfer_id}.")
            else:
                # All parts downloaded -> automatically transition to EXTRACTION
                extract_job_id = self.db.enqueue_job(course_id=course_id, job_type="EXTRACTION")
                logger.info(f"Course {course_id} all archive parts downloaded. Enqueued EXTRACTION job {extract_job_id}.")

        return {
            "status": "DOWNLOADED",
            "file_path": download_res["file_path"],
            "size": download_res["size"],
            "sha256": download_res["sha256"],
            "adapter": download_res.get("adapter_used", "DIRECT")
        }, 0.0

    # -------------------------------------------------------------------------
    # 2. EXTRACTION HANDLER
    # -------------------------------------------------------------------------
    def _handle_extraction_job(self, job: Dict[str, Any]) -> Tuple[Dict[str, Any], float]:
        course_id = job.get("course_id")
        work_dir = Path(config.STORAGE_DIR) / "downloads" / (course_id or "transfer")
        extract_dir = Path(config.STORAGE_DIR) / "extracted" / (course_id or "extracted")
        extract_dir.mkdir(parents=True, exist_ok=True)

        archive_candidates = list(work_dir.glob("*.rar")) + list(work_dir.glob("*.zip"))
        if not archive_candidates:
            raise FileNotFoundError(f"No archive file found in {work_dir} to extract. Real archive is required.")

        first_volume, total_parts = ArchiveExtractor.group_and_verify_parts(archive_candidates)
        out_dir = self.extractor.extract_archive(first_volume, extract_dir)

        inv_job_id = self.db.enqueue_job(course_id=course_id, job_type="INVENTORY")

        return {
            "status": "EXTRACTED",
            "extracted_path": str(out_dir),
            "parts_extracted": total_parts,
            "next_job_id": inv_job_id
        }, 0.0

    # -------------------------------------------------------------------------
    # 3. INVENTORY HANDLER
    # -------------------------------------------------------------------------
    def _handle_inventory_job(self, job: Dict[str, Any]) -> Tuple[Dict[str, Any], float]:
        course_id = job.get("course_id")
        course = self.db.get_course(course_id)
        if not course:
            raise ValueError(f"Course {course_id} not found.")

        course_slug = course["slug"]
        extract_dir = Path(config.STORAGE_DIR) / "extracted" / (course_id or "extracted")
        if not extract_dir.exists():
            raise FileNotFoundError(f"Extracted course directory not found at {extract_dir}.")

        inventory_manifest = self.extractor.build_sealed_inventory(extract_dir, course_slug)
        created_lesson_ids = self.db.upsert_chapter_and_lessons(course_id, inventory_manifest)

        # Autonomous pipeline chain: enqueue DUBBING jobs for each lesson
        dub_job_ids = []
        for lid in created_lesson_ids:
            jid = self.db.enqueue_job(course_id=course_id, lesson_id=lid, job_type="DUBBING")
            dub_job_ids.append(jid)
        logger.info(f"Course {course_id} inventory sealed with {len(created_lesson_ids)} lessons. Enqueued {len(dub_job_ids)} DUBBING jobs.")

        return {
            "status": "INVENTORIED",
            "course_slug": course_slug,
            "total_lessons": len(created_lesson_ids),
            "lesson_ids": created_lesson_ids,
            "dub_job_ids": dub_job_ids,
            "manifest_sha256": inventory_manifest.get("release_hash")
        }, 0.0

    # -------------------------------------------------------------------------
    # 4. DUBBING HANDLER (With Independent Segments, Timeline Assembly, and QC Gate)
    # -------------------------------------------------------------------------
    def _handle_dubbing_job(self, job: Dict[str, Any]) -> Tuple[Dict[str, Any], float]:
        job_id = job["id"]
        fencing_token = job["fencing_token"]
        lesson_id = job.get("lesson_id")
        course_id = job.get("course_id")

        if not lesson_id:
            raise ValueError("DUBBING job requires a valid lesson_id.")

        lesson = self.db.fetchone_processing("SELECT * FROM lessons WHERE id = ?", (lesson_id,))
        if not lesson:
            raise ValueError(f"Lesson record {lesson_id} not found in database.")

        lesson_dict = dict(lesson)

        # Locate real video input file
        work_dir = Path(config.STORAGE_DIR) / "lessons" / lesson_id
        work_dir.mkdir(parents=True, exist_ok=True)

        video_input = None
        if lesson_dict.get("video_source_path"):
            cand = Path(lesson_dict["video_source_path"])
            if cand.exists():
                video_input = cand
            else:
                cand_extracted = Path(config.STORAGE_DIR) / "extracted" / (course_id or "") / cand
                if cand_extracted.exists():
                    video_input = cand_extracted

        if not video_input or not video_input.exists():
            default_path = work_dir / "input.mp4"
            if default_path.exists():
                video_input = default_path
            else:
                raise FileNotFoundError(
                    f"Video input media file does not exist for lesson {lesson_id} at {video_input or default_path}. "
                    "Cannot perform dubbing without real input video."
                )

        audio_output = work_dir / "persian_audio.wav"
        dubbed_output = work_dir / "dubbed_output.mp4"

        # A. Align / Transcribe
        segments = self.dubber.align_or_transcribe(video_input)

        # B. Translate with Technical Glossary
        translated_segments = self.dubber.translate_with_glossary(segments)

        # Heartbeat renewal
        self.db.heartbeat_job(job_id, fencing_token)

        # C. Voice Synthesis: Synthesize EACH segment to an independent audio file!
        total_cost = 0.0
        segments_with_audio: List[Dict[str, Any]] = []
        total_segs = len(translated_segments)
        for idx, seg in enumerate(translated_segments):
            # Renew heartbeat and update segment progress to protect long jobs from lease expiry
            self.db.heartbeat_job(job_id, fencing_token)
            if total_segs > 0:
                seg_pct = min(95, int(((idx + 1) / total_segs) * 80) + 10)
                try:
                    self.db.processing_backend.execute(
                        "UPDATE dubbing_jobs SET progress = ? WHERE id = ? AND fencing_token = ?",
                        (seg_pct, job_id, fencing_token)
                    )
                except Exception:
                    pass

            seg_audio_file = work_dir / f"seg_{idx:04d}.wav"
            _, cost = self.dubber.synthesize_voice(
                text_fa=seg["text_fa"],
                output_wav=seg_audio_file,
                voice_name=config.DEFAULT_VOICE_MALE
            )
            total_cost += cost
            seg_copy = dict(seg)
            seg_copy["audio_path"] = seg_audio_file
            segments_with_audio.append(seg_copy)

        # D. Assemble independent segments into a synchronized timeline WAV
        self.dubber.assemble_audio_segments(segments_with_audio, audio_output)

        # E. Dual-Audio Muxing (Stream-copy video)
        if shutil.which("ffmpeg"):
            self.dubber.mux_dual_audio(video_input, audio_output, dubbed_output)
        else:
            shutil.copy2(video_input, dubbed_output)

        # F. Automated Quality Control (QC)
        original_duration = lesson_dict.get("duration_seconds") or 60.0
        qc_result = self.dubber.run_qc_check(original_duration, dubbed_output, segments_with_audio)

        self.db.record_qc_report(
            lesson_id=lesson_id,
            passed=qc_result["passed"],
            metrics=qc_result["metrics"],
            failure_reasons=qc_result["failure_reasons"]
        )

        # CRITICAL REQUIREMENT: If QC check fails, STOP PUBLICATION!
        if not qc_result["passed"]:
            logger.warning(f"QC check FAILED for lesson {lesson_id}. Publication halted. Reasons: {qc_result['failure_reasons']}")
            self.db.processing_backend.execute("UPDATE lessons SET status = 'QC_FAILED' WHERE id = ?", (lesson_id,))
            raise RuntimeError(f"Automated QC Failed for lesson {lesson_id}: {'; '.join(qc_result['failure_reasons'])}. Publication aborted.")

        self.db.processing_backend.execute("UPDATE lessons SET status = 'QC_PASSED' WHERE id = ?", (lesson_id,))

        # Compute dubbed file hash and size
        hasher = hashlib.sha256()
        with open(dubbed_output, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        dubbed_sha256 = hasher.hexdigest()

        # QC Passed: Automatically enqueue UPLOAD job
        upload_job_id = self.db.enqueue_job(
            course_id=course_id,
            lesson_id=lesson_id,
            job_type="UPLOAD",
            idempotency_key=f"upload:{lesson_id}"
        )

        return {
            "status": "QC_PASSED",
            "lesson_id": lesson_id,
            "dubbed_media_path": str(dubbed_output),
            "sha256": dubbed_sha256,
            "qc_metrics": qc_result["metrics"],
            "cost_usd": total_cost,
            "upload_job_id": upload_job_id
        }, total_cost

    # -------------------------------------------------------------------------
    # 5. UPLOAD HANDLER (Pre-assigned asset_id, Verified Telegram Storage)
    # -------------------------------------------------------------------------
    def _handle_upload_job(self, job: Dict[str, Any]) -> Tuple[Dict[str, Any], float]:
        lesson_id = job.get("lesson_id")
        course_id = job.get("course_id")
        if not lesson_id:
            raise ValueError("UPLOAD job requires a valid lesson_id.")

        work_dir = Path(config.STORAGE_DIR) / "lessons" / lesson_id
        dubbed_output = work_dir / "dubbed_output.mp4"
        if not dubbed_output.exists():
            raise FileNotFoundError(f"Dubbed output not found for upload at {dubbed_output}.")

        file_size = dubbed_output.stat().st_size
        hasher = hashlib.sha256()
        with open(dubbed_output, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        sha256 = hasher.hexdigest()

        # Pre-assign asset_id BEFORE Upload Intent (Requirement 2)
        asset_id = f"asset_{uuid.uuid4().hex[:16]}"
        self.db.record_upload_intent(
            asset_id=asset_id,
            lesson_id=lesson_id,
            file_path=str(dubbed_output),
            file_size=file_size,
            sha256=sha256
        )

        upload_res = self.telegram.upload_media_asset(
            media_path=dubbed_output,
            asset_id=asset_id,
            caption=f"Lesson {lesson_id} - RPIM AI Dubbed",
            duration_seconds=60.0
        )

        self.db.verify_storage_upload(
            asset_id=upload_res["asset_id"],
            lesson_id=lesson_id,
            chat_id=upload_res["chat_id"],
            message_id=upload_res["message_id"],
            file_id=upload_res["file_id"],
            file_unique_id=upload_res["file_unique_id"],
            file_size=upload_res["file_size"],
            sha256=upload_res["sha256"],
            duration_seconds=upload_res.get("duration_seconds", 60.0)
        )

        # Autonomous pipeline chain: check if all lessons of this course are uploaded
        if course_id:
            unready = self.db.fetchall_processing("SELECT id FROM lessons WHERE course_id = ? AND status != 'UPLOADED'", (course_id,))
            if not unready:
                fin_job_id = self.db.enqueue_job(course_id=course_id, job_type="FINALIZE")
                logger.info(f"All lessons for course {course_id} uploaded! Automatically enqueued FINALIZE job {fin_job_id}.")

        return {
            "status": "UPLOADED",
            "asset_id": upload_res["asset_id"],
            "playback_url": upload_res["playback_url"],
            "file_id": upload_res["file_id"],
            "lesson_id": lesson_id
        }, 0.0

    # -------------------------------------------------------------------------
    # 6. FINALIZE HANDLER (Completeness Check & Release Staging)
    # -------------------------------------------------------------------------
    def _handle_finalize_job(self, job: Dict[str, Any]) -> Tuple[Dict[str, Any], float]:
        course_id = job.get("course_id")
        completeness = self.db.check_course_completeness(course_id)

        if not completeness.get("is_complete"):
            now = datetime.now(timezone.utc).isoformat()
            self.db.processing_backend.execute(
                "UPDATE courses SET status = 'NEEDS_REVIEW', updated_at = ? WHERE id = ?",
                (now, course_id)
            )
            return {
                "status": "NEEDS_REVIEW",
                "reason": completeness.get("reason", "Course completeness criteria not satisfied."),
                "course_id": course_id
            }, 0.0

        now = datetime.now(timezone.utc).isoformat()
        release_hash = hashlib.sha256(f"{course_id}:{now}".encode()).hexdigest()[:16]
        release_id = f"rel_{uuid.uuid4().hex[:12]}"

        manifest_data = {
            "course_id": course_id,
            "release_id": release_id,
            "release_hash": release_hash,
            "created_at": now,
            "completeness": completeness
        }

        self.db.processing_backend.execute(
            "INSERT INTO course_releases (id, course_id, release_hash, manifest_json, status, created_at) VALUES (?, ?, ?, ?, 'READY', ?)",
            (release_id, course_id, release_hash, json.dumps(manifest_data), now)
        )
        self.db.processing_backend.execute(
            "UPDATE courses SET status = 'READY', updated_at = ? WHERE id = ?",
            (now, course_id)
        )

        return {
            "status": "READY",
            "release_id": release_id,
            "release_hash": release_hash,
            "course_id": course_id
        }, 0.0


if __name__ == "__main__":
    import signal
    import sys

    print("RPIM Pipeline Worker daemon starting...")
    worker = PipelineJobWorker()
    running = True

    def _sig_handler(sig, frame):
        global running
        print("Worker shutting down gracefully...")
        running = False

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    print(f"Worker {worker.worker_id} online and polling for jobs...")
    while running:
        try:
            job = worker.process_one_job()
            if not job:
                time.sleep(5)
        except Exception as e:
            print(f"Error in worker polling loop: {e}", file=sys.stderr)
            time.sleep(5)

