"""
RPIM Central API Service (FastAPI / ASGI Engine)
Exposes complete OpenAPI specification, RFC 7807 structured error responses,
internal secret authentication, asynchronous job dispatch (202 Accepted),
Telegram streaming gateway with Range 206, and system health endpoints.
"""

import os
import hmac
import time
import json
import uuid
import hashlib
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response, HTTPException, Header, Depends, status
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import config
from database import DualDatabaseManager
from adapters.discovery import DownloadlyParser
from adapters.telegram import TelegramStorageAdapter
from adapters.publisher import RPIMPublisherAdapter
from adapters.taxonomy import SmartTaxonomyEngine
from adapters.transfer import TransferAdapter

# Configure structured logger
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("rpim.engine")

# Validate production credentials on startup
config.validate_production_credentials()

app = FastAPI(
    title="RPIM Engine API",
    version="2.0.0",
    description="RPIM TV Persian Dubbing, Telegram Gateway, and Publishing Engine API"
)

# CORS: Restricted origins for internal API endpoints in production, open in dev/testing
internal_allowed_origins = config.CORS_ALLOWED_ORIGINS if config.ENV == "production" else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=internal_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)

@app.middleware("http")
async def public_stream_cors_middleware(request: Request, call_next):
    """
    Decoupled CORS policy:
    Ensures public media streaming (/api/v1/stream/{asset_id}) is accessible to any player origin (*),
    while internal API endpoints are restricted to allowed internal domains.
    """
    if request.url.path.startswith("/api/v1/stream/"):
        if request.method == "OPTIONS":
            return Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                    "Access-Control-Allow-Headers": "Range, Content-Range, Accept-Encoding, Accept, Content-Type",
                    "Access-Control-Expose-Headers": "Content-Range, Content-Length, Accept-Ranges, Content-Type",
                }
            )
        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Range, Content-Range, Accept-Encoding, Accept, Content-Type"
        response.headers["Access-Control-Expose-Headers"] = "Content-Range, Content-Length, Accept-Ranges, Content-Type"
        return response
    return await call_next(request)


# Shared Database Manager instance
db = DualDatabaseManager()
telegram_adapter = TelegramStorageAdapter()
publisher_adapter = RPIMPublisherAdapter()
taxonomy_engine = SmartTaxonomyEngine(
    default_tag_count=config.TAXONOMY_DEFAULT_TAG_COUNT,
    max_depth=config.TAXONOMY_MAX_CATEGORY_DEPTH,
    auto_create_enabled=config.TAXONOMY_AUTO_CREATE_ENABLED
)

# Register Admin Dashboard router
import dashboard_api
app.include_router(dashboard_api.router)

# -----------------------------------------------------------------------------
# Embedded Autonomous Background Job Worker Daemon
# -----------------------------------------------------------------------------

def start_embedded_worker():
    import threading
    def _worker_loop():
        from workers.job_runner import PipelineJobWorker
        logger.info("Embedded Pipeline Job Worker started and polling for jobs...")
        worker = PipelineJobWorker(worker_id="embedded_live_worker")
        iteration = 0
        while True:
            try:
                iteration += 1
                if iteration % 60 == 0:
                    try:
                        reclaimed = worker.db.reclaim_expired_job_leases()
                        if reclaimed > 0:
                            logger.info(f"Auto-reclaimed {reclaimed} expired job leases.")
                    except Exception:
                        pass
                processed = worker.process_one_job()
                if not processed:
                    time.sleep(5)
            except Exception as e:
                logger.error(f"Embedded worker error: {e}")
                time.sleep(5)

    t = threading.Thread(target=_worker_loop, daemon=True, name="EmbeddedJobWorker")
    t.start()
    return t

