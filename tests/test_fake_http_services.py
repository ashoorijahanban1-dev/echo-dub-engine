"""
Unit and Integration Tests with Fake HTTP Servers
Tests:
  1. Telegram Bot API:
     - Media upload (sendVideo)
     - Retrieval verification (getFile)
     - Streaming file download (/file/bot...)
     - Timeout & Network failure simulation
     - Reconciliation after failure
  2. RPIM.ir Website API:
     - Staging release in DRAFT mode (/v1/admin/courses/stage)
     - Visibility switch (/v1/admin/courses/{id}/visibility)
     - Categories sync (/v1/admin/categories)
     - Tags sync (/v1/admin/tags)
  3. Iran Relay Service:
     - Authenticated streaming proxy
     - Unauthorized rejection without secret
     - HTTP Range forwarding
     - Anti-SSRF protection against private IPs and disallowed domains
     - Safe redirect validation
"""

import os
import io
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

import httpx
from fastapi.testclient import TestClient

import config
from adapters.telegram import TelegramStorageAdapter, TelegramAPIException
from adapters.publisher import RPIMPublisherAdapter, PublicationException, MediaUnreachableException
from adapters.taxonomy import SmartTaxonomyEngine
from relay_server import app as relay_app, validate_target_url


# =============================================================================
# 1. TELEGRAM BOT API FAKE SERVER TESTS
# =============================================================================

def test_telegram_upload_success(tmp_path):
    """Verifies Telegram upload and retrieval verification with mock HTTP responses."""
    test_video = tmp_path / "test_video.mp4"
    test_video.write_bytes(b"TEST_VIDEO_DATA_" * 100)

    adapter = TelegramStorageAdapter(
        bot_token="123456:FAKE_TOKEN",
        channel_id="-100999888777",
        api_base_url="https://fake.telegram.org"
    )

    fake_send_video_resp = httpx.Response(
        200,
        json={
            "ok": True,
            "result": {
                "message_id": 445566,
                "video": {
                    "file_id": "BAACAgQAAxkBAAE_TEST_FILE_ID",
                    "file_unique_id": "AgAD_TEST_UNIQUE_ID",
                    "file_size": test_video.stat().st_size,
                    "width": 1920,
                    "height": 1080
                }
            }
        }
    )

    fake_get_file_resp = httpx.Response(
        200,
        json={
            "ok": True,
            "result": {
                "file_id": "BAACAgQAAxkBAAE_TEST_FILE_ID",
                "file_path": "videos/file_01.mp4",
                "file_size": test_video.stat().st_size
            }
        }
    )

    with patch("httpx.post", return_value=fake_send_video_resp):
        with patch("httpx.get", return_value=fake_get_file_resp):
            res = adapter.upload_media_asset(
                media_path=test_video,
                asset_id="asset_preassigned_001",
                caption="Lesson 1 Dubbed",
                duration_seconds=45.0
            )

            assert res["status"] == "VERIFIED"
            assert res["asset_id"] == "asset_preassigned_001"
            assert res["file_id"] == "BAACAgQAAxkBAAE_TEST_FILE_ID"
            assert res["message_id"] == 445566


def test_telegram_upload_timeout_and_network_failure(tmp_path):
    """Verifies handling of network timeout and error during upload."""
    test_video = tmp_path / "test_video.mp4"
    test_video.write_bytes(b"TEST_DATA" * 50)

    adapter = TelegramStorageAdapter(
        bot_token="123456:FAKE_TOKEN",
        channel_id="-100999888777",
        api_base_url="https://fake.telegram.org"
    )

    # Simulate timeout
    with patch("httpx.post", side_effect=httpx.TimeoutException("Connection timed out")):
        with pytest.raises(TelegramAPIException) as exc_info:
            adapter.upload_media_asset(
                media_path=test_video,
                asset_id="asset_timeout_test"
            )
        assert "Failed to upload" in str(exc_info.value)


