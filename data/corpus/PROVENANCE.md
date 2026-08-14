# Corpus provenance

Where the corpus came from, so its biases stay inspectable instead of
becoming invisible properties of "what people say about fragrance".

Recorded from the ingest log of the run that built it. Counts are comments
stored per video; every row was new, so no video was returned by two
searches.

## Run 1 — 2026-08-09, YouTube Data API v3

24 videos, **3,155 comments**, 828 quota units of the 10,000 daily budget.
Collected with `--max-videos 3 --limit 300` per query.

| # | search query | video id | comments |
|---|---|---|---|
| 1 | `baccarat rouge 540 dupe` | `sN9pXNod5iE` | 16 |
| 2 | `baccarat rouge 540 dupe` | `Zh-Cx8nFKbQ` | 197 |
| 3 | `baccarat rouge 540 dupe` | `1YP1acQbsGQ` | 61 |
| 4 | `creed aventus dupe` | `xrZs6rAEq-w` | 28 |
| 5 | `creed aventus dupe` | `b4SEOsiRydw` | 141 |
| 6 | `creed aventus dupe` | `ZOd2QEVJX8c` | 208 |
| 7 | `dior sauvage dupe` | `tP4jhMqUYCU` | 54 |
| 8 | `dior sauvage dupe` | `QTdp7ZZ7OM8` | 10 |
| 9 | `dior sauvage dupe` | `5VT9WmQKb9w` | 89 |
| 10 | `tom ford oud wood dupe` | `q0Q_VDgJV9s` | 91 |
| 11 | `tom ford oud wood dupe` | `qytnonMTe-U` | 290 |
| 12 | `tom ford oud wood dupe` | `tDr7FON9jUg` | 236 |
| 13 | `parfums de marly delina dupe` | `7ETjZDC1aEU` | 63 |
| 14 | `parfums de marly delina dupe` | `kpDNoS1og6Q` | 29 |
| 15 | `parfums de marly delina dupe` | `JP7zSQ-bR10` | 114 |
| 16 | `parfums de marly layton dupe` | `MmmVbQGP1qk` | 222 |
| 17 | `parfums de marly layton dupe` | `gdVAjqsK28I` | 103 |
| 18 | `parfums de marly layton dupe` | `OPxVZKffBAw` | 327 |
| 19 | `lattafa khamrah review` | `fqHC5kLs0MY` | 151 |
| 20 | `lattafa khamrah review` | `00mVJV-HkTc` | 307 |
| 21 | `lattafa khamrah review` | `wqNw73QZxJk` | 143 |
| 22 | `lattafa bade'e al oud review` | `_Gs_z7_AlGw` | 136 |
| 23 | `lattafa bade'e al oud review` | `HS_YHabwTC4` | 88 |
| 24 | `lattafa bade'e al oud review` | `S7iJz3NA3Vs` | 51 |

Per query: BR540 274 · Aventus 377 · Sauvage 153 · Oud Wood 617 ·
Delina 206 · Layton 652 · Khamrah 601 · Bade'e Al Oud 275.

**This table is now also data.** `video_discoveries` holds the same
mapping (seeded by `scripts/seed_video_discoveries.py`), so query diversity
can be counted per edge rather than eyeballed here — see
`pages pairs --show-queries`. Reading it that way showed that four of the
six pairs that currently publish are backed by videos retrieved by a
*single* query, which the 2-video bar does not catch. SPEC records the
consequence.

## Known biases

Read any claim distribution from this corpus against these before drawing
a conclusion from it.

- **Six of eight queries contain the word "dupe".** `DUPE_OF` and
  `SIMILAR_TO` claims are over-represented *by construction*. This corpus
  cannot answer "how often do people compare fragrances at all" — it was
  built by asking for the comparisons. It can answer "when people compare,
  what do they compare to".
- **Videos were chosen by YouTube's relevance ranking, not by us.** Search
  favours high-view videos, which skews toward mainstream designer
  fragrances and toward channels whose titles promise a dupe.
