import pytest
from fastapi.testclient import TestClient
from server import app
from database import DualDatabaseManager

client = TestClient(app)

def test_dashboard_html_render():
    res = client.get("/dashboard")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "RPIM TV" in res.text
    assert "RPIM TV Engine" in res.text

def test_admin_route_alias():
    res = client.get("/admin")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]

def test_dashboard_overview():
    res = client.get("/api/v1/dashboard/overview")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "HEALTHY"
    assert "metrics" in data
    assert "servers" in data
    assert "disk_free_gb" in data["metrics"]
    assert "total_cost_usd" in data["metrics"]

def test_dashboard_courses():
    res = client.get("/api/v1/dashboard/courses")
    assert res.status_code == 200
    data = res.json()
    assert "courses" in data
    assert isinstance(data["courses"], list)

def test_dashboard_jobs():
    res = client.get("/api/v1/dashboard/jobs")
    assert res.status_code == 200
    data = res.json()
    assert "jobs" in data
    assert isinstance(data["jobs"], list)

def test_dashboard_logs_and_clear():
    # Trigger a log entry
    import logging
    logging.getLogger("rpim.test").info("Dashboard automated test log message")

    res = client.get("/api/v1/dashboard/logs?limit=50")
    assert res.status_code == 200
    data = res.json()
    assert "logs" in data
    assert any("Dashboard automated test log message" in l["message"] for l in data["logs"])

    # Clear logs
    res_clear = client.post("/api/v1/dashboard/logs/clear")
    assert res_clear.status_code == 200
    assert res_clear.json()["status"] == "CLEARED"

def test_dashboard_test_voice_endpoint(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "GEMINI_API_KEY", "mock_key_for_test")

    res = client.post("/api/v1/dashboard/test-voice", json={
        "text": "سلام این یک تست صدای آزمایشی برای داشبورد است.",
        "voice": "Puck",
        "style": "conversational_professional"
    })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "SYNTHESIZED"
    assert "audio_url" in data
    assert "rewritten_text" in data
    assert data["voice"] == "Puck"

def test_dashboard_reclaim_leases():
    res = client.post("/api/v1/dashboard/recovery/reclaim-leases")
    assert res.status_code == 200
    assert res.json()["status"] == "SUCCESS"

def test_root_index_renders_dashboard():
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]

def test_health_endpoints():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "HEALTHY"

def test_dashboard_ingest_course():
    res = client.post("/api/v1/dashboard/ingest", json={
        "url": "https://downloadly.ir/elearning/video-tutorials/docker-mastery-course/",
        "title_fa": "آموزش جامع داکر",
        "title_en": "Docker Mastery",
        "voice": "Puck",
        "style": "conversational_professional"
    })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "INGESTED"
    assert "course_id" in data
    assert "job_id" in data
    assert data["title_fa"] == "آموزش جامع داکر"

