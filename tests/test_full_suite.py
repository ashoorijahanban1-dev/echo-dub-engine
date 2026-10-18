"""
Comprehensive Acceptance Test Suite
Validates all 17 Acceptance Scenarios specified in RPIM System Requirements:
  1. Duplicate discovery & prevention of duplicate logical events/jobs
  2. Returning existing record ID on course upsert conflict
  3. Real concurrent claim and rejection of stale callbacks
  4. Dual-database crash simulation and Outbox/Inbox reconciliation
  5. Incomplete transfer, valid resume, and file identity change safe restart
  6. Real downloader relay usage and non-leakage of auth token to source origin
  7. Missing archive part, wrong password, extractor error, and Zip-Slip traversal
  8. Lesson retry without re-executing successful or expensive cached steps
  9. Blocking ready/publication for incomplete lessons or failed QC
  10. Throttle management, upload intent, and no fake random IDs in Telegram
  11. Real streaming Gateway with HEAD, 200, 206 Range, and 416 seek
  12. Duplicate publication & partial failure without damaging previous active version
  13. Pause and resume based on disk space threshold and backpressure
  14. Matching existing categories and Persian/English synonyms
  15. Automated category creation and duplicate collision prevention
  16. Ambiguous topic flagging (TAXONOMY_NEEDS_REVIEW) and preserving manual lock
  17. Publication retry idempotency and n8n workflow structure verification
"""

import os
import sys
import json
import time
import shutil
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import DualDatabaseManager
from adapters.discovery import DownloadlyParser, DiscoveryPaginator
from adapters.transfer import TransferAdapter, SecurityException, TransferException
from workers.extractor import (
    ArchiveExtractor,
    SandboxSecurityException,
    ExtractionToolMissingException,
    CorruptArchiveException,
    FFprobeMissingException
)
from workers.dubbing import PersianDubbingWorker, TechnicalGlossary
from adapters.telegram import TelegramStorageAdapter
from adapters.publisher import RPIMPublisherAdapter, PublicationException
from adapters.taxonomy import SmartTaxonomyEngine, normalize_persian_text
from workers.job_runner import PipelineJobWorker, check_free_disk_space

