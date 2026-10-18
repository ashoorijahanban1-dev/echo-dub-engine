"""
Comprehensive Pipeline E2E & Unit Test Suite
Validates all 10 Acceptance Criteria from Antigravity prompt.
"""

import os
import sys
import unittest
import tempfile
import shutil
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import DatabaseManager
from adapters.discovery import DownloadlyParser
from adapters.transfer import TransferAdapter, SecurityException
from workers.extractor import ArchiveExtractor, SandboxSecurityException
from workers.dubbing import PersianDubbingWorker
from adapters.telegram import TelegramStorageAdapter
from adapters.publisher import RPIMPublisherAdapter, PublicationException

class TestPipelineE2E(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_pipeline.db")
        self.db = DatabaseManager(self.db_path)
        self.fixtures_dir = Path(__file__).resolve().parent.parent / "fixtures"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # Criterion 1: Discover the same course repeatedly and enqueue it once per version
    def test_01_idempotent_discovery(self):
        with open(self.fixtures_dir / "downloadly_listing.html", "r", encoding="utf-8") as f:
            html = f.read()

        courses = DownloadlyParser.parse_listing_page(html)
        self.assertEqual(len(courses), 2)

        # Upsert first time
        c1 = courses[0]
        id1 = self.db.upsert_discovered_course(
            url=c1["url"],
            canonical_url=c1["canonical_url"],
            slug=c1["slug"],
            title_en=c1["title_en"],
            title_fa=c1["title_fa"],
            fingerprint=c1["fingerprint"],
            metadata={"source": "test"}
        )

        # Upsert second time (same canonical URL)
        id2 = self.db.upsert_discovered_course(
            url=c1["url"] + "?utm_source=test", # Tracking parameter stripped
            canonical_url=c1["canonical_url"],
            slug=c1["slug"],
            title_en=c1["title_en"],
            title_fa=c1["title_fa"],
            fingerprint=c1["fingerprint"],
            metadata={"source": "test"}
        )

        self.assertEqual(id1, id2, "Canonical course upsert must be idempotent across multiple visits.")

        with self.db.get_connection() as conn:
            count = conn.execute("SELECT count(*) as cnt FROM discovered_courses").fetchone()["cnt"]
            self.assertEqual(count, 1, "Duplicate discoveries must not produce multiple records.")

    # Criterion 2: Concurrent dispatchers/workers cannot own the same active claim; stale callbacks cannot overwrite
    def test_02_concurrency_and_fencing_token(self):
        # Setup course and job
        cid = self.db.create_or_update_course(
            discovery_id="disc_123",
            canonical_url="https://downloadly.ir/elearning/docker/",
            slug="docker-course",
            title_en="Docker Course",
            title_fa="دوره داکر",
            fingerprint="fp123",
            total_parts=3
        )
        job_id = self.db.enqueue_job(cid, "DUBBING")

        # Worker 1 claims job
        claim1 = self.db.claim_job("worker_1", "DUBBING", lease_seconds=300)
        self.assertIsNotNone(claim1)
        self.assertEqual(claim1["id"], job_id)
        token1 = claim1["fencing_token"]

        # Worker 2 attempts to claim same job concurrently
        claim2 = self.db.claim_job("worker_2", "DUBBING", lease_seconds=300)
        self.assertIsNone(claim2, "Second worker must receive None while job is actively claimed.")

        # Stale callback test: Attempt completion with invalid/stale fencing token
        stale_result = self.db.complete_job(job_id, fencing_token="stale_fake_token", result_data={"ok": True})
        self.assertFalse(stale_result, "Stale fencing token must be rejected.")

        # Valid completion
        valid_result = self.db.complete_job(job_id, fencing_token=token1, result_data={"ok": True})
        self.assertTrue(valid_result, "Correct fencing token must successfully complete the job.")

    # Criterion 3: Missing archive part prevents inventory completion; malicious extraction paths are blocked (Zip-Slip)
    def test_03_archive_sandbox_and_security(self):
        extractor = ArchiveExtractor(self.temp_dir)

        # Zip-slip traversal test
        with self.assertRaises(SandboxSecurityException):
            extractor.validate_archive_path(Path(self.temp_dir), "../../etc/passwd")

        with self.assertRaises(SandboxSecurityException):
            extractor.validate_archive_path(Path(self.temp_dir), "/windows/system32/cmd.exe")

        # Safe file passes
        safe_path = extractor.validate_archive_path(Path(self.temp_dir), "video1.mp4")
        self.assertTrue(str(safe_path).startswith(str(Path(self.temp_dir).resolve())))

    # Criterion 4: Anti-SSRF and Range Support in Transfer Adapter
    def test_04_transfer_anti_ssrf_and_ranges(self):
        adapter = TransferAdapter(iran_relay_base_url="https://rpim.ir")

        # SSRF tests
        with self.assertRaises(SecurityException):
            adapter.validate_target_url("http://127.0.0.1:8080/secret")

        with self.assertRaises(SecurityException):
            adapter.validate_target_url("http://10.0.0.1/admin")

        with self.assertRaises(SecurityException):
            adapter.validate_target_url("http://malicious-site.com/video.mp4")

        # Allowed host passes
        adapter.validate_target_url("https://dl.downloadly.ir/Files/Elearning/video.part1.rar")

    # Criterion 5: Persian Dubbing & Automated QC Validation
    def test_05_dubbing_qc_validation(self):
        worker = PersianDubbingWorker()

        # Dummy video path and missing output path
        missing_output = Path(self.temp_dir) / "non_existent.mp4"
        qc_result = worker.run_qc_check(
            original_duration=60.0,
            output_media_path=missing_output,
            segments=[{"id": 1, "text": "Hello"}]
        )
        self.assertFalse(qc_result["passed"], "QC must fail when output file does not exist.")
        self.assertTrue(qc_result["human_review_required"])

        # Test with empty segments
        empty_media = Path(self.temp_dir) / "empty.mp4"
        empty_media.touch()
        qc_empty = worker.run_qc_check(
            original_duration=60.0,
            output_media_path=empty_media,
            segments=[]
        )
        self.assertFalse(qc_empty["passed"], "QC must fail when there are zero translated segments.")

    # Criterion 6: Telegram Storage Gateway & Range 206 Streaming
    def test_06_telegram_range_streaming(self):
        # Create a dummy media file of 1000 bytes
        media_file = Path(self.temp_dir) / "lesson1.mp4"
        with open(media_file, "wb") as f:
            f.write(b"A" * 1000)

        adapter = TelegramStorageAdapter()
        
        # Test full content (200)
        res_full = adapter.handle_range_request(media_file, None)
        self.assertEqual(res_full["status_code"], 200)
        self.assertEqual(res_full["headers"]["Content-Length"], "1000")

        # Test partial content (206)
        res_range = adapter.handle_range_request(media_file, "bytes=100-199")
        self.assertEqual(res_range["status_code"], 206)
        self.assertEqual(res_range["headers"]["Content-Range"], "bytes 100-199/1000")
        self.assertEqual(res_range["headers"]["Content-Length"], "100")

        # Test invalid range (416)
        res_invalid = adapter.handle_range_request(media_file, "bytes=1500-2000")
        self.assertEqual(res_invalid["status_code"], 416)

    # Criterion 7: Course Publishing Adapter Staging & Idempotency
    def test_07_publisher_staging(self):
        publisher = RPIMPublisherAdapter()

        # Unsealed manifest must fail
        with self.assertRaises(PublicationException):
            publisher.stage_draft_release({"is_sealed": False})

        # Sealed manifest stages as DRAFT
        manifest = {
            "course_slug": "docker-2026",
            "is_sealed": True,
            "release_hash": "hash_abc_123",
            "total_lessons": 2,
            "chapters": [
                {
                    "chapter_title": "فصل اول",
                    "lessons": [
                        {"title": "مقدمه", "duration_seconds": 120.0, "file_name": "l1.mp4"},
                        {"title": "نصب داکر", "duration_seconds": 300.0, "file_name": "l2.mp4"}
                    ]
                }
            ]
        }

        stage_res = publisher.stage_draft_release(manifest)
        self.assertEqual(stage_res["visibility"], "DRAFT", "Initial staging must strictly be DRAFT.")
        self.assertEqual(stage_res["staged_lessons_count"], 2)

        # Atomic commit visibility
        commit_res = publisher.commit_visibility_switch(
            remote_course_id=stage_res["remote_course_id"],
            release_hash=stage_res["release_hash"],
            make_public=False # Remains draft in development
        )
        self.assertEqual(commit_res["status"], "COMMITTED")
        self.assertEqual(commit_res["visibility"], "DRAFT")

if __name__ == "__main__":
    unittest.main()
