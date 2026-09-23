PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS works (
    id INTEGER PRIMARY KEY,
    canonical_title TEXT NOT NULL,
    canonical_author TEXT NOT NULL,
    language TEXT,
    original_publication_date TEXT,
    identity_confidence REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(canonical_title, canonical_author)
);

CREATE TABLE IF NOT EXISTS editions (
    id INTEGER PRIMARY KEY,
    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    calibre_book_id INTEGER,
    calibre_library TEXT,
    title TEXT NOT NULL,
    author TEXT NOT NULL,
    isbn TEXT,
    publisher TEXT,
    publication_date TEXT,
    series TEXT,
    series_index REAL,
    language TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(calibre_library, calibre_book_id)
);

CREATE TABLE IF NOT EXISTS research_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    provider TEXT NOT NULL,
    depth TEXT NOT NULL,
    budget_usd REAL,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'RUNNING'
);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY,
    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    source_name TEXT,
    source_url TEXT,
    citation_text TEXT,
    retrieved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(work_id, source_url, citation_text)
);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    value_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    research_schema_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'RESEARCHED',
    note TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(work_id, field_name)
);

CREATE TABLE IF NOT EXISTS fact_evidence (
    fact_id INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
    PRIMARY KEY(fact_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS significance_claims (
    id INTEGER PRIMARY KEY,
    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    claim TEXT NOT NULL,
    confidence REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'RESEARCHED',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS claim_evidence (
    claim_id INTEGER NOT NULL REFERENCES significance_claims(id) ON DELETE CASCADE,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
    PRIMARY KEY(claim_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS award_evidence (
    id INTEGER PRIMARY KEY,
    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    award_name TEXT NOT NULL,
    award_year INTEGER,
    category TEXT,
    result TEXT NOT NULL CHECK(result IN ('winner', 'finalist', 'nominee')),
    source_name TEXT NOT NULL,
    source_identifier TEXT NOT NULL,
    source_url TEXT,
    confidence REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'RESEARCHED',
    retrieved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(work_id, source_name, source_identifier, result)
);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY,
    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    rubric_version TEXT NOT NULL,
    total REAL NOT NULL,
    confidence REAL,
    components_json TEXT NOT NULL,
    why_read TEXT,
    scored_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(work_id, rubric_version)
);

-- Score kinds are kept separate so significance and personal preference can
-- evolve independently. `scores` remains as a legacy table for old databases.
CREATE TABLE IF NOT EXISTS derived_scores (
    id INTEGER PRIMARY KEY,
    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    score_kind TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    total REAL NOT NULL,
    confidence REAL,
    components_json TEXT NOT NULL,
    explanation TEXT,
    scored_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(work_id, score_kind, rubric_version)
);

CREATE TABLE IF NOT EXISTS significance_provider_attempts (
    id INTEGER PRIMARY KEY,
    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('MATCH', 'MISS', 'ERROR')),
    error TEXT,
    attempted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(work_id, provider)
);

CREATE TABLE IF NOT EXISTS source_imports (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_filename TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source, content_sha256)
);

CREATE TABLE IF NOT EXISTS source_records (
    id INTEGER PRIMARY KEY,
    import_id INTEGER NOT NULL REFERENCES source_imports(id) ON DELETE CASCADE,
    work_id INTEGER REFERENCES works(id) ON DELETE SET NULL,
    source_record_id TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    match_status TEXT NOT NULL CHECK(match_status IN ('MATCHED', 'CREATED', 'AMBIGUOUS')),
    match_method TEXT,
    UNIQUE(import_id, source_record_id)
);

CREATE TABLE IF NOT EXISTS reading_status_observations (
    id INTEGER PRIMARY KEY,
    source_record_id INTEGER NOT NULL UNIQUE REFERENCES source_records(id) ON DELETE CASCADE,
    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('unread', 'currently_reading', 'read', 'abandoned')),
    date_read TEXT
);

CREATE TABLE IF NOT EXISTS rating_observations (
    id INTEGER PRIMARY KEY,
    source_record_id INTEGER NOT NULL UNIQUE REFERENCES source_records(id) ON DELETE CASCADE,
    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    rating REAL NOT NULL,
    scale_max REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS owned_editions (
    id INTEGER PRIMARY KEY,
    source_record_id INTEGER NOT NULL UNIQUE REFERENCES source_records(id) ON DELETE CASCADE,
    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    format TEXT,
    isbn TEXT,
    owned_count INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tag_observations (
    id INTEGER PRIMARY KEY,
    source_record_id INTEGER NOT NULL REFERENCES source_records(id) ON DELETE CASCADE,
    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    UNIQUE(source_record_id, tag)
);

CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY,
    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS metadata_lookups (
    id INTEGER PRIMARY KEY,
    edition_id INTEGER NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    query_key TEXT NOT NULL,
    source_url TEXT,
    identity_confidence REAL,
    raw_json TEXT NOT NULL,
    normalized_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'MATCH',
    retrieved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(edition_id, provider, query_key)
);

CREATE TABLE IF NOT EXISTS metadata_proposals (
    id INTEGER PRIMARY KEY,
    edition_id INTEGER NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    lookup_id INTEGER NOT NULL REFERENCES metadata_lookups(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    current_value_json TEXT,
    proposed_value_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PROPOSED',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(edition_id, field_name, status)
);


CREATE TABLE IF NOT EXISTS metadata_issues (
    id INTEGER PRIMARY KEY,
    edition_id INTEGER NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    classification TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(edition_id, classification, provider, status)
);
