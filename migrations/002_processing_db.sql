-- Processing / Catalog Database Schema

CREATE TABLE IF NOT EXISTS courses (
    id VARCHAR(64) PRIMARY KEY,
    source_discovery_id VARCHAR(64) UNIQUE,
    canonical_url TEXT NOT NULL,
    slug VARCHAR(255) NOT NULL UNIQUE,
    title_en TEXT NOT NULL,
    title_fa TEXT NOT NULL,
    instructor VARCHAR(255),
    provider VARCHAR(100),
    description_fa TEXT,
    thumbnail_url TEXT,
    category VARCHAR(100) DEFAULT 'General',
    level VARCHAR(50) DEFAULT 'Intermediate',
    source_fingerprint VARCHAR(64),
    status VARCHAR(50) NOT NULL DEFAULT 'METADATA_READY',
    -- METADATA_READY, DOWNLOADING, EXTRACTED, PROCESSING, READY, PUBLISHED, NEEDS_REVIEW, FAILED, PAUSED
    total_parts INT DEFAULT 1,
    total_lessons INT DEFAULT 0,
    completed_lessons INT DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS course_versions (
    id VARCHAR(64) PRIMARY KEY,
    course_id VARCHAR(64) NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    version_number INT NOT NULL,
    source_fingerprint VARCHAR(64) NOT NULL,
    manifest_json TEXT DEFAULT '{}',
    is_sealed BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (course_id, version_number)
);

CREATE TABLE IF NOT EXISTS chapters (
    id VARCHAR(64) PRIMARY KEY,
    course_id VARCHAR(64) NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    title_en TEXT,
    title_fa TEXT NOT NULL,
    order_index INT NOT NULL DEFAULT 1,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS lessons (
    id VARCHAR(64) PRIMARY KEY,
    chapter_id VARCHAR(64) NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    course_id VARCHAR(64) NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    title_en TEXT,
    title_fa TEXT NOT NULL,
    lesson_number INT NOT NULL DEFAULT 1,
    video_source_path TEXT,
    duration_seconds FLOAT DEFAULT 0.0,
    audio_stream_url TEXT,
    video_stream_url TEXT,
    subtitle_fa_url TEXT,
    subtitle_en_url TEXT,
    status VARCHAR(50) NOT NULL DEFAULT 'PENDING',
    -- PENDING, PROCESSING, TRANSLATED, SYNTHESIZED, MUXED, QC_PASSED, QC_FAILED, UPLOADED, FAILED
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS archive_parts (
    id VARCHAR(64) PRIMARY KEY,
    course_id VARCHAR(64) NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    part_number INT NOT NULL,
    file_name VARCHAR(255) NOT NULL,
    download_url TEXT NOT NULL,
    local_path TEXT,
    expected_size BIGINT,
    downloaded_size BIGINT DEFAULT 0,
    sha256 VARCHAR(64),
    status VARCHAR(50) NOT NULL DEFAULT 'PENDING', -- PENDING, DOWNLOADING, DOWNLOADED, VERIFIED, FAILED
    error_message TEXT,
    downloaded_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS source_files (
    id VARCHAR(64) PRIMARY KEY,
    course_id VARCHAR(64) NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    lesson_id VARCHAR(64) REFERENCES lessons(id) ON DELETE SET NULL,
    relative_path TEXT NOT NULL,
    file_size BIGINT NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    mime_type VARCHAR(100),
    file_type VARCHAR(50) NOT NULL, -- VIDEO, SUBTITLE, ATTACHMENT, CHEATSHEET
    duration_seconds FLOAT,
    video_codec VARCHAR(50),
    audio_codec VARCHAR(50),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dubbing_jobs (
    id VARCHAR(64) PRIMARY KEY,
    idempotency_key VARCHAR(128) UNIQUE,
    course_id VARCHAR(64) NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    lesson_id VARCHAR(64) REFERENCES lessons(id) ON DELETE CASCADE,
    job_type VARCHAR(50) NOT NULL, -- TRANSFER, EXTRACTION, ASR, TRANSLATION, TTS, MUX, QC, UPLOAD
    stage VARCHAR(100) NOT NULL DEFAULT 'QUEUED',
    status VARCHAR(50) NOT NULL DEFAULT 'QUEUED', -- QUEUED, CLAIMED, PROCESSING, COMPLETED, FAILED, RETRY_WAIT, DEAD_LETTER
    progress INT NOT NULL DEFAULT 0,
    attempt_count INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 3,
    next_retry_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    fencing_token VARCHAR(64),
    lease_owner VARCHAR(100),
    lease_expiry TIMESTAMP WITH TIME ZONE,
    heartbeat TIMESTAMP WITH TIME ZONE,
    cost_usd FLOAT DEFAULT 0.0,
    input_hash VARCHAR(64),
    config_hash VARCHAR(64),
    error_code VARCHAR(100),
    error_details TEXT,
    result_data TEXT DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_type ON dubbing_jobs(status, job_type);
CREATE INDEX IF NOT EXISTS idx_jobs_course_lesson ON dubbing_jobs(course_id, lesson_id);
CREATE INDEX IF NOT EXISTS idx_jobs_lease_expiry ON dubbing_jobs(status, lease_expiry);

CREATE TABLE IF NOT EXISTS segments (
    id VARCHAR(64) PRIMARY KEY,
    lesson_id VARCHAR(64) NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    segment_index INT NOT NULL,
    start_time FLOAT NOT NULL,
    end_time FLOAT NOT NULL,
    transcript_en TEXT,
    translation_fa TEXT,
    audio_path TEXT,
    duration_ratio FLOAT DEFAULT 1.0,
    status VARCHAR(50) NOT NULL DEFAULT 'PENDING'
);

CREATE TABLE IF NOT EXISTS qc_reports (
    id VARCHAR(64) PRIMARY KEY,
    lesson_id VARCHAR(64) NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    passed BOOLEAN NOT NULL DEFAULT FALSE,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    failure_reasons_json TEXT NOT NULL DEFAULT '[]',
    human_review_required BOOLEAN DEFAULT FALSE,
    reviewed_by VARCHAR(100),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS upload_intents (
    id VARCHAR(64) PRIMARY KEY,
    lesson_id VARCHAR(64) REFERENCES lessons(id) ON DELETE CASCADE,
    asset_id VARCHAR(64) NOT NULL UNIQUE,
    file_path TEXT NOT NULL,
    file_size BIGINT NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'PENDING', -- PENDING, UPLOADING, VERIFIED, FAILED
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS storage_objects (
    id VARCHAR(64) PRIMARY KEY,
    asset_id VARCHAR(64) NOT NULL UNIQUE,
    lesson_id VARCHAR(64) REFERENCES lessons(id) ON DELETE CASCADE,
    storage_provider VARCHAR(50) NOT NULL DEFAULT 'TELEGRAM_CDN',
    chat_id VARCHAR(100) NOT NULL,
    message_id BIGINT NOT NULL,
    file_id TEXT NOT NULL,
    file_unique_id VARCHAR(100),
    file_size BIGINT NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    mime_type VARCHAR(100) NOT NULL,
    duration_seconds FLOAT,
    status VARCHAR(50) NOT NULL DEFAULT 'VERIFIED',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS taxonomy_categories (
    id VARCHAR(64) PRIMARY KEY,
    name_fa VARCHAR(255) NOT NULL,
    normalized_name VARCHAR(255) NOT NULL UNIQUE,
    slug VARCHAR(255) NOT NULL UNIQUE,
    parent_id VARCHAR(64) REFERENCES taxonomy_categories(id) ON DELETE SET NULL,
    remote_id VARCHAR(100),
    description TEXT,
    synonyms_json TEXT DEFAULT '[]',
    depth INT NOT NULL DEFAULT 1,
    is_auto_created BOOLEAN DEFAULT FALSE,
    is_locked BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS taxonomy_tags (
    id VARCHAR(64) PRIMARY KEY,
    name_fa VARCHAR(100) NOT NULL,
    normalized_name VARCHAR(100) NOT NULL UNIQUE,
    slug VARCHAR(100) NOT NULL UNIQUE,
    remote_id VARCHAR(100),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS course_taxonomy (
    id VARCHAR(64) PRIMARY KEY,
    course_id VARCHAR(64) NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    category_id VARCHAR(64) REFERENCES taxonomy_categories(id) ON DELETE SET NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    proposed_json TEXT NOT NULL DEFAULT '{}',
    status VARCHAR(50) NOT NULL DEFAULT 'RESOLVED', -- RESOLVED, NEEDS_REVIEW, LOCKED
    evidence TEXT,
    confidence FLOAT DEFAULT 1.0,
    is_locked BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS course_releases (
    id VARCHAR(64) PRIMARY KEY,
    course_id VARCHAR(64) NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    version_number INT NOT NULL DEFAULT 1,
    release_hash VARCHAR(64) NOT NULL UNIQUE,
    manifest_json TEXT NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'READY', -- READY, SUPERSEDED
    is_immutable BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS publications (
    id VARCHAR(64) PRIMARY KEY,
    release_id VARCHAR(64) NOT NULL REFERENCES course_releases(id) ON DELETE CASCADE,
    course_id VARCHAR(64) NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    platform VARCHAR(50) NOT NULL DEFAULT 'RPIM_WEB',
    remote_course_id VARCHAR(100),
    visibility VARCHAR(50) NOT NULL DEFAULT 'DRAFT', -- DRAFT, PUBLISHED, HIDDEN
    verification_status VARCHAR(50) NOT NULL DEFAULT 'PENDING', -- PENDING, VERIFIED, FAILED
    error_message TEXT,
    published_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS inbox_events (
    id VARCHAR(64) PRIMARY KEY,
    source_event_id VARCHAR(64) NOT NULL UNIQUE,
    aggregate_id VARCHAR(64),
    event_type VARCHAR(100) NOT NULL,
    payload_json TEXT NOT NULL,
    processed BOOLEAN NOT NULL DEFAULT FALSE,
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_inbox_processed ON inbox_events(processed, created_at);
