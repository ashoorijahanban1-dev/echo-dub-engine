"""
RPIM Database Abstraction Layer (Dialect-Aware)
Supports both PostgreSQL (production) and SQLite (testing/local dev)
with automatic query translation (? to %s), atomic transactions,
fencing tokens, lease renewal/heartbeats, outbox/inbox events, and dead-letter handling.
"""

import os
import re
import json
import uuid
import random
import sqlite3
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Generator
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

import config

class Dialect:
    SQLITE = "sqlite"
    POSTGRES = "postgres"


class DatabaseBackend:
    dialect: str

    def execute(self, query: str, params: Tuple = ()) -> int:
        raise NotImplementedError

    def fetchone(self, query: str, params: Tuple = ()) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def fetchall(self, query: str, params: Tuple = ()) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def execute_script(self, script_path: str):
        raise NotImplementedError

    @contextmanager
    def transaction(self) -> Generator['DatabaseBackend', None, None]:
        raise NotImplementedError


class DialectCursorWrapper:
    """Cursor wrapper ensuring ? -> %s translation and dictionary return format."""
    def __init__(self, raw_cursor: Any, dialect: Dialect):
        self._cursor = raw_cursor
        self._dialect = dialect

    def execute(self, query: str, params: Tuple = ()):
        if self._dialect == Dialect.POSTGRES:
            query = query.replace('?', '%s')
        res = self._cursor.execute(query, params)
        return self

    def fetchone(self) -> Optional[Dict[str, Any]]:
        row = self._cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    def fetchall(self) -> List[Dict[str, Any]]:
        rows = self._cursor.fetchall()
        return [dict(r) for r in rows]

    @property
    def rowcount(self) -> int:
        return getattr(self._cursor, "rowcount", -1)

    def __enter__(self):
        if hasattr(self._cursor, "__enter__"):
            self._cursor.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if hasattr(self._cursor, "__exit__"):
            return self._cursor.__exit__(exc_type, exc_val, exc_tb)

    def __iter__(self):
        for row in self._cursor:
            yield dict(row)

    def __getattr__(self, name: str):
        return getattr(self._cursor, name)


class DialectConnectionWrapper:
    """
    Unified connection wrapper ensuring that execute(), cursor(), commit(),
    and context manager semantics behave identically across SQLite and PostgreSQL.
    Converts '?' placeholders to '%s' automatically when running against PostgreSQL.
    Always produces dictionary results for rows.
    """
    def __init__(self, raw_conn: Any, dialect: Dialect):
        self._conn = raw_conn
        self._dialect = dialect

    def cursor(self):
        raw_cur = self._conn.cursor() if hasattr(self._conn, "cursor") else self._conn
        return DialectCursorWrapper(raw_cur, self._dialect)

    def execute(self, query: str, params: Tuple = ()):
        if self._dialect == Dialect.POSTGRES:
            query = query.replace('?', '%s')
        cur = self._conn.execute(query, params)
        return DialectCursorWrapper(cur, self._dialect)

    def commit(self):
        if hasattr(self._conn, "commit"):
            self._conn.commit()

    def rollback(self):
        if hasattr(self._conn, "rollback"):
            self._conn.rollback()

    def close(self):
        if hasattr(self._conn, "close"):
            self._conn.close()

    def __enter__(self):
        if hasattr(self._conn, "__enter__"):
            self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if hasattr(self._conn, "__exit__"):
            return self._conn.__exit__(exc_type, exc_val, exc_tb)
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


class SQLiteBackend(DatabaseBackend):
    dialect = Dialect.SQLITE

    def __init__(self, db_path: str):
        self.db_path = db_path
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    def get_connection(self) -> DialectConnectionWrapper:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        return DialectConnectionWrapper(conn, Dialect.SQLITE)

    @contextmanager
    def transaction(self) -> Generator[DialectConnectionWrapper, None, None]:
        conn = self.get_connection()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def execute(self, query: str, params: Tuple = ()) -> int:
        with self.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.rowcount

    def fetchone(self, query: str, params: Tuple = ()) -> Optional[Dict[str, Any]]:
        with self.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchone()

    def fetchall(self, query: str, params: Tuple = ()) -> List[Dict[str, Any]]:
        with self.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchall()

    def execute_script(self, script_path: str):
        with open(script_path, "r", encoding="utf-8") as f:
            sql = f.read()
        sql = re.sub(r'TIMESTAMP WITH TIME ZONE', 'TIMESTAMP', sql, flags=re.IGNORECASE)
        sql = re.sub(r'BOOLEAN DEFAULT FALSE', 'INTEGER DEFAULT 0', sql, flags=re.IGNORECASE)
        sql = re.sub(r'BOOLEAN DEFAULT TRUE', 'INTEGER DEFAULT 1', sql, flags=re.IGNORECASE)
        sql = re.sub(r'BOOLEAN', 'INTEGER', sql, flags=re.IGNORECASE)
        with self.transaction() as conn:
            conn._conn.executescript(sql)


