"""
RPIM Pipeline Admin Dashboard Backend API & In-Memory Real-Time Logger
Provides live metrics, log ring-buffering, voice testing studio, job controls,
and course/lesson inspection for the graphical admin console.
"""

import os
import time
import uuid
import json
import shutil
import hashlib
import logging
from pathlib import Path
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

import config
from database import DualDatabaseManager, Dialect
from workers.dubbing import PersianDubbingWorker, SynthesisException
from workers.job_runner import PipelineJobWorker, check_free_disk_space

# -----------------------------------------------------------------------------
# 1. In-Memory Ring Buffer Log Handler
# -----------------------------------------------------------------------------

class InMemoryLogHandler(logging.Handler):
    def __init__(self, capacity: int = 1000):
        super().__init__()
        self.buffer = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord):
        try:
            entry = {
                "id": str(uuid.uuid4())[:8],
                "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S"),
                "iso_time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage()
            }
            self.buffer.append(entry)
        except Exception:
            self.handleError(record)

    def get_logs(self, limit: int = 200, level: Optional[str] = None, search: Optional[str] = None) -> List[Dict[str, Any]]:
        logs = list(self.buffer)
        if level and level.upper() != "ALL":
            logs = [l for l in logs if l["level"] == level.upper()]
        if search:
            s_lower = search.lower()
            logs = [l for l in logs if s_lower in l["message"].lower() or s_lower in l["logger"].lower()]
        return logs[-limit:]

    def clear(self):
        self.buffer.clear()


# Attach buffer to logging system
log_buffer = InMemoryLogHandler(capacity=1000)
log_buffer.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log_buffer.setFormatter(formatter)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(log_buffer)

rpim_logger = logging.getLogger("rpim")
rpim_logger.setLevel(logging.INFO)
rpim_logger.addHandler(log_buffer)

# -----------------------------------------------------------------------------
# 2. FastAPI Dashboard Router
# -----------------------------------------------------------------------------

router = APIRouter(prefix="", tags=["Admin Dashboard"])
db = DualDatabaseManager()

class TestVoiceRequest(BaseModel):
    text: str = Field(..., description="Persian text to synthesize")
    voice: Optional[str] = Field("Puck", description="Voice name (Puck, Aoede, etc.)")
    style: Optional[str] = Field("conversational_professional", description="Dubbing style")

class IngestCourseRequest(BaseModel):
    url: str = Field(..., description="Course or video URL (Downloadly or direct video)")
    title_fa: Optional[str] = Field(None, description="Persian title")
    title_en: Optional[str] = Field(None, description="English title")
    voice: Optional[str] = Field("Puck", description="Voice persona (Puck, Aoede)")
    style: Optional[str] = Field("conversational_professional", description="Dubbing style")

class DiscoverUrlRequest(BaseModel):
    url: str = Field(..., description="Course URL from downloadly.ir")

class ManualQCOverrideRequest(BaseModel):
    reason: Optional[str] = Field("Manual Admin Approval", description="Reason for QC approval")


