CREATE TABLE IF NOT EXISTS fragrances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT NOT NULL,
    brand TEXT,
    house_year INTEGER,
    aliases TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    body TEXT NOT NULL,
    permalink TEXT NOT NULL,
    created_utc INTEGER NOT NULL,
    subreddit TEXT NOT NULL,
    score INTEGER NOT NULL DEFAULT 0,
    extracted_at TEXT,
    UNIQUE (source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_comments_extracted_at
    ON comments (extracted_at);

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_id INTEGER NOT NULL REFERENCES comments (id),
    claim_type TEXT NOT NULL,
    -- Whether raw_object_text names another fragrance (binary edge) or a
    -- non-fragrance tag like "weddings" / "old money" (unary attribute).
    -- Derived from claim_type at write time and stored denormalized so
    -- downstream queries can filter without knowing the mapping.
    object_kind TEXT NOT NULL CHECK (object_kind IN ('FRAGRANCE', 'TAG', 'NONE')),
    subject_frag_id INTEGER REFERENCES fragrances (id),
    object_frag_id INTEGER REFERENCES fragrances (id),
    raw_subject_text TEXT NOT NULL,
    raw_object_text TEXT,
    confidence REAL NOT NULL,
    -- Substring of comments.body the LLM cited as support for this claim.
    evidence_span TEXT NOT NULL,
    -- 1 if evidence_span was found in comments.body at write time, else 0.
    -- Unverified spans mean the model paraphrased instead of quoting.
    evidence_verified INTEGER NOT NULL DEFAULT 0 CHECK (evidence_verified IN (0, 1)),
    extraction_model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- An object is required for FRAGRANCE/TAG claims and forbidden for NONE.
    CHECK (
        (object_kind = 'NONE' AND raw_object_text IS NULL)
        OR (object_kind IN ('FRAGRANCE', 'TAG') AND raw_object_text IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_claims_comment_id
    ON claims (comment_id);

-- Supports the phase-3 graph build: fragrance-to-fragrance edges only,
-- restricted to verified evidence.
CREATE INDEX IF NOT EXISTS idx_claims_edges
    ON claims (object_kind, claim_type, evidence_verified);

CREATE TABLE IF NOT EXISTS eval_labels (
    comment_id INTEGER NOT NULL REFERENCES comments (id),
    labeled_json TEXT NOT NULL,
    labeler TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (comment_id, labeler)
);
