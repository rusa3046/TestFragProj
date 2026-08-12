-- Claims the model produced and validation deleted.
--
-- Measured at 21-24% of everything emitted, stable across runs, and those
-- deletions account for nearly every remaining false negative in the eval.
-- Until now the breakdown was printed once and lost, so "which comment,
-- and what did it actually say" could only be answered by paying to
-- extract again.
--
-- The payload is stored verbatim as the model emitted it, not as a parsed
-- Claim — a Claim is exactly what it failed to become, and normalising it
-- on the way in would discard the shape that explains the rejection.

CREATE TABLE IF NOT EXISTS rejected_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_id INTEGER NOT NULL REFERENCES comments (id),
    -- The validator's own message, kept verbatim so it groups exactly as
    -- it appears in the run log.
    reason TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    extraction_model TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rejected_reason
    ON rejected_claims (reason);

CREATE INDEX IF NOT EXISTS idx_rejected_comment
    ON rejected_claims (comment_id);
