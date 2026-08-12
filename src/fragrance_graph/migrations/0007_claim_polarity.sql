-- Whether the commenter says the relationship holds, or says it does not.
--
-- Measured across the 499 similarity claims in the first real corpus: 36
-- of them (7.2%) were denials stored as assertions. "I just try latafa it
-- is nothing like angel share" was recorded as DUPE_OF latafa -> angel
-- share, so the ranked answer for Angel Share would have listed Lattafa
-- and quoted that comment as the evidence for it.
--
-- For a product whose entire pitch is "here is what people actually said",
-- this is worse than any accuracy number measured so far. A wrong claim
-- type is a mislabelled fact; a denial stored as an assertion is a
-- fabricated one that quotes a real person against themselves.
--
-- NOT folded into `sentiment`, which is the obvious-looking move and is
-- wrong the same way `LONGEVITY_COMPLAINT` was wrong in v1 — it collapses
-- two independent facts into one column. All 36 denials were NEGATIVE, but
-- so were five genuine edges ("worst dupe of (540)" asserts the dupe and
-- dislikes it). Sentiment is how they feel; polarity is whether they are
-- claiming it at all.
--
-- Denials are stored, never dropped. "Nine people say Khamrah is nothing
-- like Angels' Share" is a fact a buyer wants. They are simply excluded
-- from edges — see query.py.
--
-- Existing rows default to ASSERTED, which is wrong for the 36 known
-- denials. That is deliberate: the column is added first so the
-- re-extraction has somewhere to land, and `extract.polarity audit` lists
-- the known cases so the re-extraction can be checked by hand against
-- them. Backfilling by regex would bake this file's guesses into the
-- corpus as though a model had judged them.

ALTER TABLE claims ADD COLUMN polarity TEXT NOT NULL DEFAULT 'ASSERTED'
    CHECK (polarity IN ('ASSERTED', 'DENIED'));

-- Edges are read filtered to ASSERTED, so polarity belongs in the index
-- that serves them.
CREATE INDEX IF NOT EXISTS idx_claims_polarity
    ON claims (polarity, claim_type);
