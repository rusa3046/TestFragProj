# Codex review — fragrance-graph

You are reviewing a frozen commit of a system in a read-only sandbox. You
cannot modify anything and should not try.

## What this system claims to be

It reads YouTube comments about perfume and publishes comparison pages —
"X is a dupe of Y" — but **only** where several unrelated people
independently said so. Similarity is asserted by humans, never computed by
a model. From `SPEC.md`:

> Evidence spans and permalinks are load-bearing rather than diagnostic,
> ranking is by **distinct commenter count** rather than row count, and a
> pair with too few distinct commenters is not a weak result — it is not a
> result.

So the failure that matters most is not a crash. It is **a page that says
"9 people" when the true number of independent humans is smaller.** Rank
your findings by how close they come to that.

## How to report

For every finding:

- **Cite `file.py:function`** — a path and a symbol, not a vibe. If you
  cannot name where it lives, you have not found it yet.
- **Give the smallest reproduction**: the input, the state, and the wrong
  output. A concrete row beats a paragraph.
- **Mark it `HYPOTHESIS`** if you could not verify it in the source you
  read. Guessing is allowed; presenting a guess as a finding is not.
- Say plainly when a checklist item is **handled correctly** and name the
  code or test that handles it. A review that only lists problems tells the
  reader nothing about coverage.

Do not propose refactors, style changes, or new features. Do not comment on
naming, formatting, or type annotations. There is a linter for that.

---

## 1. Evidence independence and double-counting

The gate is `gate.py` (`MIN_COMMENTERS = 3`, `MIN_SOURCES = 2`). Everything
below is a way to satisfy it without three independent humans existing.

- `query.py:_commenter_key` decides what counts as one person. It falls
  back to the comment id when `author_id` is empty. Work out what happens
  when a **single anonymous person** posts the same claim on three videos:
  does the fallback split them into three commenters? Is that the safe
  direction of the error or the dangerous one? The docstring argues it is
  safe — check the argument, not the conclusion.
- `query.py:similar_to` groups by `(other fragrance, claim_type)`. The same
  person calling something both a dupe and similar appears in two rows.
  `pair_commenters` is meant to be the un-double-counted number. Trace
  every consumer — `pages.py:qualifying_pairs`, `pages.py:verdict`,
  `frontier.py:pairs` — and check each one leads with the distinct count
  rather than a sum of rows.
- `frontier.py:pairs` and `PAIRS_SQL` count people and creators from
  `comments.author_id` and `comments.source_channel`. `pages.py` counts
  from resolved fragrance ids. **These two disagree by construction.**
  Establish which one the gate actually uses and whether any code path
  reports one as if it were the other.
- One person replying to themselves in a thread: are parent and reply
  separate `comments` rows with the same `author_id`? Check
  `ingest/youtube.py:normalize_comment` for where the author is read from.

## 2. Creator-level versus commenter-level deduplication

`MIN_SOURCES` counts **channels**, not videos. The stated reason is that
one creator's audience agreeing with itself is one room.

- `pages.py:qualifying_pairs` and `query.py:similar_to` both compute
  `sources`. Confirm both key on `comments.source_channel` and not on
  `video_id`. Migration `0004_source_channel.sql` renamed this field; check
  nothing still reads the old meaning.
- `frontier.py:one_per_creator` limits enrichment to one video per channel.
  Does anything downstream assume that held, in a corpus where earlier
  ingests did not obey it?
- If a creator changes channel name or id, do their old and new comments
  count as two sources? Where would that be visible.

## 3. Polarity inversion

`migrations/0007_claim_polarity.sql` added `polarity`. "These smell nothing
alike" names two bottles and asserts the edge does **not** exist. Counting
it inverts a real person's meaning — the single worst output this system
can produce.

- Every query that reads claims must filter `polarity = 'ASSERTED'`. Audit
  all of them: `query.py`, `pages.py`, `frontier.py:PAIRS_SQL`,
  `attributes.py:RESOLVED_SQL` and `FLOATING_SQL`, `evals/score.py`.
  Name any that does not.
- `extract/polarity.py:denied_without_flag` and `:suspects` exist to find
  denials the extractor mislabelled. Are they wired to anything that runs,
  or only to a CLI a person must remember?
- `sentiment` and `polarity` are different columns meaning different
  things. Find any place that treats `sentiment = 'NEGATIVE'` as a denial,
  or vice versa. Check `query.py:aggregate_sentiment` and
  `pages.py:_contradiction` specifically.

## 4. Alias fragmentation versus flanker merging

Two opposite failures, and the fix for one causes the other.

- **Fragmentation**: "layton", "PDM Layton" and "Parfums de Marly Layton"
  are one bottle. If they resolve to three ids, evidence splits and nothing
  publishes. `resolve/names.py:best_match`, `:debranded`, `:similarity` and
  `resolve/entities.py:backfill` are the machinery. What is the fuzzy
  threshold, and what is the worst false merge it permits?
- **Merging**: Khamrah and Khamrah Qahwa are *different perfumes*. Merging
  them fabricates agreement. `pages.py:is_flanker_pair` holds those pairs
  to `MIN_FLANKER_COMMENTERS = 5` and `MIN_FLANKER_SOURCES = 3`. Work out
  how `is_flanker_pair` decides, and construct a real pair from
  `data/corpus/fragrances.jsonl` that is a flanker pair but is not
  detected as one.