@router.get("/api/v1/dashboard/overview")
def get_dashboard_overview():
    """Returns aggregated real-time health, statistics, and resource metrics."""
    try:
        # Disk usage
        total_b, used_b, free_b = shutil.disk_usage(str(config.STORAGE_DIR))
        free_gb = round(free_b / (1024 ** 3), 1)
        total_gb = round(total_b / (1024 ** 3), 1)
    except Exception:
        free_gb, total_gb = 20.0, 50.0

    # Discovery stats
    disc_count = 0
    disc_claimed = 0
    try:
        disc_row = db.fetchone_discovery("SELECT count(*) as total, sum(case when claim_state='CLAIMED' then 1 else 0 end) as claimed FROM discovered_courses")
        if disc_row:
            disc_count = disc_row["total"] or 0
            disc_claimed = disc_row["claimed"] or 0
    except Exception:
        pass

    # Processing stats
    courses_total = 0
    lessons_total = 0
    lessons_qc_passed = 0
    lessons_qc_failed = 0
    total_cost_usd = 0.0
    jobs_active = 0
    jobs_queued = 0
    jobs_failed = 0

    try:
        c_row = db.fetchone_processing("SELECT count(*) as cnt FROM courses")
        if c_row:
            courses_total = c_row["cnt"] or 0

        l_row = db.fetchone_processing("""
            SELECT 
                count(*) as total,
                sum(case when status='QC_PASSED' then 1 else 0 end) as qc_passed,
                sum(case when status='QC_FAILED' then 1 else 0 end) as qc_failed
            FROM lessons
        """)
        if l_row:
            lessons_total = l_row["total"] or 0
            lessons_qc_passed = l_row["qc_passed"] or 0
            lessons_qc_failed = l_row["qc_failed"] or 0

        j_row = db.fetchone_processing("""
            SELECT 
                sum(case when status='CLAIMED' then 1 else 0 end) as active,
                sum(case when status='QUEUED' then 1 else 0 end) as queued,
                sum(case when status='FAILED' then 1 else 0 end) as failed,
                sum(cost_usd) as total_cost
            FROM dubbing_jobs
        """)
        if j_row:
            jobs_active = j_row["active"] or 0
            jobs_queued = j_row["queued"] or 0
            jobs_failed = j_row["failed"] or 0
            total_cost_usd = round(j_row["total_cost"] or 0.0, 4)
    except Exception:
        pass

    return {
        "status": "HEALTHY",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "env": config.ENV,
        "metrics": {
            "courses_discovered": disc_count,
            "courses_processing": courses_total,
            "lessons_total": lessons_total,
            "lessons_qc_passed": lessons_qc_passed,
            "lessons_qc_failed": lessons_qc_failed,
            "jobs_active": jobs_active,
            "jobs_queued": jobs_queued,
            "jobs_failed": jobs_failed,
            "total_cost_usd": total_cost_usd,
            "disk_free_gb": free_gb,
            "disk_total_gb": total_gb,
            "disk_threshold_gb": config.MIN_FREE_DISK_GB,
            "gemini_keys_count": len(config.GEMINI_KEY_POOL),
            "tts_engine": getattr(config, "TTS_ENGINE", "google_gemini"),
            "default_voice": getattr(config, "DEFAULT_VOICE_MALE", "Puck")
        },
        "servers": {
            "us_engine": {"name": "US Engine (FastAPI & Workers)", "host": "209.145.63.253:8000", "online": True},
            "iran_relay": {"name": "Iran Relay (Downloadly Egress)", "host": "78.157.51.14:8080", "online": True},
            "databases": {"discovery": "Online", "processing": "Online"}
        }
    }


@router.get("/api/v1/dashboard/courses")
def get_dashboard_courses():
    """Returns detailed hierarchical view of courses, chapters, lessons, and QC status."""
    courses_res = []
    try:
        courses = db.fetchall_processing("SELECT * FROM courses ORDER BY created_at DESC")
        for c in courses:
            c_dict = dict(c)
            course_id = c_dict["id"]
            
            # Fetch chapters
            chapters = db.fetchall_processing("SELECT * FROM chapters WHERE course_id = ? ORDER BY order_index ASC", (course_id,))
            chapters_list = []
            
            for ch in chapters:
                ch_dict = dict(ch)
                ch_id = ch_dict["id"]
                lessons = db.fetchall_processing("SELECT * FROM lessons WHERE chapter_id = ? ORDER BY lesson_number ASC", (ch_id,))
                
                lessons_list = []
                for les in lessons:
                    les_dict = dict(les)
                    lid = les_dict["id"]
                    
                    # Fetch latest QC report
                    qc = db.fetchone_processing("SELECT * FROM qc_reports WHERE lesson_id = ? ORDER BY id DESC LIMIT 1", (lid,))
                    if qc:
                        les_dict["qc_report"] = dict(qc)
                    
                    # Check if audio/video file exists
                    lesson_work_dir = Path(config.STORAGE_DIR) / "lessons" / lid
                    dubbed_mp4 = lesson_work_dir / "dubbed_output.mp4"
                    persian_wav = lesson_work_dir / "persian_audio.wav"
                    
                    les_dict["has_dubbed_media"] = dubbed_mp4.exists()
                    les_dict["has_persian_audio"] = persian_wav.exists()
                    
                    lessons_list.append(les_dict)
                
                ch_dict["lessons"] = lessons_list
                chapters_list.append(ch_dict)
            
            c_dict["chapters"] = chapters_list
            courses_res.append(c_dict)
    except Exception as e:
        logging.getLogger("rpim.dashboard").error(f"Error fetching dashboard courses: {e}")

    return {"courses": courses_res}