def seed_demo_data_if_empty():
    """
    Ensures that when a new server or container starts with empty databases,
    demonstration data (sample course, chapter, QC-passed lesson, and real audio)
    is seeded so the dashboard is immediately interactive and functional.
    """
    try:
        from database import DualDatabaseManager, Dialect
        from workers.dubbing import PersianDubbingWorker
        db = DualDatabaseManager()
        courses = db.fetchall_processing("SELECT id FROM courses LIMIT 1")
        if courses:
            return

        logger.info("Empty database detected. Seeding demonstration course and dubbed audio...")
        is_pg = getattr(db.processing_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        ins_cmd = "INSERT INTO" if is_pg else "INSERT OR REPLACE INTO"
        now = datetime.now(timezone.utc).isoformat()

        url = "https://downloadly.ir/elearning/video-tutorials/docker-mastery/"
        canonical = url
        slug = "docker-mastery"
        title_fa = "دوره جامع کانتینرسازی و داکر (نسخه دوبله استودیویی)"
        title_en = "Docker Mastery Course"
        fp = "fp_demo_docker_mastery"

        disc_id = db.upsert_discovered_course(
            url=url, canonical_url=canonical, slug=slug, title_en=title_en, title_fa=title_fa, fingerprint=fp, metadata={}
        )
        course_id = db.create_or_update_course(
            discovery_id=disc_id, canonical_url=canonical, slug=slug, title_en=title_en, title_fa=title_fa, fingerprint=fp, total_parts=1
        )

        ch_id = f"ch_{uuid.uuid4().hex[:12]}"
        db.processing_backend.execute(
            f"""
            {ins_cmd} chapters (id, course_id, title_fa, order_index, created_at)
            VALUES ({p}, {p}, 'فصل اول: آشنایی با داکر و مفاهیم اولیه', 1, {p})
            """,
            (ch_id, course_id, now)
        )

        lesson_id = "les_demo_docker_01"
        lesson_work_dir = Path(config.STORAGE_DIR) / "lessons" / lesson_id
        lesson_work_dir.mkdir(parents=True, exist_ok=True)
        persian_audio_path = lesson_work_dir / "persian_audio.wav"

        db.processing_backend.execute(
            f"""
            {ins_cmd} lessons (id, chapter_id, course_id, lesson_number, title_en, title_fa, duration_seconds, status, video_source_path, created_at, updated_at)
            VALUES ({p}, {p}, {p}, 1, 'Introduction to Containers & Docker Engine', 'درس اول: مقدمه بر معماری کانتینرها و موتور داکر', 180.0, 'QC_PASSED', {p}, {p}, {p})
            """,
            (lesson_id, ch_id, course_id, str(lesson_work_dir / "input.mp4"), now, now)
        )

        # Generate audio file
        if not persian_audio_path.exists():
            demo_text = "سلام و درود به همه شما دوستان و دانشجویان گرامی. خیلی خوش اومدید به این دوره آموزشی تخصصی. امروز می‌خوایم با هم مفاهیم پایه‌ای کانتینرسازی و نحوه استقرار سرویس‌ها روی داکر رو از صفر تا صد به صورت کاملاً عملی و گام به گام بررسی کنیم."
            try:
                dubber = PersianDubbingWorker()
                dubber.synthesize_voice(text_fa=demo_text, output_wav=persian_audio_path, voice_name="Puck")
            except Exception as synth_err:
                logger.warning(f"Demo voice synthesis fallback: {synth_err}")
                import struct, math
                sample_rate = 24000
                duration = 3.0
                raw_pcm = bytearray()
                for i in range(int(sample_rate * duration)):
                    val = int(8000 * math.sin(2 * math.pi * 440 * (i / sample_rate)))
                    raw_pcm.extend(struct.pack('<h', val))
                wav_bytes = PersianDubbingWorker._pcm_to_wav(bytes(raw_pcm), sample_rate, 1, 16)
                with open(persian_audio_path, "wb") as f:
                    f.write(wav_bytes)

        db.record_qc_report(
            lesson_id=lesson_id,
            passed=True,
            metrics={"wpm": 134.2, "duration_ratio": 0.98, "terminology_preservation": 1.0, "snr_db": 28.5},
            failure_reasons=[]
        )

        job_id = db.enqueue_job(
            course_id=course_id,
            lesson_id=lesson_id,
            job_type="DUBBING",
            idempotency_key=f"dubbing_demo:{lesson_id}"
        )
        db.processing_backend.execute(
            f"UPDATE dubbing_jobs SET status = 'COMPLETED', progress = 100 WHERE id = {p}",
            (job_id,)
        )
        logger.info(f"Demonstration course, lesson, and dubbed audio seeded successfully ({lesson_id}).")
    except Exception as e:
        logger.error(f"Error seeding demo data: {e}")

if os.environ.get("RPIM_WORKER_EMBEDDED", "1") == "1" and os.environ.get("PYTEST_CURRENT_TEST") is None:
    @app.on_event("startup")
    def on_startup_start_worker():
        try:
            seed_demo_data_if_empty()
        except Exception as seed_err:
            logger.warning(f"Startup demo seed failed: {seed_err}")
        start_embedded_worker()

# -----------------------------------------------------------------------------
# Security & Authentication Middleware
# -----------------------------------------------------------------------------

def verify_internal_secret(
    x_rpim_internal_secret: Optional[str] = Header(None, alias="X-RPIM-Internal-Secret"),
    authorization: Optional[str] = Header(None)
):
    """
    Validates internal API credentials from n8n or peer services.
    If INTERNAL_API_SECRET is configured, requests must provide matching token.
    """
    expected_secret = config.INTERNAL_API_SECRET
    if not expected_secret:
        return True # In local dev/testing without configured secret

    token = x_rpim_internal_secret
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization.split("Bearer ", 1)[1].strip()

    if not token or not hmac.compare_digest(token, expected_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"type": "AUTH_FAILED", "title": "Unauthorized", "detail": "Invalid or missing internal secret."}
        )
    return True


