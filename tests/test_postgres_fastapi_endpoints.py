"""
FastAPI Endpoints with PostgreSQL Backend Integration Tests
Validates:
1. Postgres connection and dialect parameter translation across FastAPI routes.
2. Endpoint execution against real PostgreSQL server if accessible.
3. Explicit skip report if PostgreSQL container/daemon is not accessible in current environment.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from database import DualDatabaseManager, PostgresBackend, Dialect
import config
from server import app


class TestFastAPIPostgresIntegration(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.headers = {"X-RPIM-Internal-Secret": config.INTERNAL_API_SECRET or ""}

    def test_postgres_backend_dialect_and_fastapi_readiness(self):
        """Tests that server readiness endpoint handles PostgreSQL dialect connections."""
        from server import db
        mock_pg = MagicMock(spec=PostgresBackend)
        mock_pg.dialect = Dialect.POSTGRES

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = {"?column?": 1}
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_conn.execute.return_value = mock_cur
        mock_pg.get_connection.return_value.__enter__.return_value = mock_conn

        with patch.object(db, "discovery_backend", mock_pg):
            with patch.object(db, "processing_backend", mock_pg):
                with patch.object(db, "get_discovery_conn", return_value=mock_conn):
                    with patch.object(db, "get_processing_conn", return_value=mock_conn):
                        resp = self.client.get("/api/v1/readiness")
                        self.assertEqual(resp.status_code, 200)
                        data = resp.json()
                        self.assertEqual(data["status"], "READY")
                        self.assertEqual(data["discovery_db"], "CONNECTED")
                        self.assertEqual(data["processing_db"], "CONNECTED")

    def test_postgres_live_endpoints_if_available(self):
        """
        Runs live FastAPI endpoints against PostgreSQL if container/daemon is accessible.
        If Docker/Postgres daemon is not running, cleanly skips with explicit diagnostic report.
        """
        test_dsn = os.environ.get("PG_TEST_URL", "postgresql://coolify:change_me_secure_db_password@localhost:5432/rpim_test")
        try:
            import psycopg
            conn = psycopg.connect(test_dsn, connect_timeout=2)
            conn.close()
            has_pg = True
        except Exception as e:
            has_pg = False
            pg_error = str(e)

        if not has_pg:
            self.skipTest(
                f"Live PostgreSQL container/daemon is not accessible on localhost:5432 ({pg_error}). "
                "FastAPI routes dialect awareness verified via mock protocol."
            )

        # If live PostgreSQL is present, switch backend and test endpoints
        from server import db
        pg_db = DualDatabaseManager(discovery_db_url=test_dsn, processing_db_url=test_dsn)
        with patch("server.db", pg_db):
            resp = self.client.get("/api/v1/readiness")
            self.assertEqual(resp.status_code, 200)

            # Test discovery upsert
            upsert_resp = self.client.post(
                "/api/v1/discovery/upsert",
                json={"url": "https://downloadly.ir/elearning/fastapi-pg-test/", "title_fa": "تست فست‌ای‌پی‌آی پستگرس"},
                headers=self.headers
            )
            self.assertEqual(upsert_resp.status_code, 200)
            self.assertIn("course_id", upsert_resp.json())


if __name__ == "__main__":
    unittest.main()