def test_telegram_reconcile_after_failure(tmp_path):
    """
    Tests reconciliation: If upload was interrupted but message was posted,
    getFile verifies whether asset is already indexed.
    """
    adapter = TelegramStorageAdapter(
        bot_token="123456:FAKE_TOKEN",
        channel_id="-100999888777",
        api_base_url="https://fake.telegram.org"
    )

    fake_get_file_resp = httpx.Response(
        200,
        json={
            "ok": True,
            "result": {
                "file_id": "RECONCILED_FILE_ID",
                "file_path": "videos/reconciled.mp4"
            }
        }
    )

    with patch("httpx.get", return_value=fake_get_file_resp):
        file_path = adapter.verify_file_retrieval("RECONCILED_FILE_ID")
        assert file_path == "videos/reconciled.mp4"


# =============================================================================
# 2. RPIM.IR WEBSITE API FAKE SERVER TESTS
# =============================================================================

def test_rpim_publisher_staging_with_fake_api():
    """Tests staging release with fake rpim.ir API response."""
    publisher = RPIMPublisherAdapter(
        api_base_url="https://fake.rpim.ir/api",
        api_key="valid_test_admin_key"
    )

    manifest = {
        "course_slug": "advanced-kubernetes",
        "is_sealed": True,
        "release_hash": "a1b2c3d4e5f67890",
        "total_lessons": 1,
        "chapters": [
            {
                "chapter_title": "فصل اول",
                "lessons": [
                    {
                        "lesson_number": 1,
                        "title_fa": "مقدمه",
                        "duration_seconds": 120.0,
                        "playback_url": "/api/v1/stream/asset_test_123"
                    }
                ]
            }
        ]
    }

    fake_stage_resp = httpx.Response(
        200,
        json={
            "remote_course_id": "rpim_crs_1001",
            "release_hash": "a1b2c3d4e5f67890",
            "status": "STAGED_DRAFT"
        }
    )

    fake_head_resp = httpx.Response(206, headers={"Content-Range": "bytes 0-10/1000"})
    with patch("httpx.post", return_value=fake_stage_resp):
        with patch("httpx.head", return_value=fake_head_resp):
            res = publisher.stage_draft_release(manifest=manifest)
            assert res["verification_status"] == "VERIFIED"
            assert res["remote_course_id"] == "rpim_crs_1001"
            assert res["visibility"] == "DRAFT"



def test_rpim_publisher_unreachable_media_rejection():
    """Verifies that staging rejects when media URL returns 404 or fails HEAD/Range."""
    publisher = RPIMPublisherAdapter(
        api_base_url="https://fake.rpim.ir/api",
        api_key="valid_test_admin_key"
    )

    manifest = {
        "course_slug": "bad-media-course",
        "is_sealed": True,
        "release_hash": "hash_bad_001",
        "total_lessons": 1,
        "chapters": [
            {
                "chapter_title": "فصل اول",
                "lessons": [
                    {
                        "lesson_number": 1,
                        "title_fa": "درس نامعتبر",
                        "playback_url": "https://cdn.example.com/missing-video.mp4"
                    }
                ]
            }
        ]
    }

    # Simulate 404 on media URL
    fake_404_resp = httpx.Response(404, text="Not Found")
    with patch("httpx.head", return_value=fake_404_resp):
        with patch("httpx.get", return_value=fake_404_resp):
            with pytest.raises(MediaUnreachableException):
                publisher.stage_draft_release(manifest=manifest)


def test_rpim_publisher_visibility_switch():
    """Tests atomic visibility commit switch."""
    publisher = RPIMPublisherAdapter(
        api_base_url="https://fake.rpim.ir/api",
        api_key="valid_test_admin_key"
    )

    fake_patch_resp = httpx.Response(
        200,
        json={"status": "UPDATED", "visibility": "PUBLISHED"}
    )

    with patch("httpx.patch", return_value=fake_patch_resp):
        res = publisher.commit_visibility_switch(
            remote_course_id="rpim_crs_1001",
            release_hash="a1b2c3d4e5f67890",
            make_public=True
        )
        assert res["visibility"] == "PUBLISHED"
        assert res["status"] == "COMMITTED"