# -----------------------------------------------------------------------------
# Pydantic Schemas
# -----------------------------------------------------------------------------

class DiscoveryUpsertRequest(BaseModel):
    url: str
    title: Optional[str] = None
    title_en: Optional[str] = None
    title_fa: Optional[str] = None
    fingerprint: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

class DiscoveryClaimRequest(BaseModel):
    worker_id: str
    lease_seconds: Optional[int] = 300

class EnqueueExtractRequest(BaseModel):
    discovery_id: str
    url: str
    claim_token: Optional[str] = None
    title_en: Optional[str] = None
    title_fa: Optional[str] = None
    slug: Optional[str] = None
    total_parts: Optional[int] = 1
    archive_parts: Optional[List[Dict[str, Any]]] = None
    instructor: Optional[str] = None
    provider: Optional[str] = None
    description_fa: Optional[str] = None
    thumbnail_url: Optional[str] = None

class DubbingJobRequest(BaseModel):
    course_id: str
    lesson_id: str
    voice: Optional[str] = "Puck"
    callback_url: Optional[str] = None

class UploadTelegramRequest(BaseModel):
    lesson_id: str
    output_media_path: str
    caption: Optional[str] = ""
    duration_seconds: Optional[float] = 0.0

class MarkReviewRequest(BaseModel):
    lesson_id: str
    reasons: Optional[List[str]] = None

class CheckCompletenessRequest(BaseModel):
    course_id: str

class EmitReadyRequest(BaseModel):
    course_id: str
    release_hash: str

class TaxonomyResolveRequest(BaseModel):
    course_id: str
    is_locked: Optional[bool] = False

class StageDraftRequest(BaseModel):
    course_id: str
    release_hash: str

class CommitVisibilityRequest(BaseModel):
    remote_course_id: str
    release_hash: str
    make_public: Optional[bool] = False


# -----------------------------------------------------------------------------
# API Endpoints
# -----------------------------------------------------------------------------

from fastapi.responses import RedirectResponse

@app.get("/", include_in_schema=False, response_class=HTMLResponse)
def root_index():
    html_path = Path(__file__).resolve().parent / "templates" / "dashboard.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"), status_code=200)
    return HTMLResponse("<h1>RPIM Dashboard</h1>", status_code=200)

@app.get("/health")
@app.get("/api/v1/health")
def health_check():
    return {
        "status": "HEALTHY",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "2.0.0"
    }

@app.get("/api/v1/readiness")
def readiness_check():
    # Verify database connections
    try:
        with db.get_discovery_conn() as conn:
            conn.execute("SELECT 1")
        with db.get_processing_conn() as conn:
            conn.execute("SELECT 1")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database readiness failure: {e}")

    return {
        "status": "READY",
        "discovery_db": "CONNECTED",
        "processing_db": "CONNECTED"
    }


# --- Discovery Endpoints (WF01 & WF02) ---

