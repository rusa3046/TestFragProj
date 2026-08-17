-- Declared notes: what an official source SAYS is in the bottle.
--
-- This table is the other half of FACET's central separation. Community
-- perception lives in claims/evidence with counted humans behind every
-- sentence; THIS table holds what a brand or retailer lists, with the
-- source pinned per row. The two never mix: a brand listing "rose" is a
-- fact about the listing, not evidence the bottle smells rose-forward,
-- and nothing here may ever enter an evidence count. Renderers show the
-- two side by side ("Official notes include rose and lychee" next to
-- "9 of 12 wearers call it rose-forward") -- that contrast is the
-- product, and it only exists while the storage stays separate.
--
-- claim_type is the declarer: brand_declared (the house's own page,
-- curated by hand into data/curation/declared-notes.jsonl) outranks
-- retailer_declared (extracted from a retailer listing's explicit
-- "Notes:" list at retail-ingest time -- the LIST only, never the prose
-- around it). raw_note is verbatim from the source; canonical_note is a
-- conservative lowercase/trim, no vocabulary collapse -- merging
-- "Turkish rose" into "rose" is a judgement the reader should get to
-- see, not one storage makes silently.

CREATE TABLE IF NOT EXISTS fragrance_note_claim (
    id            BIGSERIAL PRIMARY KEY,
    fragrance_id  BIGINT NOT NULL REFERENCES fragrances(id) ON DELETE CASCADE,
    raw_note      TEXT   NOT NULL,
    canonical_note TEXT  NOT NULL,
    note_stage    TEXT   NOT NULL DEFAULT 'unspecified'
                  CHECK (note_stage IN ('top', 'heart', 'base', 'unspecified')),
    claim_type    TEXT   NOT NULL
                  CHECK (claim_type IN ('brand_declared', 'retailer_declared')),
    source_name   TEXT   NOT NULL,
    source_url    TEXT   NOT NULL DEFAULT '',
    retrieved_at  TIMESTAMPTZ,
    verification_status TEXT NOT NULL DEFAULT 'unverified'
);

-- One declarer says a note once per bottle per stage. Re-imports upsert.
CREATE UNIQUE INDEX IF NOT EXISTS fragrance_note_claim_natural
    ON fragrance_note_claim
       (fragrance_id, claim_type, source_name, canonical_note, note_stage);

-- Scent family is a listing-level declared attribute (the retailer's own
-- browse taxonomy: Floral, Fresh, Woody...). NULL until a harvest
-- actually carries it -- never inferred, never filled from notes.
ALTER TABLE retailer_listings
    ADD COLUMN IF NOT EXISTS scent_family TEXT;