def test_taxonomy_sync_from_fake_api():
    """Tests synchronizing categories and tags from fake rpim.ir API."""
    engine = SmartTaxonomyEngine()

    fake_cats_resp = httpx.Response(
        200,
        json=[
            {"id": 201, "name_fa": "داده‌کاوی و هوش تجاری", "slug": "data-mining", "depth": 1},
            {"id": 202, "name_fa": "مهندسی نرم‌افزار", "slug": "software-eng", "depth": 1}
        ]
    )

    fake_tags_resp = httpx.Response(
        200,
        json=[
            {"id": 301, "name_fa": "پایتون", "slug": "python"},
            {"id": 302, "name_fa": "داکر", "slug": "docker"}
        ]
    )

    with patch("httpx.get") as mock_get:
        mock_get.side_effect = [fake_cats_resp, fake_tags_resp]
        cats = engine.sync_categories_from_site(api_base_url="https://fake.rpim.ir/api", api_key="real_key")
        tags = engine.sync_tags_from_site(api_base_url="https://fake.rpim.ir/api", api_key="real_key")

        assert len(cats) >= 2
        assert any(c["slug"] == "data-mining" for c in cats)
        assert len(tags) >= 2


# =============================================================================
# 3. IRAN RELAY SERVICE TESTS
# =============================================================================

@pytest.fixture
def relay_client():
    return TestClient(relay_app)


def test_relay_health(relay_client):
    resp = relay_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "HEALTHY"


def test_relay_unauthorized_access(relay_client):
    """Verifies that requests without secret header are rejected when secret is configured."""
    with patch("relay_server.RELAY_SECRET", "super_secret_relay_token"):
        resp = relay_client.get("/api/v1/relay/stream?target=https://dl.downloadly.ir/sample.rar")
        assert resp.status_code == 401


def test_relay_ssrf_protection_private_ips(relay_client):
    """Verifies SSRF rejection for private/loopback targets."""
    forbidden_targets = [
        "http://127.0.0.1:8000/secret",
        "http://localhost:8000/secret",
        "http://192.168.1.1/admin",
        "http://10.0.0.1/metadata",
        "http://169.254.169.254/latest/meta-data/"
    ]
    for target in forbidden_targets:
        with pytest.raises(Exception):
            validate_target_url(target)


def test_relay_ssrf_disallowed_domain():
    """Verifies SSRF rejection for arbitrary external domains not in allowlist."""
    with pytest.raises(Exception) as exc:
        validate_target_url("https://malicious-site.com/payload.exe")
    assert "SSRF violation" in str(exc.value)


def test_relay_streaming_with_mock_origin(relay_client):
    """Tests authenticated relay streaming of media content."""
    fake_stream_data = b"RELAY_MEDIA_CHUNK_" * 50

    # Mock the AsyncClient inside relay_server
    with patch("httpx.AsyncClient.send") as mock_send:
        mock_resp = MagicMock()
        mock_resp.status_code = 206
        mock_resp.headers = {
            "Content-Range": "bytes 0-100/1000",
            "Content-Length": str(len(fake_stream_data)),
            "Content-Type": "video/mp4",
            "Accept-Ranges": "bytes"
        }

        async def aiter_bytes(chunk_size=65536):
            yield fake_stream_data

        mock_resp.aiter_bytes = aiter_bytes
        mock_resp.aclose = MagicMock()
        mock_send.return_value = mock_resp

        with patch("relay_server.RELAY_SECRET", "test_relay_secret"):
            resp = relay_client.get(
                "/api/v1/relay/stream?target=https://dl.downloadly.ir/course.rar",
                headers={
                    "X-RPIM-Relay-Secret": "test_relay_secret",
                    "Range": "bytes=0-100"
                }
            )
            assert resp.status_code == 206
            assert resp.headers.get("Content-Range") == "bytes 0-100/1000"
            assert resp.content == fake_stream_data