@app.post("/api/v1/discovery/upsert", dependencies=[Depends(verify_internal_secret)])
def discovery_upsert(req: DiscoveryUpsertRequest):
    try:
        canonical = DownloadlyParser.canonicalize_url(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    slug = DownloadlyParser.extract_slug(canonical)
    t_fa = req.title_fa or req.title or slug
    t_en = req.title_en # Strictly None if not supplied
    fp = req.fingerprint or hashlib.sha256(f"{canonical}:{t_fa}".encode()).hexdigest()[:16]

    course_id = db.upsert_discovered_course(
        url=req.url,
        canonical_url=canonical,
        slug=slug,
        title_en=t_en,
        title_fa=t_fa,
        fingerprint=fp,
        metadata=req.metadata or {}
    )
    return {"course_id": course_id, "canonical_url": canonical, "slug": slug, "status": "DISCOVERED"}


@app.post("/api/v1/discovery/claim", dependencies=[Depends(verify_internal_secret)])
def discovery_claim(req: DiscoveryClaimRequest):
    course = db.claim_discovered_course(worker_id=req.worker_id, lease_seconds=req.lease_seconds or 300)
    if not course:
        return JSONResponse(status_code=204, content=None)
    return course


def fetch_course_page(url: str, timeout: int = 15) -> Optional[str]:
    """
    Fetches the HTML of a course page.
    Attempts Iran relay first (if configured) to bypass foreign IP geoblocking,
    then falls back to direct HTTP request.
    """
    import urllib.request
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://downloadly.ir/"
    }
    # 1. Try Iran Relay if configured
    if config.IRAN_RELAY_BASE_URL:
        try:
            adapter = TransferAdapter()
            relay_url = adapter.build_relay_stream_url(url)
            relay_headers = dict(headers)
            if config.RELAY_AUTH_SECRET:
                relay_headers["X-RPIM-Relay-Secret"] = config.RELAY_AUTH_SECRET
            req = urllib.request.Request(relay_url, headers=relay_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            logger.warning(f"Failed to fetch course page via relay: {e}. Trying direct fetch.")

    # 2. Try direct fetch
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        logger.warning(f"Failed to fetch course page directly: {e}")

    return None


@app.post("/api/v1/processing/validate-and-enqueue", dependencies=[Depends(verify_internal_secret)])
def validate_and_enqueue(req: EnqueueExtractRequest):
    canonical = DownloadlyParser.canonicalize_url(req.url)
    slug = req.slug or DownloadlyParser.extract_slug(canonical)
    t_fa = req.title_fa or slug
    t_en = req.title_en or slug
    instructor = req.instructor
    provider = req.provider
    description_fa = req.description_fa
    thumbnail_url = req.thumbnail_url
    parts = req.archive_parts or []

    # Pre-check whether archives exist locally before doing network fetch
    downloads_dir_course = Path(config.STORAGE_DIR) / "downloads" / slug
    has_local_archives = False
    try:
        if downloads_dir_course.exists() and any(
            f.is_file() and f.suffix.lower() in [".rar", ".zip", ".7z"]
            for f in downloads_dir_course.iterdir()
        ):
            has_local_archives = True
    except Exception:
        has_local_archives = False

    # If parts were not provided in payload AND no local archive exists, attempt to fetch course details from page
    if not parts and not has_local_archives:
        html_content = fetch_course_page(canonical)
        if html_content:
            try:
                details = DownloadlyParser.parse_course_details(html_content, canonical)
                if not req.title_fa and details.get("title_fa"):
                    t_fa = details["title_fa"]
                if not req.title_en and details.get("title_en"):
                    t_en = details["title_en"]
                instructor = instructor or details.get("instructor")
                provider = provider or details.get("provider")
                description_fa = description_fa or details.get("description_fa")
                thumbnail_url = thumbnail_url or details.get("thumbnail_url")
                parts = details.get("parts") or []
            except Exception as e:
                logger.error(f"Error parsing course details for {canonical}: {e}")

    total_parts = len(parts) if parts else (req.total_parts or 1)
    fp = hashlib.sha256(f"{canonical}:{t_fa}:{total_parts}".encode()).hexdigest()[:16]

    course_id = db.create_or_update_course(
        discovery_id=req.discovery_id,
        canonical_url=canonical,
        slug=slug,
        title_en=t_en,
        title_fa=t_fa,
        fingerprint=fp,
        total_parts=total_parts,
        instructor=instructor,
        provider=provider,
        description_fa=description_fa,
        thumbnail_url=thumbnail_url
    )

    # Insert parts into archive_parts table
    for p in parts:
        p_num = p.get("part_number", 1)
        f_name = p.get("file_name") or f"{slug}.part{p_num}.rar"
        dl_url = p.get("download_url") or canonical
        exp_size = p.get("expected_size", 0)
        db.insert_archive_part(
            course_id=course_id,
            part_number=p_num,
            file_name=f_name,
            download_url=dl_url,
            expected_size=exp_size
        )

    # Mark the discovery item as enqueued to avoid duplicate claims
    try:
        db.mark_discovered_course_enqueued(req.discovery_id)
    except Exception as e:
        logger.warning(f"Could not mark discovery course {req.discovery_id} as enqueued: {e}")

    # Check whether files already exist locally or download is required
    has_local_archives = False
    for candidate_dir in [Path(config.STORAGE_DIR) / "downloads" / course_id, Path(config.STORAGE_DIR) / "downloads" / slug]:
        try:
            if candidate_dir.exists() and any(
                f.is_file() and f.suffix.lower() in [".rar", ".zip", ".7z"]
                for f in candidate_dir.iterdir()
            ):
                has_local_archives = True
                break
        except Exception:
            pass

    pending_parts = db.get_pending_archive_parts(course_id)
    if pending_parts and not has_local_archives:
        job_id = db.enqueue_job(course_id=course_id, job_type="TRANSFER")
        return {"course_id": course_id, "job_id": job_id, "job_type": "TRANSFER", "status": "QUEUED"}
    elif has_local_archives or db.all_archive_parts_downloaded(course_id):
        job_id = db.enqueue_job(course_id=course_id, job_type="EXTRACTION")
        return {"course_id": course_id, "job_id": job_id, "job_type": "EXTRACTION", "status": "QUEUED"}
    else:
        # Fallback: create default pending part and enqueue TRANSFER
        db.insert_archive_part(
            course_id=course_id,
            part_number=1,
            file_name=f"{slug}.rar",
            download_url=canonical,
            expected_size=0
        )
        job_id = db.enqueue_job(course_id=course_id, job_type="TRANSFER")
        return {"course_id": course_id, "job_id": job_id, "job_type": "TRANSFER", "status": "QUEUED"}


# --- Lesson Processing & Dubbing Endpoints (WF03 - Non-blocking!) ---

@app.post("/api/v1/lessons/dub", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(verify_internal_secret)])
def enqueue_lesson_dubbing(req: DubbingJobRequest):
    """
    CRITICAL REQUIREMENT:
    WF03 must NOT hold an open HTTP request until dubbing finishes!
    Returns HTTP 202 Accepted immediately with job_id.
    Results are retrieved via signed callback or backup polling.
    """
    job_id = db.enqueue_job(
        course_id=req.course_id,
        lesson_id=req.lesson_id,
        job_type="DUBBING",
        idempotency_key=f"dub:{req.course_id}:{req.lesson_id}"
    )
    return {
        "job_id": job_id,
        "status": "QUEUED",
        "lesson_id": req.lesson_id,
        "course_id": req.course_id,
        "message": "Lesson dubbing job enqueued for asynchronous execution.",
        "poll_url": f"/api/v1/jobs/{job_id}"
    }