- **Eight fragrances anchor the whole corpus**, and they are all
  high-traffic. Niche and vintage discussion is absent.
- **Comment counts are wildly uneven** — 10 to 327 per video. Three videos
  supply a fifth of the corpus, so their comment sections' quirks carry
  disproportionate weight.
- **Replies are included**, and reply threads amplify whoever the thread
  was arguing with. This is one reason ranking counts *distinct
  commenters* rather than rows.
- **`source_channel` holds the YouTube channel id** (`UC…`), not a
  readable name. Channel identity is available but not resolved.

## Reproducing

The video ids above are the record. Re-running the same searches will
return different videos as rankings shift — pass `--video` with these ids
to reconstruct this exact corpus, quota permitting.

Ingest is idempotent on `(source, source_id)`, so re-running is safe and
adds only what is genuinely new.

## Retrieval provenance is incomplete, and the gap cannot be closed

`video_discoveries` records which search surfaced which video. It covers
**24 of 39 videos** — the run-1 ingest of 2026-08-09, transcribed from the
table above.

The other 15 arrived on 2026-08-11 through `daily run`, before the loop
recorded discoveries. Their queries are **not recoverable**, for the reason
`record_discovery` states in its own docstring: a search re-run later
returns a different ranking, so a discovery not written down at the time is
gone. Two runs are known to have used

    fragrance dupe / fragrance clone / best fragrance 2026
    oud wood dupe / aventus clone / parfums de marly layton /
      khamrah review / cedrat boise / montblanc explorer

but the loop de-duplicated video ids across the queries of a run before
ingesting, so which query surfaced which video was never held anywhere.
Attributing them by guesswork would be worse than leaving them absent: the
count exists to gate publication, and inventing rows would inflate the
number the gate reads while looking like evidence.

Consequences, which the code models rather than hides:

- `Pair.unprovenanced` counts backing videos with no discovery row, and
  **7 of the 8 currently published pairs have at least one**. `queries` is
  therefore a lower bound nearly everywhere.
- `qualifying_pairs` skips the query check when `queries == 0`, so an
  unattributed pair is never failed for a record that was never written.
- Raising `MIN_QUERIES` above 1 should wait until new, fully-provenanced
  ingest is a large enough share of the corpus for the number to mean
  something. Ingest from 2026-08-11 onward records discoveries as it goes,
  so this closes on its own.

Video **titles** are a different case and were backfilled on 2026-08-11:
`videos.list` refetches 50 for one quota unit, so a title is recoverable
in a way a ranking is not. All 39 now carry title, channel and publish
date.

## The seeds were asking one question (2026-08-12)

Six of the eight searches behind this corpus contained "dupe" or "clone".
Measured on the discovery records: **10 of the 14 distinct queries, 71%.**

That is a leading question put to an audience assembled to answer it. It
finds people agreeing that A imitates B, and it does not find the person
who says B is worth the money, or the one who says A smells like C
instead — both of which the claim taxonomy already models and the corpus
barely contains. The published pairs show the same shape: every one of
them is a dupe claim or sits beside one.

`daily.SEED_QUERIES` now mixes eight shapes and the dupe shape is 2 of 10
rather than 6 of 8. The seeds moved out of the workflow file and into
code, where they carry their reasoning, are covered by a test, and appear
in a diff when they change.

**Nothing has been re-ingested.** The seeds are a plan; the corpus is what
happened. `daily seeds` prints both columns side by side for exactly that
reason — a report showing only the new list would read as though the
corpus had already broadened:

    shape            seeds now  corpus so far
    alternative to           1              0
    bare name                0              1
    better than              1              0
    compared to              1              0
    dupe/clone               2             10
    head to head             3              0
    review                   0              3
    smells like              1              0
    worth it                 1              0

The second column moves only when an ingest runs under the new seeds. Until
it does, `MIN_QUERIES` stays at 1 for the reason recorded above: a query
count of 2 measured against a corpus built almost entirely from one
question would unpublish pairs for our sampling rather than for their
evidence.
