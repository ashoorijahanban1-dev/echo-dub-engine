"""
EchoDub Engine - RESTful API & WebSocket Streaming Server
FastAPI backend for job submissions, real-time progress, and Telegram CDN links.
"""

import asyncio
import logging
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, HttpUrl
from fastapi import FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware

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

# CORS Middleware (Allows requests from downloadly.ir, frontend dashboard, or internal servers)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-Memory Job State Store & Active WebSockets (Can be connected to Redis in multi-worker production)
active_jobs: Dict[str, Dict[str, Any]] = {}
active_connections: Dict[str, List[WebSocket]] = {}

# Request / Response Schemas
class DubbingRequest(BaseModel):
    video_url: str
    title: Optional[str] = None
    voice_gender: Optional[str] = "male"  # "male" (Farid) or "female" (Dilara)
    preserve_bgm: Optional[bool] = True

class JobStatusResponse(BaseModel):
    job_id: str
    status: str  # "QUEUED", "PROCESSING", "COMPLETED", "FAILED"
    progress: int
    current_stage: str
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

# Background Task Worker Runner
async def execute_dubbing_job(job_id: str, request_data: DubbingRequest):
    active_jobs[job_id]["status"] = "PROCESSING"
    
    async def on_progress(percent: int, message: str):
        active_jobs[job_id]["progress"] = percent
        active_jobs[job_id]["current_stage"] = message
        
        # Broadcast to WebSocket clients
        if job_id in active_connections:
            msg_payload = {"job_id": job_id, "percent": percent, "message": message}
            for ws in active_connections[job_id]:
                try:
                    await ws.send_json(msg_payload)
                except Exception:
                    pass

    pipeline = DubbingJobPipeline(job_id=job_id)
    result = await pipeline.run(
        video_url=request_data.video_url,
        title=request_data.title,
        voice_gender=request_data.voice_gender,
        preserve_bgm=request_data.preserve_bgm,
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
    """Health check endpoint for Coolify and load balancers."""
    return {"status": "ok", "app": settings.APP_NAME, "version": "1.0.0"}

@app.post("/api/v1/dub/submit", response_model=JobStatusResponse, tags=["Dubbing"])
async def submit_dubbing_job(request: DubbingRequest, background_tasks: BackgroundTasks):
    """
    Submits a video URL from downloadly.ir for automated AI dubbing and Telegram CDN distribution.
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

    # Run pipeline asynchronously in background
    background_tasks.add_task(execute_dubbing_job, job_id, request)

    return active_jobs[job_id]

@app.get("/api/v1/dub/status/{job_id}", response_model=JobStatusResponse, tags=["Dubbing"])
async def get_job_status(job_id: str):
    """
    Retrieves real-time processing status, progress percentage, and Telegram CDN link.
    """
    if job_id not in active_jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return active_jobs[job_id]

@app.get("/api/v1/dub/jobs", tags=["Dubbing"])
async def list_recent_jobs():
    """
    Lists all recent dubbing jobs and their statuses.
    """
    return list(active_jobs.values())

@app.websocket("/api/v1/dub/ws/{job_id}")
async def websocket_progress_endpoint(websocket: WebSocket, job_id: str):
    """
    Real-time WebSocket connection streaming logs and progress percentage.
    """
    await websocket.accept()
    if job_id not in active_connections:
        active_connections[job_id] = []
    active_connections[job_id].append(websocket)

    try:
        # Send initial status
        if job_id in active_jobs:
            await websocket.send_json(active_jobs[job_id])
            
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if job_id in active_connections and websocket in active_connections[job_id]:
            active_connections[job_id].remove(websocket)
