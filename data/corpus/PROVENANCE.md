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