@app.get("/api/v1/jobs/{job_id}", dependencies=[Depends(verify_internal_secret)])
def get_job_status(job_id: str):
    row = db.fetchone_processing("SELECT * FROM dubbing_jobs WHERE id = ?", (job_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    job = dict(row)
    if job.get("result_data"):
        try:
            job["result_data"] = json.loads(job["result_data"])
        except Exception:
            pass
    return job


@app.post("/api/v1/storage/upload-telegram", dependencies=[Depends(verify_internal_secret)])
def upload_telegram_asset(req: UploadTelegramRequest):
    media_path = Path(req.output_media_path)
    if not media_path.exists():
        raise HTTPException(status_code=400, detail=f"Media file not found: {req.output_media_path}")

    # Record upload intent before calling Telegram
    file_size = media_path.stat().st_size
    sha256 = TelegramStorageAdapter.calculate_file_sha256(media_path) if hasattr(TelegramStorageAdapter, "calculate_file_sha256") else "hash"
    asset_id = f"asset_{uuid.uuid4().hex[:16]}"

    db.record_upload_intent(
        asset_id=asset_id,
        lesson_id=req.lesson_id,
        file_path=str(media_path),
        file_size=file_size,
        sha256=sha256
    )

    # Perform upload with the pre-assigned asset_id (adapter cannot generate a secondary ID!)
    upload_res = telegram_adapter.upload_media_asset(
        media_path=media_path,
        asset_id=asset_id,
        caption=req.caption or "",
        duration_seconds=req.duration_seconds or 0.0
    )

    # Verify and register in database
    db.verify_storage_upload(
        asset_id=upload_res["asset_id"],
        lesson_id=req.lesson_id,
        chat_id=upload_res["chat_id"],
        message_id=upload_res["message_id"],
        file_id=upload_res["file_id"],
        file_unique_id=upload_res["file_unique_id"],
        file_size=upload_res["file_size"],
        sha256=upload_res["sha256"],
        mime_type=upload_res.get("mime_type", "video/mp4"),
        duration_seconds=upload_res.get("duration_seconds", 0.0)
    )

    return upload_res


@app.post("/api/v1/lessons/mark-review", dependencies=[Depends(verify_internal_secret)])
def mark_lesson_review(req: MarkReviewRequest):
    db.record_qc_report(
        lesson_id=req.lesson_id,
        passed=False,
        metrics={},
        failure_reasons=req.reasons or ["Manual QC rejection"]
    )
    return {"status": "NEEDS_REVIEW", "lesson_id": req.lesson_id}


# --- Finalize & Completeness Endpoints (WF04) ---

@app.post("/api/v1/courses/check-completeness", dependencies=[Depends(verify_internal_secret)])
def check_course_completeness(req: CheckCompletenessRequest):
    res = db.check_course_completeness(course_id=req.course_id)
    return res


@app.post("/api/v1/courses/emit-ready", dependencies=[Depends(verify_internal_secret)])
def emit_course_ready(req: EmitReadyRequest):
    now = datetime.now(timezone.utc).isoformat()
    release_id = f"rel_{uuid.uuid4().hex[:12]}"
    with db.processing_transaction() as conn:
        conn.execute("""
            INSERT INTO course_releases (id, course_id, release_hash, manifest_json, status, created_at)
            VALUES (?, ?, ?, '{}', 'READY', ?)
        """, (release_id, req.course_id, req.release_hash, now))
        conn.execute("UPDATE courses SET status = 'READY', updated_at = ? WHERE id = ?", (now, req.course_id))

    return {"status": "READY", "release_id": release_id, "course_id": req.course_id, "release_hash": req.release_hash}


# --- Publishing & Taxonomy Endpoints (WF05) ---

@app.post("/api/v1/publish/taxonomy-resolve", dependencies=[Depends(verify_internal_secret)])
def resolve_taxonomy_endpoint(req: TaxonomyResolveRequest):
    course = db.get_course(req.course_id)
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    existing_tax = db.fetchone_processing("SELECT * FROM course_taxonomy WHERE course_id = ?", (req.course_id,))
    if existing_tax and (existing_tax.get("is_locked") or req.is_locked):
        return {
            "category_id": existing_tax["category_id"],
            "tag_ids": json.loads(existing_tax["tags_json"]) if existing_tax.get("tags_json") else [],
            "status": "LOCKED",
            "evidence": "Locked by database record.",
            "confidence": existing_tax.get("confidence") or 1.0,
            "is_locked": True
        }

    rows_cats = db.fetchall_processing("SELECT * FROM taxonomy_categories")
    rows_tags = db.fetchall_processing("SELECT * FROM taxonomy_tags")
    existing_cats = [dict(r) for r in rows_cats]
    existing_tags = [dict(r) for r in rows_tags]

    resolved = taxonomy_engine.resolve_taxonomy(
        course_data=course,
        existing_categories=existing_cats,
        existing_tags=existing_tags,
        is_locked=req.is_locked or False
    )

    # Save to database
    now = datetime.now(timezone.utc).isoformat()
    tax_id = f"tax_{uuid.uuid4().hex[:12]}"
    db.execute_processing("""
        INSERT INTO course_taxonomy (
            id, course_id, category_id, tags_json, proposed_json,
            status, evidence, confidence, is_locked, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        tax_id, req.course_id, resolved.get("category_id"),
        json.dumps(resolved.get("tag_ids", [])),
        json.dumps(resolved.get("category") or {}),
        resolved.get("status", "RESOLVED"),
        resolved.get("evidence", ""),
        resolved.get("confidence", 1.0),
        1 if req.is_locked else 0,
        now, now
    ))

    return resolved


@app.post("/api/v1/publish/stage-draft", dependencies=[Depends(verify_internal_secret)])
def publish_stage_draft(req: StageDraftRequest):
    course = db.get_course(req.course_id)
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    # Fetch lessons and taxonomy to build staging manifest
    lessons = db.fetchall_processing("SELECT * FROM lessons WHERE course_id = ? ORDER BY lesson_number ASC", (req.course_id,))
    chapters = db.fetchall_processing("SELECT * FROM chapters WHERE course_id = ? ORDER BY order_index ASC", (req.course_id,))
    tax_row = db.fetchone_processing("SELECT * FROM course_taxonomy WHERE course_id = ? ORDER BY created_at DESC LIMIT 1", (req.course_id,))

    tax_dict = None
    if tax_row:
        tax_dict = {
            "category_id": tax_row["category_id"],
            "tag_ids": json.loads(tax_row["tags_json"]) if tax_row.get("tags_json") else []
        }

    stg_manifest = {
        "course_slug": course["slug"],
        "is_sealed": True,
        "release_hash": req.release_hash,
        "total_lessons": len(lessons),
        "chapters": [
            {
                "chapter_title": ch["title_fa"],
                "order_index": ch["order_index"],
                "lessons": [dict(l) for l in lessons if l["chapter_id"] == ch["id"]]
            }
            for ch in chapters
        ] if chapters else [
            {
                "chapter_title": "فصل اول",
                "order_index": 1,
                "lessons": [dict(l) for l in lessons]
            }
        ]
    }

    stage_res = publisher_adapter.stage_draft_release(stg_manifest, course_meta=course, taxonomy=tax_dict)

    # Record publication record in database
    now = datetime.now(timezone.utc).isoformat()
    pub_id = f"pub_{uuid.uuid4().hex[:12]}"
    rel = db.fetchone_processing("SELECT id FROM course_releases WHERE release_hash = ?", (req.release_hash,))
    rel_id = rel["id"] if rel else f"rel_{req.release_hash[:8]}"
    db.execute_processing("""
        INSERT INTO publications (id, release_id, course_id, platform, remote_course_id, visibility, verification_status, published_at)
        VALUES (?, ?, ?, 'RPIM_WEB', ?, 'DRAFT', 'VERIFIED', ?)
    """, (pub_id, rel_id, req.course_id, stage_res["remote_course_id"], now))

    return stage_res


@app.post("/api/v1/publish/commit-visibility", dependencies=[Depends(verify_internal_secret)])
def publish_commit_visibility(req: CommitVisibilityRequest):
    commit_res = publisher_adapter.commit_visibility_switch(
        remote_course_id=req.remote_course_id,
        release_hash=req.release_hash,
        make_public=req.make_public or config.AUTO_PUBLISH_ON_COMPLETE
    )

    # Update publication record in database
    db.execute_processing("""
        UPDATE publications SET visibility = ?, published_at = ? WHERE remote_course_id = ?
    """, (commit_res["visibility"], commit_res["published_at"], req.remote_course_id))

    return commit_res



# --- Recovery & Maintenance Endpoints (WF06) ---

@app.post("/api/v1/recovery/reclaim-expired-leases", dependencies=[Depends(verify_internal_secret)])
def reclaim_expired_leases():
    reclaimed = db.reclaim_expired_job_leases()
    return {"reclaimed_jobs_count": reclaimed, "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/api/v1/recovery/reconcile-outbox", dependencies=[Depends(verify_internal_secret)])
def reconcile_outbox():
    pending = db.get_pending_outbox_events(limit=50)
    reconciled_count = 0
    for evt in pending:
        # Transfer outbox event to processing inbox
        delivered = db.process_inbox_event(
            source_event_id=evt["id"],
            aggregate_id=evt["aggregate_id"],
            event_type=evt["event_type"],
            payload_json=evt["payload_json"]
        )
        if delivered:
            db.mark_outbox_event_sent(evt["id"])
            reconciled_count += 1
    return {"reconciled_events_count": reconciled_count, "pending_count": len(pending)}


@app.post("/api/v1/recovery/dead-letter-summary", dependencies=[Depends(verify_internal_secret)])
def dead_letter_summary():
    dead_jobs = db.fetchall_processing("SELECT * FROM dubbing_jobs WHERE status = 'DEAD_LETTER'")
    dead_events = db.fetchall_discovery("SELECT * FROM discovery_outbox WHERE status = 'DEAD_LETTER'")

    return {
        "dead_jobs_count": len(dead_jobs),
        "dead_events_count": len(dead_events),
        "dead_jobs": [dict(j) for j in dead_jobs],
        "dead_events": [dict(e) for e in dead_events]
    }


# --- Telegram Streaming Gateway with HTTP 206 Range (Public) ---

@app.api_route("/api/v1/stream/{asset_id}", methods=["GET", "HEAD"])
async def stream_media_asset(asset_id: str, request: Request):
    """
    Transparent Local Streaming Gateway for Telegram stored assets.
    Supports RFC 7233 Range headers (bytes=start-end, bytes=start-, bytes=-suffix).
    Returns HTTP 200, 206 Partial Content, or 416 Range Not Satisfiable.
    """
    obj = db.fetchone_processing("SELECT * FROM storage_objects WHERE asset_id = ? OR file_id = ?", (asset_id, asset_id))
    intent = db.fetchone_processing("SELECT * FROM upload_intents WHERE asset_id = ?", (asset_id,))

    # 1. Check local file on disk or cached asset
    file_path = None
    if intent and os.path.exists(intent["file_path"]):
        file_path = Path(intent["file_path"])

    if not file_path or not file_path.exists():
        # Check cache storage directory
        cached_candidates = list(Path(config.CACHE_STORAGE_DIR).glob(f"*{asset_id}*"))
        if cached_candidates and cached_candidates[0].exists():
            file_path = cached_candidates[0]

    # 2. If file missing locally, automatically fetch and cache from Telegram CDN (never 404 for verified assets)
    tg_file_id = obj.get("file_id") if obj else (asset_id if (asset_id.startswith("BAAC") or len(asset_id) > 30) else None)
    if (not file_path or not file_path.exists()) and tg_file_id:
        target_cache_path = Path(config.CACHE_STORAGE_DIR) / f"tg_{asset_id[:32]}.mp4"
        try:
            telegram_adapter.download_file_from_telegram(file_id=tg_file_id, destination_path=target_cache_path)
            if target_cache_path.exists():
                file_path = target_cache_path
        except Exception as e:
            logger.error(f"Failed to retrieve asset {asset_id} from Telegram CDN: {e}")

    if not file_path or not file_path.exists():
        raise HTTPException(status_code=404, detail="Media asset not found on local cache gateway or Telegram storage.")

    file_size = file_path.stat().st_size
    range_header = request.headers.get("Range")

    range_spec = TelegramStorageAdapter.handle_range_request(file_size_or_path=file_size, range_header=range_header)
    status_code = range_spec["status_code"]
    headers = range_spec["headers"]

    if request.method == "HEAD":
        return Response(status_code=status_code, headers=headers)

    if status_code == 416:
        return Response(status_code=416, headers=headers)

    # Stream content
    start = range_spec["start"]
    length = range_spec["length"]
    content_gen = TelegramStorageAdapter.stream_file_range(file_path=file_path, start=start, length=length)

    return StreamingResponse(
        content_gen,
        status_code=status_code,
        headers=headers,
        media_type="video/mp4"
    )
