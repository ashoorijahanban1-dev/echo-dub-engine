-- Migration 003: Conversational Dubbing & Style Versioning Support
-- Adds support for fa_conversational_v1, linguistic QC tracking, and DRAFT publish status

ALTER TABLE segments ADD COLUMN IF NOT EXISTS style_version VARCHAR(50) DEFAULT 'fa_formal_v0';
ALTER TABLE segments ADD COLUMN IF NOT EXISTS rewritten_fa TEXT;

ALTER TABLE lessons ADD COLUMN IF NOT EXISTS dubbing_style VARCHAR(50) DEFAULT 'conversational_professional';
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS publish_status VARCHAR(50) DEFAULT 'DRAFT';

CREATE INDEX IF NOT EXISTS idx_segments_style_version ON segments(style_version);
CREATE INDEX IF NOT EXISTS idx_lessons_publish_status ON lessons(publish_status);
