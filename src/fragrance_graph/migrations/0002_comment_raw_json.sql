-- Retain the full Reddit payload per comment. Fields we don't care about
-- today become fields we need in phase 3, and re-fetching old comments is
-- unreliable: accounts get deleted, posts get removed, scores drift.
--
-- NOT NULL with a '{}' default so downstream json parsing never has to
-- special-case a missing payload.
ALTER TABLE comments ADD COLUMN raw_json TEXT NOT NULL DEFAULT '{}';
