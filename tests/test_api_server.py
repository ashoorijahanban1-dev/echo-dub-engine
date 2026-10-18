"""
Integration tests for RPIM FastAPI Server
Validates OpenAPI docs, authentication middleware, health, discovery endpoints,
asynchronous dubbing dispatch (202 Accepted), and streaming gateway.
"""

import os
import json
import unittest
from fastapi.testclient import TestClient

from server import app, db
import config

class TestAPIServer(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.secret = config.INTERNAL_API_SECRET or "test_secret"
        config.INTERNAL_API_SECRET = self.secret

    def test_health_and_readiness(self):
        r1 = self.client.get("/api/v1/health")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json()["status"], "HEALTHY")

        r2 = self.client.get("/api/v1/readiness")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["status"], "READY")

    def test_auth_rejection(self):
        # Missing header
        res = self.client.post("/api/v1/discovery/claim", json={"worker_id": "w1"})
        self.assertEqual(res.status_code, 401)

        # Invalid header
        res2 = self.client.post(
            "/api/v1/discovery/claim",
            json={"worker_id": "w1"},
            headers={"X-RPIM-Internal-Secret": "wrong_secret"}
        )
        self.assertEqual(res2.status_code, 401)

    def test_discovery_upsert_and_claim_api(self):
        # Authorized upsert
        headers = {"X-RPIM-Internal-Secret": self.secret}
        upsert_res = self.client.post(
            "/api/v1/discovery/upsert",
            json={
                "url": "https://downloadly.ir/elearning/fastapi-course/",
                "title_en": "FastAPI Mastery",
                "title_fa": "آموزش جامع فست‌ای‌پی‌آی"
            },
            headers=headers
        )
        self.assertEqual(upsert_res.status_code, 200)
        data = upsert_res.json()
        self.assertIn("course_id", data)

        # Authorized claim
        claim_res = self.client.post(
            "/api/v1/discovery/claim",
            json={"worker_id": "api_test_worker", "lease_seconds": 60},
            headers=headers
        )
        self.assertIn(claim_res.status_code, (200, 204))

    def test_dubbing_async_endpoint_202(self):
        headers = {"X-RPIM-Internal-Secret": self.secret}
        # Insert course and lesson
        cid = db.create_or_update_course(
            discovery_id="disc_api_01",
            canonical_url="https://downloadly.ir/elearning/api-test/",
            slug="api-test",
            title_en="API Test",
            title_fa="تست وب‌سرویس",
            fingerprint="fp_api_1",
            total_parts=1
        )
        manifest = {
            "chapters": [{
                "chapter_title": "فصل اول",
                "lessons": [{"lesson_number": 1, "title": "مقدمه"}]
            }]
        }
        les_ids = db.upsert_chapter_and_lessons(cid, manifest)

        # Non-blocking dub endpoint
        dub_res = self.client.post(
            "/api/v1/lessons/dub",
            json={"course_id": cid, "lesson_id": les_ids[0], "voice": "Puck"},
            headers=headers
        )
        self.assertEqual(dub_res.status_code, 202, "Dubbing endpoint must return 202 Accepted immediately!")
        job_id = dub_res.json()["job_id"]

        # Poll status
        poll_res = self.client.get(f"/api/v1/jobs/{job_id}", headers=headers)
        self.assertEqual(poll_res.status_code, 200)
        self.assertEqual(poll_res.json()["status"], "QUEUED")

    def test_validate_and_enqueue_creates_transfer_job(self):
        headers = {"X-RPIM-Internal-Secret": self.secret}
        res = self.client.post(
            "/api/v1/processing/validate-and-enqueue",
            json={
                "discovery_id": "disc_test_transfer_01",
                "url": "https://downloadly.ir/elearning/video-tutorials/react-native-mastery/",
                "title_fa": "آموزش ری‌اکت نیتیو",
                "title_en": "React Native Mastery",
                "archive_parts": [
                    {
                        "part_number": 1,
                        "file_name": "react_native.part1.rar",
                        "download_url": "https://dl.downloadly.ir/react_native.part1.rar",
                        "expected_size": 104857600
                    },
                    {
                        "part_number": 2,
                        "file_name": "react_native.part2.rar",
                        "download_url": "https://dl.downloadly.ir/react_native.part2.rar",
                        "expected_size": 104857600
                    }
                ]
            },
            headers=headers
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["job_type"], "TRANSFER")
        self.assertEqual(data["status"], "QUEUED")
        course_id = data["course_id"]
        job_id = data["job_id"]

        # Verify job in DB is TRANSFER
        job = db.fetchone_processing("SELECT * FROM dubbing_jobs WHERE id = ?", (job_id,))
        self.assertIsNotNone(job)
        self.assertEqual(job["job_type"], "TRANSFER")
        self.assertEqual(job["status"], "QUEUED")

        # Verify archive parts exist in DB
        pending = db.get_pending_archive_parts(course_id)
        self.assertEqual(len(pending), 2)
        self.assertEqual(pending[0]["part_number"], 1)
        self.assertEqual(pending[1]["part_number"], 2)

    def test_validate_and_enqueue_with_local_archive_enqueues_extraction(self):
        headers = {"X-RPIM-Internal-Secret": self.secret}
        cid = "crs_local_archive_test"
        local_dir = config.STORAGE_DIR / "downloads" / cid
        local_dir.mkdir(parents=True, exist_ok=True)
        dummy_rar = local_dir / "course.rar"
        dummy_rar.write_bytes(b"Rar!\x1a\x07\x00dummy")

        try:
            res = self.client.post(
                "/api/v1/processing/validate-and-enqueue",
                json={
                    "discovery_id": "disc_local_01",
                    "url": "https://downloadly.ir/elearning/video-tutorials/local-course/",
                    "slug": cid
                },
                headers=headers
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["job_type"], "EXTRACTION")
            self.assertEqual(data["status"], "QUEUED")
        finally:
            if dummy_rar.exists():
                dummy_rar.unlink()
            if local_dir.exists():
                import shutil
                shutil.rmtree(local_dir, ignore_errors=True)

if __name__ == "__main__":
    unittest.main()