- `attributes.py:HARMLESS_SUFFIX` and `:subject_of` refuse a video whose
  title continues past a known name ("Khamrah **Waha**"). Is that list
  exhaustive enough to be a safety property, or does a common word slip a
  flanker through?
- `resolve/entities.py:apply_batch` turns a name that already exists into
  an alias rather than a second node. Verify the "already exists" check
  (`_existing_fragrance`) covers canonical names, aliases *and* debranded
  forms — and that migration `0009_fragrance_name_unique.sql` catches what
  it misses. Note it is unique on `lower(canonical_name)`; find any lookup
  that still compares case-sensitively.

## 5. Catalogue metadata leaking in as community evidence

The product's whole claim is that every number came from a human comment.

- `fragrances.canonical_name`, `.brand`, `.aliases` and `.house_year` are
  curator-supplied. Confirm none of them can increment a commenter count, a
  source count, or a claim row. Follow `pages.py:bottle_facts` and
  `pages.py:brand_casing`.
- `commerce/feeds.py` and `commerce/links.py` hold retailer and product
  rows. The stated invariant is that ranking cannot see them, pinned by
  `test_ranking_is_identical_with_and_without_products`. Read that test:
  does it prove the claim, or does it only prove the tables are empty in
  the fixture?
- `resolve/entities.py` can draft names. A drafted row must not become
  evidence: check `UnconfirmedDraft` and `UnreviewedRows` actually block
  the write path rather than warn beside it.
- Does any page text assert something no comment said — a note list, a
  release year, a category? Read `pages.py:render_pair`,
  `pages.py:_absent` and `pages.py:verdict`.

## 6. Is the 3-commenter / 2-creator gate gameable

Assume an adversary who wants a specific pair published and can post
comments. What is the cheapest way in? Consider at least:

- three accounts on two channels, all controlled by one person — what in
  the system could possibly detect that, and if nothing can, is that
  acknowledged anywhere;
- one comment that names both bottles several times, if any code path
  counts mentions rather than commenters;
- a comment quoting *someone else's* claim ("everyone says X is a dupe of
  Y") — does extraction attribute that to the quoter as a fresh assertion?
  See `extract/llm.py:parse_response` and `write_claims`;
- bot or spam comments repeated verbatim across videos: is there any
  near-duplicate detection at all in `ingest/store.py:ingest`;
- `MIN_QUERIES = 1` in `gate.py` — what was it for, and is it effectively
  disabled?

## 7. Budget cap bypass across processes and working directories

`budget.py` enforces $1.00/day. It has been defeated twice, both recorded
in the source: once reaching $3.11 when the ledger path resolved relative
to the current directory, once reaching $1.11 when the code called `record`
(appends) instead of `guard` (appends *and raises*).

- `budget.py:_repo_root` walks up for `pyproject.toml`. What happens when
  the process runs from a git worktree, a clone, or a directory with no
  `pyproject.toml` above it?
- `FRAGRANCE_SPEND_LEDGER` overrides the path. Who can set it, and does any
  code path set it implicitly?
- The ledger is append-only text. Two processes extracting concurrently
  both read `spent_on` at start. Construct the interleaving that lets the
  pair spend 2× the cap and say whether anything prevents it.
- `budget.py:Budget.record` versus `:guard` — enumerate every caller of
  each. Any caller of `record` on a spending path is the 2026-08-15 defect
  returning. Check `frontier.py:budgeted_extractor` and
  `extract/llm.py:extract`'s `on_spend` parameter.
- `spent_on` skips malformed lines with a warning. Can a malformed line
  make the day look cheaper than it was, and is that the safe direction?

## 8. YouTube quota ceilings

`ingest/youtube.py:QuotaTracker`. `search.list` costs 100 units of 10,000
per day; comments cost ~1 unit per 100. Searching is the scarce resource.

- Is `QuotaTracker` per-process only? What happens across two runs on the
  same UTC day — is there any persisted counter equivalent to the spend
  ledger, and if not, is that gap acknowledged?
- Does the tracker decrement *before* or *after* the request? A 500 from
  YouTube that still charged quota is the interesting case.
- `frontier.py:run_probe` and `:enrich_one` are meant to share one search
  by persisting hits via `store_hits` / `recorded_hits`. Verify enrichment
  cannot issue a second `search.list` for a bottle already probed.
- `ingest/youtube.py:redact_key` exists so the API key never reaches a log
  or an error message. Find any path that formats a URL or response into an
  exception without passing through it.

## 9. Rebuildability from a clean clone

`data/corpus/*.jsonl` is the source of truth; the database is derived and
disposable. This broke in practice on 2026-08-15: 2,991 comments and 1,072
claims existed only in one container's database and were nearly lost.

- `corpus.py:export_corpus` and `:import_corpus` must round-trip. Identify
  any table, column or derived value written by the pipeline that export
  does **not** capture — `subject_frag_id`, `evidence_verified`,
  `extracted_at` and `videos.title` are the ones to check first.
- `corpus.py:shrinking` and `:WouldLoseRows` guard against an export that
  silently drops rows. Can an export still lose data while passing that
  check?
- Natural keys are `(source, source_id)` for comments and `canonical_name`
  for fragrances. Are those sufficient to reconstruct every foreign key, or
  does anything depend on autoincrement ids surviving?
- `migrations/` runs 0001→0010 on a fresh database. Does a clean clone plus
  `corpus import` plus `pages build` reproduce the committed site exactly,
  or does it require a step nobody wrote down?
