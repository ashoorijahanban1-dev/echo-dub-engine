-- Discovery Database Schema (PostgreSQL & SQLite compatible)

CREATE TABLE IF NOT EXISTS discovered_courses (
    id VARCHAR(64) PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    canonical_url TEXT NOT NULL UNIQUE,
    slug VARCHAR(255) NOT NULL,
    source_identifier VARCHAR(100) NOT NULL DEFAULT 'downloadly.ir',
    title_en TEXT,
    title_fa TEXT,
    source_fingerprint VARCHAR(64),
    status VARCHAR(50) NOT NULL DEFAULT 'DISCOVERED', -- DISCOVERED, CLAIMED, DISPATCHED, REJECTED, IGNORED
    claim_state VARCHAR(50) NOT NULL DEFAULT 'UNCLAIMED', -- UNCLAIMED, CLAIMED, RELEASED
    lease_owner VARCHAR(100),
    lease_expiry TIMESTAMP WITH TIME ZONE,
    claim_token VARCHAR(64),
    heartbeat TIMESTAMP WITH TIME ZONE,
    next_eligible_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    first_seen_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    metadata_json TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_discovered_status_eligible ON discovered_courses (status, next_eligible_at);
CREATE INDEX IF NOT EXISTS idx_discovered_canonical ON discovered_courses (canonical_url);

CREATE TABLE IF NOT EXISTS discovery_outbox (
    id VARCHAR(64) PRIMARY KEY,
    aggregate_id VARCHAR(64) NOT NULL,
    event_type VARCHAR(100) NOT NULL, -- course.discovered, course.dispatched
    payload_json TEXT NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'PENDING', -- PENDING, SENT, FAILED, DEAD_LETTER
    attempts INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 5,
    next_retry_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_outbox_status_created ON discovery_outbox (status, next_retry_at);
