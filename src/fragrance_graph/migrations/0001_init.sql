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
    subject_frag_id INTEGER REFERENCES fragrances (id),
    object_frag_id INTEGER REFERENCES fragrances (id),
    raw_subject_text TEXT NOT NULL,
    raw_object_text TEXT,
    confidence REAL NOT NULL,
    extraction_model TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claims_comment_id
    ON claims (comment_id);

CREATE TABLE IF NOT EXISTS eval_labels (
    comment_id INTEGER NOT NULL REFERENCES comments (id),
    labeled_json TEXT NOT NULL,
    labeler TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (comment_id, labeler)
);
