-- Where a comment was found, and *why we were looking there*.
--
-- The product's headline number is "N people across M videos". `M` exists
-- because three commenters in one comment section may be replying to each
-- other, and a single thread cannot support "three independent
-- observations". That guard is real but weaker than it reads.
--
-- Measured on the committed corpus: four of the six pairs that currently
-- clear the publishing gate are backed by videos that were *all retrieved
-- by the same search query*. Three different `parfums de marly layton
-- dupe` videos are three separate comment sections, so `min_sources >= 2`
-- passes them — but they are three rooms in which the same question was
-- asked of an audience assembled for that question. Counting them as
-- independent overstates the evidence in exactly the way the gate exists
-- to prevent.
--
-- Query diversity is the deterministic version of that concern. It needs
-- no classifier and no judgement: either two different searches found the
-- evidence or they did not. (Whether a *comment* was spontaneous or
-- prompted by the video's framing is a fuzzier question, and a later one.)
--
-- Retrieval is many-to-many on purpose. The same video can be found by
-- several queries, and later runs re-find videos already known — so a
-- single `retrieval_query` column on `comments` would have to pick one and
-- discard the rest, destroying the very count this migration exists to
-- make possible. Discovery is therefore its own table, and metadata about
-- the video itself is stored once rather than repeated on every comment.

CREATE TABLE IF NOT EXISTS videos (
    source        TEXT NOT NULL,
    video_id      TEXT NOT NULL,
    -- Nullable, and routinely null. A video is known to exist as soon as a
    -- search returns it; its title costs a separate `videos.list` call.
    -- Recording the row now and filling the title later is why ingest is
    -- not blocked on metadata it does not yet need.
    title         TEXT,
    channel_id    TEXT,
    channel_title TEXT,
    published_at  INTEGER,
    -- When the title was last fetched. Null means "never".
    fetched_at    TEXT,
    PRIMARY KEY (source, video_id)
);

CREATE TABLE IF NOT EXISTS video_discoveries (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,
    video_id        TEXT NOT NULL,
    -- The search string that surfaced this video. Not the video's title,
    -- and not a topic: it is what *we* asked for, which is the part that
    -- carries our own sampling bias.
    retrieval_query TEXT NOT NULL,
    retrieved_at    TEXT,
    -- Groups the discoveries made by one ingest run, so a run's whole
    -- footprint stays attributable after the fact.
    discovery_run   TEXT NOT NULL DEFAULT '',
    UNIQUE (source, video_id, retrieval_query, discovery_run)
);

CREATE INDEX IF NOT EXISTS idx_discoveries_video
    ON video_discoveries (source, video_id);

-- The join key, promoted out of raw_json.
--
-- Every independence count in query.py reaches for the video through
-- `json_extract(raw_json, '$.videoId')` — a source-specific JSON path,
-- unindexed, in the hottest query in the system, and repeated in four
-- places. Same reasoning as migration 0006 promoting author_id.
ALTER TABLE comments ADD COLUMN video_id TEXT;

-- Backfill from the payload already on disk, so a re-imported old export
-- lands identically to a fresh ingest. Reddit rows have no videoId and
-- stay null; `source_channel` remains their subdivision.
UPDATE comments
   SET video_id = json_extract(raw_json, '$.videoId')
 WHERE video_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_comments_video ON comments (video_id);

-- Every video the corpus already has comments for is a video we know
-- exists, whether or not we have fetched its title.
INSERT OR IGNORE INTO videos (source, video_id)
SELECT DISTINCT source, video_id
  FROM comments
 WHERE video_id IS NOT NULL AND video_id <> '';
