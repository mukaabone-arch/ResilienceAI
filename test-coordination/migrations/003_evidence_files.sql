-- ResilienceAI — Test Coordination, migration 003: uploaded evidence files (Screen 09, RA-057)
BEGIN;

ALTER TABLE test_evidence
  ADD COLUMN storage_backend varchar(20) NOT NULL DEFAULT 'link'
      CHECK (storage_backend IN ('link', 'local', 's3')),
  ADD COLUMN size_bytes    bigint CHECK (size_bytes IS NULL OR size_bytes >= 0),
  ADD COLUMN content_type  varchar(120),
  ADD COLUMN scan_status   varchar(20) NOT NULL DEFAULT 'NotScanned'
      CHECK (scan_status IN ('NotScanned', 'Clean', 'Infected', 'Error')),
  ADD COLUMN last_verified_at timestamptz;

-- An uploaded file must carry its SHA-256 checksum.
ALTER TABLE test_evidence
  ADD CONSTRAINT test_evidence_file_checksum CHECK (storage_backend = 'link' OR checksum IS NOT NULL);

COMMIT;