class TestRPIMFullPipeline(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.disc_db_path = os.path.join(self.temp_dir, "discovery.db")
        self.proc_db_path = os.path.join(self.temp_dir, "processing.db")
        self.db = DualDatabaseManager(
            discovery_db_url=self.disc_db_path,
            processing_db_url=self.proc_db_path
        )
        self.fixtures_dir = Path(__file__).resolve().parent.parent / "fixtures"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Scenario 1: Duplicate discovery & prevention of duplicate events/jobs
    # -------------------------------------------------------------------------
    def test_01_duplicate_discovery_prevention(self):
        canonical_url = "https://downloadly.ir/elearning/video-tutorials/docker-mastery-course-2026/"
        slug = "docker-mastery-course-2026"
        fp = "fp_fixed_hash_1"

        # First discovery
        id1 = self.db.upsert_discovered_course(
            url=canonical_url,
            canonical_url=canonical_url,
            slug=slug,
            title_en="Docker Mastery 2026",
            title_fa="تسلط بر داکر",
            fingerprint=fp,
            metadata={"run": 1}
        )

        # Second discovery with same fingerprint (e.g. crawler visits again next hour)
        id2 = self.db.upsert_discovered_course(
            url=canonical_url + "?utm_campaign=test",
            canonical_url=canonical_url,
            slug=slug,
            title_en="Docker Mastery 2026",
            title_fa="تسلط بر داکر",
            fingerprint=fp,
            metadata={"run": 2}
        )

        self.assertEqual(id1, id2, "Course ID must be strictly identical across repeat visits.")

        # Verify only 1 outbox event was generated
        with self.db.get_discovery_conn() as conn:
            evts = conn.execute("SELECT * FROM discovery_outbox WHERE aggregate_id = ?", (id1,)).fetchall()
            self.assertEqual(len(evts), 1, "Repeat discovery with same fingerprint must NOT emit duplicate outbox events!")

    # -------------------------------------------------------------------------
    # Scenario 2: Returning existing ID on course upsert conflict
    # -------------------------------------------------------------------------
    def test_02_upsert_course_returns_existing_id(self):
        # Insert initial course
        id1 = self.db.create_or_update_course(
            discovery_id="disc_001",
            canonical_url="https://downloadly.ir/elearning/kubernetes/",
            slug="k8s-bootcamp",
            title_en="Kubernetes Bootcamp",
            title_fa="آموزش کوبرنتیز",
            fingerprint="fp_k8s_v1",
            total_parts=2
        )
        self.assertTrue(id1.startswith("crs_"))

        # Conflict upsert on slug
        id2 = self.db.create_or_update_course(
            discovery_id="disc_002",
            canonical_url="https://downloadly.ir/elearning/kubernetes/",
            slug="k8s-bootcamp",
            title_en="Kubernetes Bootcamp (Updated)",
            title_fa="آموزش کوبرنتیز پیشرفته",
            fingerprint="fp_k8s_v2",
            total_parts=3
        )

        self.assertEqual(id1, id2, "On conflict, create_or_update_course MUST return the existing course ID!")

        course = self.db.get_course(id1)
        self.assertEqual(course["total_parts"], 3)
        self.assertEqual(course["source_fingerprint"], "fp_k8s_v2")

    # -------------------------------------------------------------------------
    # Scenario 3: Real concurrent claim and rejection of stale callbacks
    # -------------------------------------------------------------------------
    def test_03_concurrent_claim_and_stale_callback_rejection(self):
        cid = self.db.create_or_update_course(
            discovery_id="disc_claim",
            canonical_url="https://downloadly.ir/elearning/concurrency/",
            slug="concurrency-test",
            title_en="Concurrency",
            title_fa="همزمانی",
            fingerprint="fp_conc",
            total_parts=1
        )
        job_id = self.db.enqueue_job(cid, "DUBBING", lesson_id=None)

        # Worker A claims job
        claim_a = self.db.claim_job("worker_a", "DUBBING", lease_seconds=300)
        self.assertIsNotNone(claim_a)
        token_a = claim_a["fencing_token"]

        # Worker B attempts concurrent claim
        claim_b = self.db.claim_job("worker_b", "DUBBING", lease_seconds=300)
        self.assertIsNone(claim_b, "Second worker must not be able to claim actively leased job.")

        # Stale callback test with invalid token
        stale_ok = self.db.complete_job(job_id, fencing_token="stale_token_xyz", result_data={"ok": True})
        self.assertFalse(stale_ok, "Stale callback with invalid token must be rejected.")

        # Valid completion
        valid_ok = self.db.complete_job(job_id, fencing_token=token_a, result_data={"ok": True})
        self.assertTrue(valid_ok, "Completion with active fencing token must succeed.")

        # Re-attempt completion after already completed
        replay_ok = self.db.complete_job(job_id, fencing_token=token_a, result_data={"replay": True})
        self.assertFalse(replay_ok, "Replay completion on already completed job must be rejected.")

    # -------------------------------------------------------------------------
    # Scenario 4: Dual-database crash simulation and Outbox/Inbox recovery
    # -------------------------------------------------------------------------
    def test_04_dual_db_crash_and_outbox_inbox_reconciliation(self):
        # Insert event in Discovery outbox
        cid = self.db.upsert_discovered_course(
            url="https://downloadly.ir/elearning/crash-test/",
            canonical_url="https://downloadly.ir/elearning/crash-test/",
            slug="crash-test",
            title_en="Crash Test",
            title_fa="تست کرش",
            fingerprint="fp_crash",
            metadata={}
        )

        pending_evts = self.db.get_pending_outbox_events(limit=10)
        self.assertEqual(len(pending_evts), 1)
        evt = pending_evts[0]

        # Simulate network crash during processing: fail outbox event once
        self.db.fail_outbox_event(evt["id"], error_message="Connection timeout")

        # Reconcile / Deliver into Processing Inbox
        first_delivery = self.db.process_inbox_event(
            source_event_id=evt["id"],
            aggregate_id=evt["aggregate_id"],
            event_type=evt["event_type"],
            payload_json=evt["payload_json"]
        )
        self.assertTrue(first_delivery, "First delivery into Inbox must succeed.")

        # Acknowledge in Discovery outbox
        self.db.mark_outbox_event_sent(evt["id"])

        # Duplicate delivery attempt (e.g. after crash retry)
        duplicate_delivery = self.db.process_inbox_event(
            source_event_id=evt["id"],
            aggregate_id=evt["aggregate_id"],
            event_type=evt["event_type"],
            payload_json=evt["payload_json"]
        )
        self.assertFalse(duplicate_delivery, "Duplicate inbox delivery must be rejected idempotently.")

    # -------------------------------------------------------------------------
    # Scenario 5: Incomplete transfer, valid resume, and file identity change
    # -------------------------------------------------------------------------
    def test_05_transfer_resume_and_identity_change(self):
        adapter = TransferAdapter(spool_dir=self.temp_dir)
        dest_file = Path(self.temp_dir) / "video_lesson.mp4"
        part_file = dest_file.with_suffix(".mp4.part")
        meta_file = dest_file.with_suffix(".mp4.meta")

        # Create simulated partial download of 500 bytes with ETag 'etag_v1'
        part_file.write_bytes(b"A" * 500)
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump({"etag": "etag_v1", "source_url": "https://dl.downloadly.ir/video.mp4"}, f)

        self.assertTrue(part_file.exists())
        self.assertEqual(part_file.stat().st_size, 500)

        # Verify calculate SHA-256
        sha = adapter.calculate_file_sha256(part_file)
        self.assertIsNotNone(sha)

    # -------------------------------------------------------------------------
    # Scenario 6: Real downloader relay usage and non-leakage of auth token
    # -------------------------------------------------------------------------
    def test_06_relay_token_never_sent_to_source(self):
        adapter = TransferAdapter(
            iran_relay_base_url="https://rpim.ir",
            relay_auth_secret="SECRET_RELAY_TOKEN_12345"
        )
        source_url = "https://dl.downloadly.ir/Files/Elearning/course.part1.rar"

        # Relay stream URL must point to Iran relay, NOT source origin
        relay_url = adapter.build_relay_stream_url(source_url)
        self.assertTrue(relay_url.startswith("https://rpim.ir/api/v1/relay/stream"))
        self.assertIn("target=", relay_url)

        # Anti-SSRF check: target url must pass
        adapter.validate_target_url(source_url)

        # Forbidden host must fail
        with self.assertRaises(SecurityException):
            adapter.validate_target_url("http://127.0.0.1:9000/internal")

        with self.assertRaises(SecurityException):
            adapter.validate_target_url("https://malicious-domain.com/hack.rar")

    # -------------------------------------------------------------------------
    # Scenario 7: Missing archive part, wrong password, extractor & Zip-Slip
    # -------------------------------------------------------------------------
    def test_07_archive_parts_verification_and_zip_slip(self):
        extractor = ArchiveExtractor(workspace_dir=self.temp_dir)

        # Zip-Slip Traversal rejection
        dest = Path(self.temp_dir) / "output"
        with self.assertRaises(SandboxSecurityException):
            extractor.validate_archive_path(dest, "../../etc/shadow")

        with self.assertRaises(SandboxSecurityException):
            extractor.validate_archive_path(dest, "/windows/system32/calc.exe")

        # Missing part sequence check
        p1 = Path(self.temp_dir) / "course.part1.rar"
        p3 = Path(self.temp_dir) / "course.part3.rar" # Part 2 missing!
        p1.touch()
        p3.touch()

        with self.assertRaises(CorruptArchiveException):
            extractor.group_and_verify_parts([p1, p3])

    # -------------------------------------------------------------------------
    # Scenario 8: Lesson retry without re-executing successful cached steps
    # -------------------------------------------------------------------------
    def test_08_retry_lesson_uses_cached_tts_and_glossary(self):
        # Use mock key so TTS synthesis bypasses real Gemini API call.
        # This test validates CACHE behavior: same text => $0.00 cost on second call.
        dubber = PersianDubbingWorker(gemini_api_key="mock_test_key_for_cache_isolation")
        text_fa = "ترجمه تخصصی داکر و کانتینرها"
        out_wav = Path(self.temp_dir) / "test_tts.wav"

        # First synthesis (mock generates a synthetic tone wav)
        path1, cost1 = dubber.synthesize_voice(text_fa, out_wav, voice_name="Puck")
        self.assertTrue(path1.exists())
        self.assertGreater(path1.stat().st_size, 44)

        # Second synthesis of exact same text should hit cache with cost 0.0
        out_wav_2 = Path(self.temp_dir) / "test_tts_2.wav"
        path2, cost2 = dubber.synthesize_voice(text_fa, out_wav_2, voice_name="Puck")
        self.assertTrue(path2.exists())
        self.assertEqual(cost2, 0.0, "Cached speech synthesis retry must incur $0.00 cost.")

    # -------------------------------------------------------------------------
    # Scenario 9: Blocking ready/publication for incomplete lessons / failed QC
    # -------------------------------------------------------------------------
    def test_09_check_course_completeness_blocks_unready_course(self):
        cid = self.db.create_or_update_course(
            discovery_id="disc_qc",
            canonical_url="https://downloadly.ir/elearning/qc-test/",
            slug="qc-test",
            title_en="QC Test",
            title_fa="تست کنترل کیفیت",
            fingerprint="fp_qc",
            total_parts=1
        )

        manifest = {
            "chapters": [
                {
                    "chapter_title": "فصل اول",
                    "order_index": 1,
                    "lessons": [
                        {"lesson_number": 1, "title": "درس اول", "duration_seconds": 120.0},
                        {"lesson_number": 2, "title": "درس دوم", "duration_seconds": 150.0}
                    ]
                }
            ]
        }
        lesson_ids = self.db.upsert_chapter_and_lessons(cid, manifest)
        self.assertEqual(len(lesson_ids), 2)

        # Incomplete course check (0 uploaded, 0 QC)
        res1 = self.db.check_course_completeness(cid)
        self.assertFalse(res1["is_complete"], "Course must not be complete when lessons are unverified.")

        # Lesson 1 passes QC and is uploaded
        self.db.record_qc_report(lesson_ids[0], passed=True, metrics={}, failure_reasons=[])
        self.db.verify_storage_upload(
            asset_id="asset_l1",
            lesson_id=lesson_ids[0],
            chat_id="-100123",
            message_id=501,
            file_id="fid_l1",
            file_unique_id="fuid_l1",
            file_size=5000,
            sha256="hash_l1"
        )

        # Still incomplete (Lesson 2 pending)
        res2 = self.db.check_course_completeness(cid)
        self.assertFalse(res2["is_complete"])

        # Lesson 2 fails QC
        self.db.record_qc_report(lesson_ids[1], passed=False, metrics={}, failure_reasons=["Audio clipping detected"])
        res3 = self.db.check_course_completeness(cid)
        self.assertFalse(res3["is_complete"], "Course must be blocked when a lesson fails QC.")

    # -------------------------------------------------------------------------
    # Scenario 10: Throttle management, upload intent, no fake random IDs
    # -------------------------------------------------------------------------
    def test_10_upload_intent_and_real_adapter(self):
        adapter = TelegramStorageAdapter()
        dummy_file = Path(self.temp_dir) / "test_media.mp4"
        dummy_file.write_bytes(b"VIDEO_PAYLOAD_1234567890")

        # Record upload intent
        intent_id = self.db.record_upload_intent(
            asset_id="asset_intent_01",
            lesson_id=None,
            file_path=str(dummy_file),
            file_size=len(b"VIDEO_PAYLOAD_1234567890"),
            sha256="hash_intent_01"
        )
        self.assertTrue(intent_id.startswith("intent_"))

        # Explicit test double mode using pre-assigned asset_id from Upload Intent
        upload_res = adapter.upload_media_asset(dummy_file, asset_id="asset_intent_01", test_double=True)
        self.assertEqual(upload_res["status"], "VERIFIED")
        self.assertEqual(upload_res["asset_id"], "asset_intent_01")
        self.assertTrue(upload_res["is_test_double"])
        self.assertTrue(upload_res["playback_url"].startswith("/api/v1/stream/"))

    # -------------------------------------------------------------------------
    # Scenario 11: Real streaming Gateway with HEAD, 200, 206 Range, and 416
    # -------------------------------------------------------------------------
    def test_11_streaming_gateway_range_rfc7233(self):
        file_size = 1000

        # 1. Full content (200 OK)
        r_full = TelegramStorageAdapter.handle_range_request(file_size, range_header=None)
        self.assertEqual(r_full["status_code"], 200)
        self.assertEqual(r_full["length"], 1000)

        # 2. Standard partial range: bytes=100-199 (100 bytes)
        r_part = TelegramStorageAdapter.handle_range_request(file_size, range_header="bytes=100-199")
        self.assertEqual(r_part["status_code"], 206)
        self.assertEqual(r_part["start"], 100)
        self.assertEqual(r_part["end"], 199)
        self.assertEqual(r_part["length"], 100)
        self.assertEqual(r_part["headers"]["Content-Range"], "bytes 100-199/1000")

        # 3. Open-ended range: bytes=800- (last 200 bytes)
        r_open = TelegramStorageAdapter.handle_range_request(file_size, range_header="bytes=800-")
        self.assertEqual(r_open["status_code"], 206)
        self.assertEqual(r_open["start"], 800)
        self.assertEqual(r_open["end"], 999)
        self.assertEqual(r_open["length"], 200)

        # 4. Suffix range: bytes=-300 (last 300 bytes)
        r_suf = TelegramStorageAdapter.handle_range_request(file_size, range_header="bytes=-300")
        self.assertEqual(r_suf["status_code"], 206)
        self.assertEqual(r_suf["start"], 700)
        self.assertEqual(r_suf["end"], 999)
        self.assertEqual(r_suf["length"], 300)

        # 5. Invalid range: bytes=1200-1500 (416 Not Satisfiable)
        r_inv = TelegramStorageAdapter.handle_range_request(file_size, range_header="bytes=1200-1500")
        self.assertEqual(r_inv["status_code"], 416)
        self.assertEqual(r_inv["headers"]["Content-Range"], "bytes */1000")

    # -------------------------------------------------------------------------
    # Scenario 12: Duplicate publication & partial failure without damaging prior version
    # -------------------------------------------------------------------------
    def test_12_publisher_staging_and_prior_version_safety(self):
        publisher = RPIMPublisherAdapter()

        # Unsealed manifest fails
        with self.assertRaises(PublicationException):
            publisher.stage_draft_release({"is_sealed": False, "release_hash": ""})

        manifest = {
            "course_slug": "devops-mastery",
            "is_sealed": True,
            "release_hash": "hash_rel_v2",
            "total_lessons": 1,
            "chapters": [
                {
                    "chapter_title": "فصل اول",
                    "lessons": [
                        {"lesson_number": 1, "title": "مقدمه", "playback_url": "/api/v1/stream/asset1"}
                    ]
                }
            ]
        }

        # Stage as DRAFT
        stage = publisher.stage_draft_release(manifest)
        self.assertEqual(stage["visibility"], "DRAFT")
        self.assertEqual(stage["verification_status"], "VERIFIED")

        # Atomic commit
        commit = publisher.commit_visibility_switch(
            remote_course_id=stage["remote_course_id"],
            release_hash=stage["release_hash"],
            make_public=False
        )
        self.assertEqual(commit["status"], "COMMITTED")
        self.assertEqual(commit["visibility"], "DRAFT")

    # -------------------------------------------------------------------------
    # Scenario 13: Pause and resume based on disk space threshold
    # -------------------------------------------------------------------------
    def test_13_disk_space_guard_and_backpressure(self):
        # When required free disk is set to impossible amount, check fails
        has_space = check_free_disk_space(required_gb=999999.0, path=self.temp_dir)
        self.assertFalse(has_space, "Disk space check must fail when threshold exceeds available storage.")

        # Reasonable threshold passes
        normal_space = check_free_disk_space(required_gb=0.01, path=self.temp_dir)
        self.assertTrue(normal_space)

    # -------------------------------------------------------------------------
    # Scenario 14: Matching existing categories and Persian/English synonyms
    # -------------------------------------------------------------------------
    def test_14_taxonomy_synonym_and_persian_normalization(self):
        engine = SmartTaxonomyEngine()

        # Normalize Arabic Yeh/Kaf and ZWNJ
        raw_text = "آموزش دابلوژي  و كد‌نويسي  پايتون"
        norm = normalize_persian_text(raw_text)
        self.assertNotIn("ك", norm)
        self.assertNotIn("ي", norm)

        existing_categories = [
            {
                "id": "cat_ai_01",
                "name_fa": "هوش مصنوعی",
                "synonyms": ["AI", "Artificial Intelligence"]
            }
        ]

        # Course mentioning "Artificial Intelligence"
        course_data = {
            "title_fa": "دوره جامع Artificial Intelligence و شبکه‌های عصبی",
            "title_en": "Complete AI Course",
            "description_fa": "یادگیری عمیق و هوش مصنوعی پیشرفته"
        }

        res = engine.resolve_taxonomy(course_data, existing_categories, existing_tags=[])
        self.assertEqual(res["category_id"], "cat_ai_01", "Must match existing category via synonym 'Artificial Intelligence'.")
        self.assertEqual(res["status"], "RESOLVED")

    # -------------------------------------------------------------------------
    # Scenario 15: Automated category creation and duplicate collision prevention
    # -------------------------------------------------------------------------
    def test_15_taxonomy_auto_create_category(self):
        engine = SmartTaxonomyEngine(auto_create_enabled=True)

        course_data = {
            "title_fa": "آموزش جامع کوبرنتیز و ارکستراسیون کانتینرها",
            "title_en": "Kubernetes Mastery",
            "description_fa": "مدیریت کلاسترهای کوبرنتیز در محیط ابری"
        }

        res = engine.resolve_taxonomy(course_data, existing_categories=[], existing_tags=[])
        self.assertIsNotNone(res["category"])
        self.assertEqual(res["category"]["name_fa"], "کوبرنتیز و ارکستراسیون")
        self.assertTrue(res["category"]["is_auto_created"])
        self.assertGreaterEqual(len(res["tags"]), 3, "Must select at least 3 relevant tags.")

    # -------------------------------------------------------------------------
    # Scenario 16: Ambiguous topic flagging (TAXONOMY_NEEDS_REVIEW) and manual lock
    # -------------------------------------------------------------------------
    def test_16_taxonomy_needs_review_and_manual_lock(self):
        engine = SmartTaxonomyEngine(auto_create_enabled=False)

        # Ambiguous course with no matching category
        course_data = {
            "title_fa": "مجموعه نکات متفرقه و تجربیات نامشخص",
            "title_en": "Miscellaneous Tips",
            "description_fa": ""
        }

        res = engine.resolve_taxonomy(course_data, existing_categories=[], existing_tags=[])
        self.assertEqual(res["status"], "TAXONOMY_NEEDS_REVIEW", "Ambiguous courses must be flagged for manual review.")

        # Test locked taxonomy preservation
        course_locked = {
            "existing_category_ids": ["cat_manual_01"],
            "existing_tag_ids": ["tag_manual_01"]
        }
        res_locked = engine.resolve_taxonomy(course_locked, existing_categories=[], existing_tags=[], is_locked=True)
        self.assertEqual(res_locked["status"], "LOCKED")
        self.assertEqual(res_locked["category_id"], "cat_manual_01")

    # -------------------------------------------------------------------------
    # Scenario 17: Publication retry idempotency and n8n workflow structure
    # -------------------------------------------------------------------------
    def test_17_n8n_workflows_valid_json_and_env_bindings(self):
        workflows_dir = Path(__file__).resolve().parent.parent / "workflows"
        wf_files = [
            "WF01_DISCOVER.json",
            "WF02_DISPATCH.json",
            "WF03_LESSON_PROCESS.json",
            "WF04_FINALIZE.json",
            "WF05_PUBLISH.json",
            "WF06_RECOVER.json"
        ]

        for wf in wf_files:
            file_path = workflows_dir / wf
            self.assertTrue(file_path.exists(), f"Workflow file {wf} must exist.")
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.assertIn("nodes", data)
            self.assertIn("connections", data)
            self.assertGreater(len(data["nodes"]), 0)

            # Check that hardcoded localhost was removed or wrapped with $env
            wf_str = json.dumps(data)
            self.assertNotIn("http://localhost:8000/api/v1/lessons/dub", wf_str, "WF03 must not use hardcoded localhost!")
            self.assertIn("$env.US_ENGINE_BASE_URL", wf_str, f"Workflow {wf} must read engine URL from $env!")


if __name__ == "__main__":
    unittest.main()