def test_telegram_fake_http_server_cache_miss_and_range_streaming(tmp_path):
    """
    Live Fake HTTP Server integration test for Telegram Storage Gateway:
    1. Starts a real local HTTP server emulating Telegram Bot API getFile & CDN file download.
    2. Exercises Gateway cache miss: file is absent locally, Gateway downloads from Fake HTTP Server.
    3. Exercises RFC 7233 HEAD request, byte-range slices (0-49, 100-199, suffix -200), and 416 error.
    4. Verifies CORS headers are properly separated on the streaming endpoint.
    """
    import uuid
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from fastapi.testclient import TestClient
    from server import app, telegram_adapter, db
    import config

    # Real media payload: 5000 bytes with recognizable sequence
    real_video_payload = b"".join(f"CHUNK_{i:04d}_PAD_DATA_FOR_RANGE_TESTING_1234567890\n".encode("utf-8") for i in range(125))[:5000]
    payload_len = len(real_video_payload)
    test_file_id = f"BAACAgQAAxkBAAE_{uuid.uuid4().hex[:16]}"
    test_asset_id = f"asset_fake_http_{uuid.uuid4().hex[:12]}"
    bot_token = "fake_test_bot_token_998877"

    class FakeTelegramHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            # Telegram getFile API
            if f"/bot{bot_token}/getFile" in self.path:
                body = json.dumps({
                    "ok": True,
                    "result": {
                        "file_id": test_file_id,
                        "file_path": "videos/test_media_stream.mp4",
                        "file_size": payload_len
                    }
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            # Telegram CDN file download
            elif f"/file/bot{bot_token}/videos/test_media_stream.mp4" in self.path:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(payload_len))
                self.end_headers()
                self.wfile.write(real_video_payload)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass # Suppress test log noise

    server = HTTPServer(("127.0.0.1", 0), FakeTelegramHandler)
    server_port = server.server_port
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Configure adapters with the Fake HTTP Server base URL
    orig_api_base = telegram_adapter.api_base_url
    orig_token = telegram_adapter.bot_token
    orig_cache = config.CACHE_STORAGE_DIR

    telegram_adapter.api_base_url = f"http://127.0.0.1:{server_port}"
    telegram_adapter.bot_token = bot_token
    telegram_adapter.test_double = False
    config.CACHE_STORAGE_DIR = cache_dir

    try:
        # Register in DB
        db.processing_backend.execute(
            "INSERT INTO storage_objects (asset_id, storage_provider, chat_id, message_id, file_id, file_unique_id, file_size, sha256, mime_type, status, created_at) "
            "VALUES (?, 'TELEGRAM_CDN', '-100123', 888, ?, 'uniq_888', ?, 'dummy_hash', 'video/mp4', 'VERIFIED', '2026-09-08T00:00:00Z')",
            (test_asset_id, test_file_id, payload_len)
        )

        client = TestClient(app)

        # 1. HEAD request on cache miss -> downloads file, returns 200 with headers
        head_resp = client.head(f"/api/v1/stream/{test_asset_id}")
        assert head_resp.status_code == 200
        assert head_resp.headers["Accept-Ranges"] == "bytes"
        assert head_resp.headers["Content-Length"] == str(payload_len)
        assert head_resp.headers["Access-Control-Allow-Origin"] == "*"

        # Verify file is now cached on disk
        cached_files = list(cache_dir.glob(f"*{test_asset_id}*"))
        assert len(cached_files) == 1
        assert cached_files[0].stat().st_size == payload_len

        # 2. GET full content (200 OK)
        full_resp = client.get(f"/api/v1/stream/{test_asset_id}")
        assert full_resp.status_code == 200
        assert full_resp.content == real_video_payload
        assert full_resp.headers["Access-Control-Allow-Origin"] == "*"

        # 3. GET Range bytes=0-49 (206 Partial Content)
        range_50_resp = client.get(f"/api/v1/stream/{test_asset_id}", headers={"Range": "bytes=0-49"})
        assert range_50_resp.status_code == 206
        assert range_50_resp.headers["Content-Range"] == f"bytes 0-49/{payload_len}"
        assert range_50_resp.headers["Content-Length"] == "50"
        assert range_50_resp.content == real_video_payload[0:50]

        # 4. GET Range bytes=100-199 (206 Partial Content)
        range_100_resp = client.get(f"/api/v1/stream/{test_asset_id}", headers={"Range": "bytes=100-199"})
        assert range_100_resp.status_code == 206
        assert range_100_resp.headers["Content-Range"] == f"bytes 100-199/{payload_len}"
        assert range_100_resp.headers["Content-Length"] == "100"
        assert range_100_resp.content == real_video_payload[100:200]

        # 5. GET Suffix Range bytes=-200 (last 200 bytes)
        suffix_resp = client.get(f"/api/v1/stream/{test_asset_id}", headers={"Range": "bytes=-200"})
        assert suffix_resp.status_code == 206
        assert suffix_resp.headers["Content-Range"] == f"bytes {payload_len-200}-{payload_len-1}/{payload_len}"
        assert suffix_resp.headers["Content-Length"] == "200"
        assert suffix_resp.content == real_video_payload[-200:]

        # 6. GET Out of Range bytes=99999-999999 (416 Range Not Satisfiable)
        oob_resp = client.get(f"/api/v1/stream/{test_asset_id}", headers={"Range": "bytes=99999-999999"})
        assert oob_resp.status_code == 416
        assert oob_resp.headers["Content-Range"] == f"bytes */{payload_len}"

        # 7. OPTIONS preflight CORS check
        options_resp = client.options(f"/api/v1/stream/{test_asset_id}")
        assert options_resp.status_code == 204
        assert options_resp.headers["Access-Control-Allow-Origin"] == "*"

    finally:
        telegram_adapter.api_base_url = orig_api_base
        telegram_adapter.bot_token = orig_token
        config.CACHE_STORAGE_DIR = orig_cache
        server.shutdown()
        server.server_close()
        db.processing_backend.execute("DELETE FROM storage_objects WHERE asset_id = ?", (test_asset_id,))


def test_clients_ignore_ambient_proxy_environment(monkeypatch):
    """
    Verifies that internal HTTP clients ignore ambient HTTP_PROXY, HTTPS_PROXY, ALL_PROXY
    and do not raise SOCKS proxy ImportError when socksio is absent.
    """
    from relay_server import create_relay_http_client
    from adapters.telegram import TelegramStorageAdapter

    # Inject hostile ambient SOCKS and HTTP proxies
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")

    # 1. Relay AsyncClient must initialize with trust_env=False without raising SOCKS error
    relay_client = create_relay_http_client()
    assert relay_client.trust_env is False

    # 2. Telegram adapter must create clients with trust_env=False
    tg = TelegramStorageAdapter(test_double=True)
    c = tg._create_http_client()
    assert c.trust_env is False

    # 3. Config helper must return trust_env=False
    kwargs = config.get_httpx_client_kwargs()
    assert kwargs["trust_env"] is False
    assert "proxy" not in kwargs


def test_proxy_url_explicit_activation(monkeypatch):
    """
    Verifies that explicit PROXY_URL configuration activates the proxy cleanly.
    """
    import relay_server

    relay_server.PROXY_URL = "http://corporate-proxy.internal:8080"
    client = relay_server.create_relay_http_client()
    assert client.trust_env is False
    assert client._mounts is not None

    # Reset
    relay_server.PROXY_URL = None