class PostgresBackend(DatabaseBackend):
    dialect = Dialect.POSTGRES

    def __init__(self, dsn: str):
        self.dsn = dsn
        try:
            import psycopg
            from psycopg.rows import dict_row
            self._psycopg = psycopg
            self._dict_row = dict_row
        except ImportError:
            self._psycopg = None
            self._dict_row = None

    def get_connection(self) -> DialectConnectionWrapper:
        if not self._psycopg:
            raise RuntimeError("psycopg is required for PostgreSQL. Install via: pip install psycopg[binary]")
        import time
        max_retries = 3
        for attempt in range(max_retries):
            try:
                raw_conn = self._psycopg.connect(self.dsn, row_factory=self._dict_row)
                return DialectConnectionWrapper(raw_conn, Dialect.POSTGRES)
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(0.5 * (2 ** attempt))
                else:
                    raise

    def _translate_query(self, query: str) -> str:
        """Translates ? parameter placeholders to %s for psycopg."""
        return query.replace('?', '%s')

    @contextmanager
    def transaction(self) -> Generator[DialectConnectionWrapper, None, None]:
        conn = self.get_connection()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def execute(self, query: str, params: Tuple = ()) -> int:
        pg_query = self._translate_query(query)
        with self.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(pg_query, params)
                return cur.rowcount

    def fetchone(self, query: str, params: Tuple = ()) -> Optional[Dict[str, Any]]:
        pg_query = self._translate_query(query)
        with self.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(pg_query, params)
                return cur.fetchone()

    def fetchall(self, query: str, params: Tuple = ()) -> List[Dict[str, Any]]:
        pg_query = self._translate_query(query)
        with self.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(pg_query, params)
                return cur.fetchall()


    def execute_script(self, script_path: str):
        with open(script_path, "r", encoding="utf-8") as f:
            sql = f.read()
        with self.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)


def create_backend(url_or_path: str) -> DatabaseBackend:
    if url_or_path.startswith("postgresql://") or url_or_path.startswith("postgres://"):
        try:
            import psycopg
            return PostgresBackend(url_or_path)
        except ImportError:
            return SQLiteBackend("storage/fallback_postgres.db")
    elif url_or_path.startswith("sqlite:///"):
        return SQLiteBackend(url_or_path.replace("sqlite:///", ""))
    elif url_or_path == ":memory:":
        return SQLiteBackend(":memory:")
    else:
        return SQLiteBackend(url_or_path)


