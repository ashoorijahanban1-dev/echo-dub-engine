"""
EchoDub Engine - RESTful API & WebSocket Streaming Server
FastAPI backend for job submissions, file uploads, real-time progress, and Telegram CDN links.
"""

import os
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from pydantic import BaseModel
from fastapi import FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
import aiofiles

from config import settings
from pipeline import DubbingJobPipeline

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("EchoDub.API")

app = FastAPI(
    title=settings.APP_NAME,
    description="Automated AI Video Dubbing & Telegram CDN Pipeline for Educational Content",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

active_jobs: Dict[str, Dict[str, Any]] = {}
active_connections: Dict[str, List[WebSocket]] = {}

class DubbingRequest(BaseModel):
    video_url: str
    title: Optional[str] = None
    voice_gender: Optional[str] = "male"
    preserve_bgm: Optional[bool] = True

class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    progress: int
    current_stage: str
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

# Background Task Worker Runner (URL-based or Local-file-based)
async def execute_dubbing_job(job_id: str, video_source: str, title: Optional[str], voice_gender: str, preserve_bgm: bool, is_local_file: bool = False):
    active_jobs[job_id]["status"] = "PROCESSING"
    
    async def on_progress(percent: int, message: str):
        active_jobs[job_id]["progress"] = percent
        active_jobs[job_id]["current_stage"] = message
        
        if job_id in active_connections:
            msg_payload = {"job_id": job_id, "percent": percent, "message": message}
            for ws in active_connections[job_id]:
                try:
                    await ws.send_json(msg_payload)
                except Exception:
                    pass

    pipeline = DubbingJobPipeline(job_id=job_id)
    
    if is_local_file:
        # If video was already uploaded directly from Iran server
        result = await pipeline.run_from_local_file(
            local_video_path=Path(video_source),
            title=title,
            voice_gender=voice_gender,
            preserve_bgm=preserve_bgm,
            progress_callback=on_progress
        )
    else:
        result = await pipeline.run(
            video_url=video_source,
            title=title,
            voice_gender=voice_gender,
            preserve_bgm=preserve_bgm,
            progress_callback=on_progress
        )

    if result.get("success"):
        active_jobs[job_id]["status"] = "COMPLETED"
        active_jobs[job_id]["result"] = result
    else:
        active_jobs[job_id]["status"] = "FAILED"
        active_jobs[job_id]["error"] = result.get("error")

# ==============================================================================
# API Endpoints
# ==============================================================================

@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "ok", "app": settings.APP_NAME, "version": "1.0.0"}

@app.post("/api/v1/dub/submit", response_model=JobStatusResponse, tags=["Dubbing"])
async def submit_dubbing_job(request: DubbingRequest, background_tasks: BackgroundTasks):
    """
    Submits a video URL for automated AI dubbing.
    """
    job_id = f"job_{int(asyncio.get_event_loop().time() * 1000)}"
    
    active_jobs[job_id] = {
        "job_id": job_id,
        "status": "QUEUED",
        "progress": 0,
        "current_stage": "Job queued in background worker...",
        "result": None,
        "error": None
    }

    background_tasks.add_task(
        execute_dubbing_job,
        job_id,
        request.video_url,
        request.title,
        request.voice_gender,
        request.preserve_bgm,
        False
    )

    return active_jobs[job_id]

@app.post("/api/v1/dub/upload", response_model=JobStatusResponse, tags=["Dubbing"])
async def upload_and_dub_video(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    voice_gender: Optional[str] = Form("male"),
    preserve_bgm: Optional[bool] = Form(True)
):
    """
    Directly receives a video file downloaded by Iran Server (bypassing Geo-IP blocks) and processes it.
    """
    job_id = f"job_up_{int(asyncio.get_event_loop().time() * 1000)}"
    job_dir = settings.TEMP_DIR / job_id
    os.makedirs(job_dir, exist_ok=True)
    
    saved_file_path = job_dir / file.filename
    
    # Save uploaded file
    async with aiofiles.open(saved_file_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            await f.write(chunk)

    active_jobs[job_id] = {
        "job_id": job_id,
        "status": "QUEUED",
        "progress": 0,
        "current_stage": "Video received from Iran server. Queuing AI dubbing...",
        "result": None,
        "error": None
    }

    background_tasks.add_task(
        execute_dubbing_job,
        job_id,
        str(saved_file_path),
        title or file.filename,
        voice_gender,
        preserve_bgm,
        True
    )

    return active_jobs[job_id]

@app.get("/api/v1/dub/status/{job_id}", response_model=JobStatusResponse, tags=["Dubbing"])
async def get_job_status(job_id: str):
    if job_id not in active_jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return active_jobs[job_id]

@app.get("/api/v1/dub/jobs", tags=["Dubbing"])
async def list_recent_jobs():
    return list(active_jobs.values())

@app.websocket("/api/v1/dub/ws/{job_id}")
async def websocket_progress_endpoint(websocket: WebSocket, job_id: str):
    await websocket.accept()
    if job_id not in active_connections:
        active_connections[job_id] = []
    active_connections[job_id].append(websocket)

    try:
        if job_id in active_jobs:
            await websocket.send_json(active_jobs[job_id])
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if job_id in active_connections and websocket in active_connections[job_id]:
            active_connections[job_id].remove(websocket)