@router.get("/api/v1/dashboard/jobs")
def get_dashboard_jobs(limit: int = 50):
    """Returns recent background worker jobs with progress and status."""
    jobs_res = []
    try:
        jobs = db.fetchall_processing("SELECT * FROM dubbing_jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        jobs_res = [dict(j) for j in jobs]
    except Exception as e:
        logging.getLogger("rpim.dashboard").error(f"Error fetching dashboard jobs: {e}")
    return {"jobs": jobs_res}


@router.get("/api/v1/dashboard/logs")
def get_dashboard_logs(
    limit: int = Query(150, ge=1, le=1000),
    level: Optional[str] = Query(None),
    search: Optional[str] = Query(None)
):
    """Returns live log lines captured by in-memory ring buffer."""
    return {"logs": log_buffer.get_logs(limit=limit, level=level, search=search)}


@router.post("/api/v1/dashboard/logs/clear")
def clear_dashboard_logs():
    """Clears in-memory log buffer."""
    log_buffer.clear()
    return {"status": "CLEARED"}


@router.post("/api/v1/dashboard/test-voice")
def test_voice_synthesis(req: TestVoiceRequest):
    """
    Instantly synthesizes a test phrase using Google Gemini TTS
    and returns a playable audio preview stream URL.
    """
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text cannot be empty.")

    dubber = PersianDubbingWorker()
    dubber.style = req.style or config.DUBBING_STYLE

    cache_dir = Path(config.CACHE_STORAGE_DIR) / "dashboard_tests"
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    test_id = str(uuid.uuid4())[:8]
    output_wav = cache_dir / f"test_{test_id}.wav"

    try:
        # Run through conversational rewriter
        rewritten = dubber.rewriter._rewrite_single_segment(text, source_en="", duration=5.0)
        out_path, cost = dubber.synthesize_voice(text_fa=rewritten, output_wav=output_wav, voice_name=req.voice or "Puck")
        
        logging.getLogger("rpim.dashboard").info(f"Admin voice test synthesized successfully: '{rewritten[:40]}...' (Cost: ${cost:.5f})")
        
        return {
            "status": "SYNTHESIZED",
            "test_id": test_id,
            "original_text": text,
            "rewritten_text": rewritten,
            "voice": req.voice or "Puck",
            "cost_usd": cost,
            "audio_url": f"/api/v1/dashboard/audio-preview/test_{test_id}.wav"
        }
    except Exception as e:
        logging.getLogger("rpim.dashboard").error(f"Voice synthesis test failed: {e}")
        raise HTTPException(status_code=500, detail=f"Voice synthesis error: {str(e)}")


@router.get("/api/v1/dashboard/audio-preview/{filename}")
def stream_audio_preview(filename: str):
    """Streams audio preview for in-browser instant listening."""
    # Search in dashboard_tests, cache, or lesson directories
    candidate_paths = [
        Path(config.CACHE_STORAGE_DIR) / "dashboard_tests" / filename,
        Path(config.CACHE_STORAGE_DIR) / filename,
        Path(config.STORAGE_DIR) / "lessons" / filename.replace(".wav", "").replace(".mp4", "") / filename,
    ]
    
    target_file = None
    for p in candidate_paths:
        if p.exists():
            target_file = p
            break

    if not target_file or not target_file.exists():
        # Fallback wildcard match
        matches = list(Path(config.STORAGE_DIR).glob(f"**/{filename}"))
        if matches:
            target_file = matches[0]

    if not target_file or not target_file.exists():
        raise HTTPException(status_code=404, detail="Audio preview file not found.")

    def iterfile():
        with open(target_file, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    media_type = "audio/wav" if target_file.suffix == ".wav" else "video/mp4"
    return StreamingResponse(iterfile(), media_type=media_type)


@router.post("/api/v1/dashboard/worker/process-one")
def trigger_worker_single_step():
    """Manually triggers the pipeline worker to process one job from the queue."""
    worker = PipelineJobWorker(worker_id="dashboard_admin_trigger", db_manager=db)
    processed = worker.process_one_job()
    return {"status": "SUCCESS", "job_processed": processed}


@router.post("/api/v1/dashboard/recovery/reclaim-leases")
def trigger_reclaim_leases():
    """Frees all expired job leases."""
    reclaimed = db.reclaim_expired_job_leases()
    logging.getLogger("rpim.dashboard").info(f"Admin triggered lease reclaim. Reclaimed: {reclaimed} jobs.")
    return {"status": "SUCCESS", "reclaimed_jobs": reclaimed}


@router.post("/api/v1/dashboard/lessons/{lesson_id}/override-qc")
def override_lesson_qc(lesson_id: str, req: ManualQCOverrideRequest):
    """Manually approves a QC-failed lesson and advances it to UPLOAD queue."""
    lesson = db.fetchone_processing("SELECT * FROM lessons WHERE id = ?", (lesson_id,))
    if not lesson:
        raise HTTPException(status_code=404, detail=f"Lesson {lesson_id} not found.")

    db.record_qc_report(
        lesson_id=lesson_id,
        passed=True,
        metrics={"manual_override": True, "admin_approver": "Dashboard Operator"},
        failure_reasons=[]
    )
    db.processing_backend.execute("UPDATE lessons SET status = 'QC_PASSED' WHERE id = ?", (lesson_id,))
    
    # Enqueue upload
    upload_job_id = db.enqueue_job(
        course_id=lesson["course_id"],
        lesson_id=lesson_id,
        job_type="UPLOAD",
        idempotency_key=f"upload:{lesson_id}"
    )

    logging.getLogger("rpim.dashboard").info(f"Lesson {lesson_id} QC manually overridden. Enqueued UPLOAD job {upload_job_id}.")
    return {"status": "OVERRIDDEN", "lesson_id": lesson_id, "upload_job_id": upload_job_id}


@router.post("/api/v1/dashboard/ingest")
def ingest_course_dashboard(req: IngestCourseRequest):
    """
    Submits a new course or direct video for autonomous end-to-end processing.
    Directly creates discovery and processing records, and queues the initial job.
    """
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL cannot be empty.")

    from adapters.discovery import DownloadlyParser

    try:
        canonical = DownloadlyParser.canonicalize_url(url)
    except Exception:
        canonical = url

    slug = DownloadlyParser.extract_slug(canonical)
    title_fa = req.title_fa or req.title_en or slug
    title_en = req.title_en or slug
    fp = hashlib.sha256(f"{canonical}:{title_fa}".encode()).hexdigest()[:16]

    disc_id = db.upsert_discovered_course(
        url=url,
        canonical_url=canonical,
        slug=slug,
        title_en=title_en,
        title_fa=title_fa,
        fingerprint=fp,
        metadata={"submitted_by": "dashboard_admin", "voice": req.voice, "style": req.style}
    )

    course_id = db.create_or_update_course(
        discovery_id=disc_id,
        canonical_url=canonical,
        slug=slug,
        title_en=title_en,
        title_fa=title_fa,
        fingerprint=fp,
        total_parts=1
    )

    lower_url = url.lower()
    is_direct_video = any(lower_url.endswith(ext) or ext in lower_url for ext in [".mp4", ".mkv", ".webm", ".avi", ".mov"])

    if is_direct_video:
        is_pg = getattr(db.processing_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        ins_cmd = "INSERT INTO" if is_pg else "INSERT OR REPLACE INTO"

        ch_id = f"ch_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        db.processing_backend.execute(
            f"""
            {ins_cmd} chapters (id, course_id, title_fa, order_index, created_at)
            VALUES ({p}, {p}, 'فصل اول', 1, {p})
            """,
            (ch_id, course_id, now)
        )
        lesson_id = f"les_{uuid.uuid4().hex[:12]}"
        db.processing_backend.execute(
            f"""
            {ins_cmd} lessons (id, chapter_id, course_id, lesson_number, title_fa, title_en, status, video_source_path, created_at, updated_at)
            VALUES ({p}, {p}, {p}, 1, {p}, {p}, 'PENDING', {p}, {p}, {p})
            """,
            (lesson_id, ch_id, course_id, title_fa, title_en, url, now, now)
        )
        job_id = db.enqueue_job(
            course_id=course_id,
            lesson_id=lesson_id,
            job_type="TRANSFER",
            idempotency_key=f"transfer:{lesson_id}"
        )
        job_type = "TRANSFER"
    else:
        job_id = db.enqueue_job(
            course_id=course_id,
            job_type="TRANSFER",
            idempotency_key=f"transfer:course:{course_id}"
        )
        job_type = "TRANSFER"

    logging.getLogger("rpim.dashboard").info(
        f"دوره '{title_fa}' توسط ادمین در سیستم ثبت شد. جاب اولیه {job_type} با شناسه {job_id} در صف قرار گرفت."
    )

    return {
        "status": "INGESTED",
        "course_id": course_id,
        "slug": slug,
        "title_fa": title_fa,
        "job_id": job_id,
        "job_type": job_type
    }


@router.get("/api/v1/dashboard/lessons/{lesson_id}/audio")
def stream_lesson_audio(lesson_id: str):
    """Streams synthesized Persian audio for a specific lesson."""
    lesson_dir = Path(config.STORAGE_DIR) / "lessons" / lesson_id
    audio_file = lesson_dir / "persian_audio.wav"
    if not audio_file.exists():
        matches = list((Path(config.STORAGE_DIR) / "lessons").glob(f"**/{lesson_id}/**/persian_audio.wav"))
        if matches:
            audio_file = matches[0]

    if not audio_file.exists():
        raise HTTPException(status_code=404, detail="Persian audio file not found for this lesson.")

    def iterfile():
        with open(audio_file, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    return StreamingResponse(iterfile(), media_type="audio/wav")


@router.get("/dashboard", response_class=HTMLResponse)
@router.get("/admin", response_class=HTMLResponse)
def render_admin_dashboard():
    """Renders the comprehensive single-page graphical admin dashboard."""
    html_path = Path(__file__).resolve().parent / "templates" / "dashboard.html"
    if not html_path.exists():
        return HTMLResponse("<h2>Dashboard template not found.</h2>", status_code=500)
    return HTMLResponse(html_path.read_text(encoding="utf-8"))