class DualDatabaseManager:
    """
    Central database manager managing Discovery DB and Processing DB separately.
    Fully dialect-aware for both PostgreSQL and SQLite.
    """
    def __init__(
        self,
        discovery_db_url: Optional[str] = None,
        processing_db_url: Optional[str] = None,
        run_migrations: bool = True
    ):
        if discovery_db_url and not processing_db_url:
            disc_url = discovery_db_url
            proc_url = discovery_db_url
        else:
            disc_url = discovery_db_url or config.DISCOVERY_DB_URL
            proc_url = processing_db_url or config.PROCESSING_DB_URL

        self.discovery_backend = create_backend(disc_url)
        self.processing_backend = create_backend(proc_url)

        if run_migrations:
            self.migrate_all()

    def migrate_all(self):
        migrations_dir = Path(__file__).resolve().parent / "migrations"
        disc_migration = migrations_dir / "001_discovery_db.sql"
        proc_migration = migrations_dir / "002_processing_db.sql"

        if disc_migration.exists():
            self.discovery_backend.execute_script(str(disc_migration))
        if proc_migration.exists():
            self.processing_backend.execute_script(str(proc_migration))

    def get_discovery_conn(self) -> DialectConnectionWrapper:
        return self.discovery_backend.get_connection()

    def get_processing_conn(self) -> DialectConnectionWrapper:
        return self.processing_backend.get_connection()

    @contextmanager
    def processing_transaction(self):
        with self.processing_backend.transaction() as conn:
            yield conn

    @contextmanager
    def discovery_transaction(self):
        with self.discovery_backend.transaction() as conn:
            yield conn

    def execute_processing(self, query: str, params: Tuple = ()) -> int:
        return self.processing_backend.execute(query, params)

    def fetchone_processing(self, query: str, params: Tuple = ()) -> Optional[Dict[str, Any]]:
        return self.processing_backend.fetchone(query, params)

    def fetchall_processing(self, query: str, params: Tuple = ()) -> List[Dict[str, Any]]:
        return self.processing_backend.fetchall(query, params)

    def execute_discovery(self, query: str, params: Tuple = ()) -> int:
        return self.discovery_backend.execute(query, params)

    def fetchone_discovery(self, query: str, params: Tuple = ()) -> Optional[Dict[str, Any]]:
        return self.discovery_backend.fetchone(query, params)

    def fetchall_discovery(self, query: str, params: Tuple = ()) -> List[Dict[str, Any]]:
        return self.discovery_backend.fetchall(query, params)


    # =========================================================================
    # DISCOVERY DATABASE OPERATIONS
    # =========================================================================

    def upsert_discovered_course(
        self,
        url: str,
        canonical_url: str,
        slug: str,
        title_en: Optional[str],
        title_fa: Optional[str],
        fingerprint: str,
        metadata: Dict[str, Any]
    ) -> str:
        now = datetime.now(timezone.utc).isoformat()
        course_id = f"disc_{uuid.uuid5(uuid.NAMESPACE_URL, canonical_url).hex[:16]}"
        
        with self.discovery_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            execute_fn = cur.execute
            
            # Use dialect query if Postgres
            is_pg = getattr(self.discovery_backend, "dialect", "") == Dialect.POSTGRES
            placeholder = "%s" if is_pg else "?"
            
            check_sql = f"SELECT id, source_fingerprint, status FROM discovered_courses WHERE canonical_url = {placeholder}"
            cur.execute(check_sql, (canonical_url,))
            existing = cur.fetchone()

            if existing:
                real_id = existing["id"] if isinstance(existing, dict) else existing[0]
                old_fp = existing["source_fingerprint"] if isinstance(existing, dict) else existing[1]
                
                update_sql = f"""
                    UPDATE discovered_courses
                    SET url = {placeholder},
                        title_en = COALESCE({placeholder}, title_en),
                        title_fa = COALESCE({placeholder}, title_fa),
                        source_fingerprint = {placeholder},
                        last_seen_at = {placeholder},
                        metadata_json = {placeholder}
                    WHERE id = {placeholder}
                """
                cur.execute(update_sql, (url, title_en, title_fa, fingerprint, now, json.dumps(metadata), real_id))
                
                if old_fp != fingerprint:
                    outbox_id = f"evt_{uuid.uuid4().hex[:16]}"
                    event_payload = json.dumps({
                        "course_id": real_id,
                        "canonical_url": canonical_url,
                        "fingerprint": fingerprint,
                        "is_update": True
                    })
                    ins_outbox = f"""
                        INSERT INTO discovery_outbox (id, aggregate_id, event_type, payload_json, status, created_at, next_retry_at)
                        VALUES ({placeholder}, {placeholder}, 'course.updated', {placeholder}, 'PENDING', {placeholder}, {placeholder})
                    """
                    cur.execute(ins_outbox, (outbox_id, real_id, event_payload, now, now))
                return real_id
            else:
                ins_sql = f"""
                    INSERT INTO discovered_courses (
                        id, url, canonical_url, slug, title_en, title_fa,
                        source_fingerprint, status, claim_state, next_eligible_at,
                        first_seen_at, last_seen_at, metadata_json
                    ) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, 'DISCOVERED', 'UNCLAIMED', {placeholder}, {placeholder}, {placeholder}, {placeholder})
                """
                cur.execute(ins_sql, (course_id, url, canonical_url, slug, title_en, title_fa, fingerprint, now, now, now, json.dumps(metadata)))

                outbox_id = f"evt_{uuid.uuid4().hex[:16]}"
                event_payload = json.dumps({
                    "course_id": course_id,
                    "canonical_url": canonical_url,
                    "fingerprint": fingerprint,
                    "is_update": False
                })
                ins_outbox = f"""
                    INSERT INTO discovery_outbox (id, aggregate_id, event_type, payload_json, status, created_at, next_retry_at)
                    VALUES ({placeholder}, {placeholder}, 'course.discovered', {placeholder}, 'PENDING', {placeholder}, {placeholder})
                """
                cur.execute(ins_outbox, (outbox_id, course_id, event_payload, now, now))
                return course_id

    def claim_discovered_course(self, worker_id: str, lease_seconds: int = 300) -> Optional[Dict[str, Any]]:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        expiry = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        claim_token = uuid.uuid4().hex

        with self.discovery_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            is_pg = getattr(self.discovery_backend, "dialect", "") == Dialect.POSTGRES
            p = "%s" if is_pg else "?"

            sel_sql = f"""
                SELECT * FROM discovered_courses
                WHERE (claim_state = 'UNCLAIMED' OR (claim_state = 'CLAIMED' AND lease_expiry < {p}))
                  AND status IN ('DISCOVERED', 'CLAIMED')
                  AND (next_eligible_at IS NULL OR next_eligible_at <= {p})
                  AND (url LIKE '%/elearning/video-tutorials/%' OR url LIKE '%/video-tutorials/%')
                  AND url NOT LIKE '%/download/%'
                ORDER BY first_seen_at ASC
                LIMIT 1
            """
            cur.execute(sel_sql, (now, now))
            row = cur.fetchone()
            if not row:
                return None

            course = dict(row)
            course_id = course["id"]

            upd_sql = f"""
                UPDATE discovered_courses
                SET claim_state = 'CLAIMED',
                    status = 'CLAIMED',
                    lease_owner = {p},
                    claim_token = {p},
                    lease_expiry = {p},
                    heartbeat = {p}
                WHERE id = {p} AND (claim_state = 'UNCLAIMED' OR lease_expiry < {p})
            """
            cur.execute(upd_sql, (worker_id, claim_token, expiry, now, course_id, now))
            course["claim_token"] = claim_token
            course["lease_owner"] = worker_id
            course["lease_expiry"] = expiry
            return course

    def mark_discovered_course_enqueued(self, discovery_id: str) -> bool:
        """Marks a claimed discovered course as ENQUEUED once validated and processing job created."""
        is_pg = getattr(self.discovery_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        now = datetime.now(timezone.utc).isoformat()
        with self.discovery_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            cur.execute(
                f"UPDATE discovered_courses SET status = 'ENQUEUED', claim_state = 'ENQUEUED', last_seen_at = {p} WHERE id = {p}",
                (now, discovery_id)
            )
            return True

    def get_pending_outbox_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        is_pg = getattr(self.discovery_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        query = f"""
            SELECT * FROM discovery_outbox
            WHERE status IN ('PENDING', 'FAILED')
              AND attempts < max_attempts
              AND (next_retry_at IS NULL OR next_retry_at <= {p})
            ORDER BY created_at ASC
            LIMIT {limit}
        """
        with self.discovery_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            cur.execute(query, (now,))
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def mark_outbox_event_sent(self, event_id: str):
        now = datetime.now(timezone.utc).isoformat()
        is_pg = getattr(self.discovery_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        query = f"UPDATE discovery_outbox SET status = 'SENT', processed_at = {p} WHERE id = {p}"
        with self.discovery_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            cur.execute(query, (now, event_id))

    def fail_outbox_event(self, event_id: str, error_message: str):
        now_dt = datetime.now(timezone.utc)
        is_pg = getattr(self.discovery_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        with self.discovery_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            cur.execute(f"SELECT attempts, max_attempts FROM discovery_outbox WHERE id = {p}", (event_id,))
            row = cur.fetchone()
            if not row:
                return
            attempts = row["attempts"] if isinstance(row, dict) else row[0]
            max_attempts = row["max_attempts"] if isinstance(row, dict) else row[1]
            new_attempts = attempts + 1
            if new_attempts >= max_attempts:
                status = 'DEAD_LETTER'
                next_retry = None
            else:
                status = 'FAILED'
                delay = (2 ** new_attempts) + random.uniform(0.5, 2.0)
                next_retry = (now_dt + timedelta(seconds=delay)).isoformat()

            upd = f"""
                UPDATE discovery_outbox
                SET attempts = {p}, status = {p}, error_message = {p}, next_retry_at = {p}
                WHERE id = {p}
            """
            cur.execute(upd, (new_attempts, status, error_message, next_retry, event_id))

    # =========================================================================
    # PROCESSING DATABASE OPERATIONS
    # =========================================================================

    def process_inbox_event(self, source_event_id: str, aggregate_id: str, event_type: str, payload_json: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        inbox_id = f"inb_{uuid.uuid4().hex[:16]}"
        is_pg = getattr(self.processing_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        with self.processing_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            cur.execute(f"SELECT id FROM inbox_events WHERE source_event_id = {p}", (source_event_id,))
            if cur.fetchone():
                return False
            ins = f"""
                INSERT INTO inbox_events (id, source_event_id, aggregate_id, event_type, payload_json, processed, created_at)
                VALUES ({p}, {p}, {p}, {p}, {p}, 1, {p})
            """
            cur.execute(ins, (inbox_id, source_event_id, aggregate_id, event_type, payload_json, now))
            return True

    def create_or_update_course(
        self,
        discovery_id: str,
        canonical_url: str,
        slug: str,
        title_en: str,
        title_fa: str,
        fingerprint: str,
        total_parts: int = 1,
        instructor: Optional[str] = None,
        provider: Optional[str] = None,
        description_fa: Optional[str] = None,
        thumbnail_url: Optional[str] = None
    ) -> str:
        now = datetime.now(timezone.utc).isoformat()
        is_pg = getattr(self.processing_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        with self.processing_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            chk = f"SELECT id, source_fingerprint FROM courses WHERE slug = {p} OR source_discovery_id = {p}"
            cur.execute(chk, (slug, discovery_id))
            existing = cur.fetchone()

            if existing:
                real_id = existing["id"] if isinstance(existing, dict) else existing[0]
                upd = f"""
                    UPDATE courses
                    SET canonical_url = {p},
                        title_en = {p},
                        title_fa = {p},
                        instructor = COALESCE({p}, instructor),
                        provider = COALESCE({p}, provider),
                        description_fa = COALESCE({p}, description_fa),
                        thumbnail_url = COALESCE({p}, thumbnail_url),
                        source_fingerprint = {p},
                        total_parts = {p},
                        updated_at = {p}
                    WHERE id = {p}
                """
                cur.execute(upd, (canonical_url, title_en, title_fa, instructor, provider, description_fa, thumbnail_url, fingerprint, total_parts, now, real_id))
                return real_id

            new_id = f"crs_{uuid.uuid4().hex[:16]}"
            ins = f"""
                INSERT INTO courses (
                    id, source_discovery_id, canonical_url, slug, title_en, title_fa,
                    instructor, provider, description_fa, thumbnail_url,
                    source_fingerprint, status, total_parts, created_at, updated_at
                ) VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, 'METADATA_READY', {p}, {p}, {p})
            """
            cur.execute(ins, (new_id, discovery_id, canonical_url, slug, title_en, title_fa, instructor, provider, description_fa, thumbnail_url, fingerprint, total_parts, now, now))
            return new_id

    def get_course(self, course_id: str) -> Optional[Dict[str, Any]]:
        is_pg = getattr(self.processing_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        with self.processing_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            cur.execute(f"SELECT * FROM courses WHERE id = {p}", (course_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def insert_archive_part(
        self,
        course_id: str,
        part_number: int,
        file_name: str,
        download_url: str,
        expected_size: int = 0
    ) -> str:
        part_id = f"part_{course_id}_{part_number}"
        is_pg = getattr(self.processing_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        with self.processing_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            cur.execute(f"SELECT id FROM archive_parts WHERE course_id = {p} AND part_number = {p}", (course_id, part_number))
            existing = cur.fetchone()
            if existing:
                return existing["id"] if isinstance(existing, dict) else existing[0]

            ins = f"""
                INSERT INTO archive_parts (id, course_id, part_number, file_name, download_url, expected_size, status)
                VALUES ({p}, {p}, {p}, {p}, {p}, {p}, 'PENDING')
            """
            cur.execute(ins, (part_id, course_id, part_number, file_name, download_url, expected_size))
            return part_id

    def get_pending_archive_parts(self, course_id: str) -> List[Dict[str, Any]]:
        is_pg = getattr(self.processing_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        with self.processing_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            cur.execute(
                f"SELECT * FROM archive_parts WHERE course_id = {p} AND status = 'PENDING' ORDER BY part_number ASC",
                (course_id,)
            )
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def mark_archive_part_downloaded(self, part_id: str, local_path: str, downloaded_size: int, sha256: str = ""):
        now = datetime.now(timezone.utc).isoformat()
        is_pg = getattr(self.processing_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        with self.processing_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            upd = f"""
                UPDATE archive_parts
                SET status = 'DOWNLOADED', local_path = {p}, downloaded_size = {p}, sha256 = {p}, downloaded_at = {p}
                WHERE id = {p}
            """
            cur.execute(upd, (local_path, downloaded_size, sha256, now, part_id))

    def all_archive_parts_downloaded(self, course_id: str) -> bool:
        is_pg = getattr(self.processing_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        with self.processing_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            cur.execute(f"SELECT count(1) as cnt FROM archive_parts WHERE course_id = {p} AND status != 'DOWNLOADED'", (course_id,))
            row = cur.fetchone()
            rem = (row["cnt"] if isinstance(row, dict) else row[0]) if row else 0
            return rem == 0

    def enqueue_job(
        self,
        course_id: str,
        job_type: str,
        lesson_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        input_hash: Optional[str] = None,
        config_hash: Optional[str] = None
    ) -> str:
        key = idempotency_key or f"{course_id}:{lesson_id or 'none'}:{job_type}"
        is_pg = getattr(self.processing_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        with self.processing_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            cur.execute(f"SELECT id FROM dubbing_jobs WHERE idempotency_key = {p}", (key,))
            existing = cur.fetchone()
            if existing:
                return existing["id"] if isinstance(existing, dict) else existing[0]

            job_id = f"job_{uuid.uuid4().hex[:16]}"
            now = datetime.now(timezone.utc).isoformat()
            ins = f"""
                INSERT INTO dubbing_jobs (
                    id, idempotency_key, course_id, lesson_id, job_type,
                    stage, status, progress, attempt_count, max_attempts,
                    next_retry_at, input_hash, config_hash, created_at, updated_at
                ) VALUES ({p}, {p}, {p}, {p}, {p}, 'QUEUED', 'QUEUED', 0, 0, 3, {p}, {p}, {p}, {p}, {p})
            """
            cur.execute(ins, (job_id, key, course_id, lesson_id, job_type, now, input_hash, config_hash, now, now))
            return job_id

    def claim_job(self, worker_id: str, job_type: str, lease_seconds: int = 300) -> Optional[Dict[str, Any]]:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        expiry = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        fencing_token = uuid.uuid4().hex
        is_pg = getattr(self.processing_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"

        with self.processing_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            sel = f"""
                SELECT * FROM dubbing_jobs
                WHERE job_type = {p}
                  AND (
                      status = 'QUEUED'
                      OR (status = 'RETRY_WAIT' AND next_retry_at <= {p})
                      OR (status = 'PROCESSING' AND lease_expiry < {p})
                  )
                ORDER BY created_at ASC
                LIMIT 1
            """
            cur.execute(sel, (job_type, now, now))
            row = cur.fetchone()
            if not row:
                return None

            job = dict(row)
            job_id = job["id"]

            upd = f"""
                UPDATE dubbing_jobs
                SET status = 'PROCESSING',
                    lease_owner = {p},
                    fencing_token = {p},
                    attempt_count = attempt_count + 1,
                    lease_expiry = {p},
                    heartbeat = {p},
                    updated_at = {p}
                WHERE id = {p} AND (
                    status IN ('QUEUED', 'RETRY_WAIT')
                    OR (status = 'PROCESSING' AND lease_expiry < {p})
                )
            """
            cur.execute(upd, (worker_id, fencing_token, expiry, now, now, job_id, now))
            job["fencing_token"] = fencing_token
            job["lease_owner"] = worker_id
            job["lease_expiry"] = expiry
            job["status"] = "PROCESSING"
            job["attempt_count"] = job.get("attempt_count", 0) + 1
            return job

    def heartbeat_job(self, job_id: str, fencing_token: str, lease_seconds: int = 300) -> bool:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        expiry = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        is_pg = getattr(self.processing_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        with self.processing_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            upd = f"""
                UPDATE dubbing_jobs
                SET heartbeat = {p}, lease_expiry = {p}, updated_at = {p}
                WHERE id = {p} AND fencing_token = {p} AND status = 'PROCESSING'
            """
            cur.execute(upd, (now, expiry, now, job_id, fencing_token))
            return cur.rowcount > 0

    def complete_job(self, job_id: str, fencing_token: str, result_data: Dict[str, Any], cost_usd: float = 0.0) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        is_pg = getattr(self.processing_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        with self.processing_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            upd = f"""
                UPDATE dubbing_jobs
                SET status = 'COMPLETED',
                    progress = 100,
                    result_data = {p},
                    cost_usd = {p},
                    updated_at = {p}
                WHERE id = {p}
                  AND fencing_token = {p}
                  AND status = 'PROCESSING'
                  AND lease_expiry >= {p}
            """
            cur.execute(upd, (json.dumps(result_data), cost_usd, now, job_id, fencing_token, now))
            return cur.rowcount > 0

    def fail_job(self, job_id: str, fencing_token: str, error_code: str, error_details: str, retry_delay_sec: Optional[int] = None) -> bool:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        is_pg = getattr(self.processing_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        with self.processing_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            cur.execute(f"SELECT attempt_count, max_attempts FROM dubbing_jobs WHERE id = {p} AND fencing_token = {p}", (job_id, fencing_token))
            row = cur.fetchone()
            if not row:
                return False

            attempts = row["attempt_count"] if isinstance(row, dict) else row[0]
            max_attempts = row["max_attempts"] if isinstance(row, dict) else row[1]
            if attempts >= max_attempts:
                status = 'DEAD_LETTER'
                next_retry = None
            else:
                status = 'RETRY_WAIT'
                delay = retry_delay_sec or ((2 ** attempts) * 10 + random.uniform(1.0, 5.0))
                next_retry = (now_dt + timedelta(seconds=delay)).isoformat()

            upd = f"""
                UPDATE dubbing_jobs
                SET status = {p}, error_code = {p}, error_details = {p}, next_retry_at = {p}, updated_at = {p}
                WHERE id = {p} AND fencing_token = {p}
            """
            cur.execute(upd, (status, error_code, error_details, next_retry, now, job_id, fencing_token))
            return True

    def reclaim_expired_job_leases(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        is_pg = getattr(self.processing_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        with self.processing_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            cur.execute(f"SELECT id, attempt_count, max_attempts FROM dubbing_jobs WHERE status = 'PROCESSING' AND lease_expiry < {p}", (now,))
            rows = cur.fetchall()
            reclaimed = 0
            for r in rows:
                jid = r["id"] if isinstance(r, dict) else r[0]
                att = r["attempt_count"] if isinstance(r, dict) else r[1]
                max_att = r["max_attempts"] if isinstance(r, dict) else r[2]
                if att >= max_att:
                    cur.execute(f"UPDATE dubbing_jobs SET status = 'DEAD_LETTER', error_code = 'LEASE_EXPIRED_MAX_ATTEMPTS' WHERE id = {p}", (jid,))
                else:
                    cur.execute(f"UPDATE dubbing_jobs SET status = 'QUEUED', lease_owner = NULL, fencing_token = NULL WHERE id = {p}", (jid,))
                reclaimed += 1
            return reclaimed

    def upsert_chapter_and_lessons(self, course_id: str, manifest: Dict[str, Any]) -> List[str]:
        now = datetime.now(timezone.utc).isoformat()
        created_lesson_ids = []
        is_pg = getattr(self.processing_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        with self.processing_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            for ch in manifest.get("chapters", []):
                ch_id = f"ch_{uuid.uuid4().hex[:12]}"
                ch_title = ch.get("chapter_title", "فصل اول")
                order_index = ch.get("order_index", 1)
                
                cur.execute(f"INSERT INTO chapters (id, course_id, title_fa, order_index, created_at) VALUES ({p}, {p}, {p}, {p}, {p})",
                            (ch_id, course_id, ch_title, order_index, now))

                for les in ch.get("lessons", []):
                    les_id = f"les_{uuid.uuid4().hex[:12]}"
                    les_num = les.get("lesson_number", 1)
                    title_en = les.get("title", f"Lesson {les_num}")
                    title_fa = les.get("title_fa", title_en)
                    duration = les.get("duration_seconds", 0.0)
                    video_path = les.get("relative_path", "")

                    ins_les = f"""
                        INSERT INTO lessons (
                            id, chapter_id, course_id, title_en, title_fa,
                            lesson_number, video_source_path, duration_seconds, status, created_at, updated_at
                        ) VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, 'PENDING', {p}, {p})
                    """
                    cur.execute(ins_les, (les_id, ch_id, course_id, title_en, title_fa, les_num, video_path, duration, now, now))
                    created_lesson_ids.append(les_id)

            cur.execute(f"UPDATE courses SET total_lessons = {p}, status = 'EXTRACTED' WHERE id = {p}", (len(created_lesson_ids), course_id))
        return created_lesson_ids

    def record_upload_intent(self, asset_id: str, lesson_id: Optional[str], file_path: str, file_size: int, sha256: str) -> str:
        intent_id = f"intent_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        is_pg = getattr(self.processing_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        with self.processing_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            ins = f"""
                INSERT INTO upload_intents (id, lesson_id, asset_id, file_path, file_size, sha256, status, created_at)
                VALUES ({p}, {p}, {p}, {p}, {p}, {p}, 'UPLOADING', {p})
            """
            cur.execute(ins, (intent_id, lesson_id, asset_id, file_path, file_size, sha256, now))
        return intent_id

    def verify_storage_upload(
        self,
        asset_id: str,
        lesson_id: Optional[str],
        chat_id: str,
        message_id: int,
        file_id: str,
        file_unique_id: str,
        file_size: int,
        sha256: str,
        mime_type: str = "video/mp4",
        duration_seconds: float = 0.0
    ):
        now = datetime.now(timezone.utc).isoformat()
        obj_id = f"stg_{uuid.uuid4().hex[:12]}"
        is_pg = getattr(self.processing_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        with self.processing_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            cur.execute(f"UPDATE upload_intents SET status = 'VERIFIED', completed_at = {p} WHERE asset_id = {p}", (now, asset_id))

            ins = f"""
                INSERT INTO storage_objects (
                    id, asset_id, lesson_id, storage_provider, chat_id, message_id,
                    file_id, file_unique_id, file_size, sha256, mime_type, duration_seconds, status, created_at
                ) VALUES ({p}, {p}, {p}, 'TELEGRAM_CDN', {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, 'VERIFIED', {p})
            """
            cur.execute(ins, (obj_id, asset_id, lesson_id, chat_id, message_id, file_id, file_unique_id, file_size, sha256, mime_type, duration_seconds, now))

            if lesson_id:
                upd_les = f"UPDATE lessons SET video_stream_url = {p}, status = 'UPLOADED', updated_at = {p} WHERE id = {p}"
                cur.execute(upd_les, (f"/api/v1/stream/{asset_id}", now, lesson_id))

    def record_qc_report(self, lesson_id: str, passed: bool, metrics: Dict[str, Any], failure_reasons: List[str]):
        now = datetime.now(timezone.utc).isoformat()
        qc_id = f"qc_{uuid.uuid4().hex[:12]}"
        is_pg = getattr(self.processing_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        with self.processing_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            ins = f"""
                INSERT INTO qc_reports (id, lesson_id, passed, metrics_json, failure_reasons_json, human_review_required, created_at)
                VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p})
            """
            cur.execute(ins, (qc_id, lesson_id, bool(passed), json.dumps(metrics), json.dumps(failure_reasons), not passed, now))

            new_status = 'QC_PASSED' if passed else 'QC_FAILED'
            cur.execute(f"UPDATE lessons SET status = {p}, updated_at = {p} WHERE id = {p}", (new_status, now, lesson_id))

    def check_course_completeness(self, course_id: str) -> Dict[str, Any]:
        is_pg = getattr(self.processing_backend, "dialect", "") == Dialect.POSTGRES
        p = "%s" if is_pg else "?"
        with self.processing_backend.transaction() as conn:
            cur = conn.cursor() if hasattr(conn, "cursor") else conn
            cur.execute(f"SELECT * FROM courses WHERE id = {p}", (course_id,))
            course = cur.fetchone()
            if not course:
                return {"is_complete": False, "reason": "Course not found"}

            cur.execute(f"SELECT * FROM lessons WHERE course_id = {p}", (course_id,))
            lessons = cur.fetchall()
            if not lessons:
                return {"is_complete": False, "reason": "No lessons registered"}

            total = len(lessons)
            uploaded_count = 0
            qc_passed_count = 0

            for les in lessons:
                les_id = les["id"] if isinstance(les, dict) else les[0]
                cur.execute(f"SELECT passed FROM qc_reports WHERE lesson_id = {p} ORDER BY created_at DESC LIMIT 1", (les_id,))
                qc = cur.fetchone()
                if qc and (qc["passed"] if isinstance(qc, dict) else qc[0]):
                    qc_passed_count += 1

                cur.execute(f"SELECT status FROM storage_objects WHERE lesson_id = {p}", (les_id,))
                stg = cur.fetchone()
                if stg and (stg["status"] if isinstance(stg, dict) else stg[0]) == "VERIFIED":
                    uploaded_count += 1

            is_complete = (uploaded_count == total and qc_passed_count == total)
            return {
                "is_complete": is_complete,
                "course_id": course_id,
                "total_lessons": total,
                "uploaded_lessons": uploaded_count,
                "qc_passed_lessons": qc_passed_count
            }

    def get_connection(self):
        return self.get_processing_conn()


DatabaseManager = DualDatabaseManager
