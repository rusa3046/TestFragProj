-- Who wrote the comment, as an opaque stable id.
--
-- The product ranks by *distinct commenters*, not by claim rows. Those are
-- very different numbers: one person writing "this is a BR540 dupe" in
-- four threads is one person, and a ranking that counts it as four is the
-- exact failure mode — a loud minority looking like consensus — that the
-- evidence-first pitch exists to avoid.
--
-- The identity was already being retained inside raw_json (YouTube's
-- authorChannelId, Reddit's author). Leaving it there would have meant
-- every ranking query hard-coding a source-specific JSON path, unindexed,
-- in the hottest query in the system.
--
-- Stored as the platform's pseudonymous id, never the display name. The
-- ranking needs to know two comments came from one person; it does not
-- need to know who that person is, and display names end up on public
-- pages by accident in a way channel ids do not.
--
-- '' means unknown, not "one shared author". Every count in query.py
-- treats an empty author as its own distinct commenter (falling back to
-- comment id) rather than silently collapsing every anonymous row into
-- one, which would under-count instead of over-counting — the safer
-- direction, but still wrong if it happened invisibly.

ALTER TABLE comments ADD COLUMN author_id TEXT NOT NULL DEFAULT '';

-- Backfill from the payload already on disk. Both paths are checked for
-- every row regardless of source: the corpus predates this column and a
-- re-import of an old export must land the same way as a fresh ingest.
-- SQLite spelled these `json_extract(raw_json, '$.path')`. Postgres
-- reads the same paths through jsonb: `->` walks in, `->>` comes out as
-- text. A missing key is NULL either way, which is what `coalesce` is
-- here for.
UPDATE comments
   SET author_id = coalesce(
           raw_json::jsonb -> 'authorChannelId' ->> 'value',  -- YouTube
           raw_json::jsonb ->> 'author',                      -- Reddit
           ''
       )
 WHERE author_id = '';

CREATE INDEX IF NOT EXISTS idx_comments_author ON comments (author_id);
