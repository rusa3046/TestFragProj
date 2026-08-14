# Operating this database

Exercises against a copy of this project's schema, big enough that the
answers are real. Every number below was measured on **3,000,000 comments
and 1,200,198 claims** — 1.7 GB — on a laptop-class container.

The point of doing it here rather than on a tutorial schema: these are
your tables, your queries, and your migrations. When you say "I added a
unique index to a five-million-row table without blocking writes", the
table has a name and the index is in `migrations/`.

## Setting up

```bash
createdb fragrance_graph_scale
uv run python -m scripts.scale load --comments 3000000
```

Roughly 70 seconds and 1.7 GB. Load again to grow it — numbering
continues, so `--comments 3000000` twice gives you six million.

**The rows are fabricated.** They resample real comment bodies onto new
authors, videos and timestamps, so lengths and skew stay realistic while
the row count does not. Three guards keep them away from the product:
the loader refuses any database not named `*_scale`, `corpus export`
refuses to read from one, and every synthetic `source_id` starts with
`synthetic-`. See the module docstring in `scripts/scale.py`.

---

## 1. Reading a plan

```sql
EXPLAIN (ANALYZE, BUFFERS) SELECT count(*) FROM comments WHERE score > 195;
```

```
Finalize Aggregate  (actual time=207.164..211.724 rows=1 loops=1)
  Buffers: shared hit=12260 read=122604
  ->  Gather  (Workers Launched: 2)
        ->  Parallel Seq Scan on comments
```

**134,864 buffers touched, 122,604 of them read from disk, 212 ms.** No
index on `score`, so every row is visited. Two parallel workers, which
Postgres chose because the table is big enough to be worth it.

Now one with an index — `idx_comments_author`, from migration 0006:

```sql
EXPLAIN (ANALYZE, BUFFERS) SELECT count(*) FROM comments WHERE author_id = 'author0000042';
```

```
Index Only Scan using idx_comments_author  (actual time=0.051..0.153 rows=16)
  Buffers: shared hit=2 read=17
```

**19 buffers, 0.15 ms.** Four orders of magnitude fewer pages for a query
over the same table.

And the third shape, which people find hardest to place — a **bitmap heap
scan**, used when an index matches too many rows for one-at-a-time lookups
but too few to justify reading everything:

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(DISTINCT author_id) FROM comments WHERE video_id = 'vid012345';
```

```
Bitmap Heap Scan on comments  (actual time=0.143..0.801 rows=104)
  Recheck Cond: (video_id = 'vid012345'::text)
  Heap Blocks: exact=104
```

The index is read first to build a bitmap of *which pages* hold matches,
then the heap is read once in physical order. `Heap Blocks: exact=104`
is the tell.

**What to take from `BUFFERS`:** `read` is pages that were not in cache.
It is the number that predicts how a query behaves on a cold server, and
it is invisible in wall-clock time on a warm one.

**Try:** `SET max_parallel_workers_per_gather = 0` and re-run the first
query. Same rows, one worker, and now you can see what parallelism was
buying.

---

## 2. Where each index type loses

The schema ships B-tree indexes only. Each of these is worth adding and
measuring on the scale database:

| Index | Try it on | Where it loses |
|---|---|---|
| **B-tree** | `comments (created_utc)` | Nothing here — it is the right default. But it is ~1/3 the table's size |
| **GIN** | `to_tsvector('english', body)` | Slow to build and to update; a write-heavy table pays for every insert |
| **BRIN** | `comments (created_utc)` | Tiny and nearly free — *if* rows are physically ordered by the column. Shuffle the table and it degrades to useless |
| **Partial** | `claims (comment_id) WHERE evidence_verified = 1` | Only usable when the query's WHERE matches the index's; a query without that predicate cannot use it |
| **Covering** | `claims (comment_id) INCLUDE (claim_type)` | Bigger index, and only pays when the query needs no other column |

BRIN is the instructive one on this data, because the loader writes rows in
insertion order with random timestamps — so a BRIN index on `created_utc`
will be *useless*, and seeing that is the lesson. Load with ordered
timestamps and it becomes 100x smaller than the B-tree for the same query.

---

## 3. MVCC, dead tuples, autovacuum

An UPDATE in Postgres does not modify a row; it writes a new version and
leaves the old one dead until vacuum reclaims it.

```sql
UPDATE comments SET score = score + 1 WHERE id % 15 = 0;   -- 200,000 rows
SELECT n_live_tup, n_dead_tup, last_autovacuum
  FROM pg_stat_user_tables WHERE relname = 'comments';
```

```
 n_live_tup | n_dead_tup | last_autovacuum
------------+------------+-----------------
    2999870 |     200000 |
```

Two hundred thousand dead tuples and autovacuum has not run — the default
threshold is 20% of the table plus 50 rows, and 200k of 3M is under 7%.

**The exercise:** repeat the UPDATE until autovacuum fires, watching
`last_autovacuum` and `pg_relation_size`. Then set
`ALTER TABLE comments SET (autovacuum_vacuum_scale_factor = 0.01)` and
watch it fire five times sooner. That parameter is the whole reason large
tables need per-table autovacuum settings: 20% of a 300-million-row table
is 60 million dead rows before anything happens.

**Where this project actually generates churn:** `resolve.entities
backfill` updates `subject_frag_id` and `object_frag_id` on every claim it
resolves. Run it against the scale database and watch the dead tuples.

---

## 4. Building an index without stopping the world

```
CREATE INDEX             idx_probe_score ON comments (score);   1,864 ms
CREATE INDEX CONCURRENTLY idx_probe_score ON comments (score);  6,282 ms
```

**3.4x slower, and the only one you can run on a live table.** The plain
form takes a lock that blocks every write for its whole duration. On this
table that is two seconds; on a 100-million-row table it is your outage.

Three things `CONCURRENTLY` costs you:

1. **It cannot run inside a transaction block.** This is why
   `db.migrate()` detects `CONCURRENTLY` and runs that file with
   autocommit, one statement at a time — see the module docstring in
   `db.py`. A migration runner that wraps every file in BEGIN/COMMIT
   cannot express the safe version of the commonest migration there is.
2. **It is not atomic.** A failed build leaves an `INVALID` index behind,
   which keeps costing you on writes and has to be dropped by hand:
   ```sql
   SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
   ```
3. **It waits for existing transactions.** One long-running transaction
   holds it up indefinitely — see the next section.

---

## 5. What actually blocks what

Open two `psql` sessions.

```sql
-- session 1
BEGIN;
UPDATE comments SET score = 0 WHERE id = 1;
-- and leave it open

-- session 2
ALTER TABLE comments ADD COLUMN probe TEXT;   -- blocks
```

Then, in a third session:

```sql
SELECT blocked.pid, blocked.query AS blocked_query,
       blocking.pid AS blocked_by, blocking.query AS blocking_query
  FROM pg_stat_activity blocked
  JOIN pg_stat_activity blocking
    ON blocking.pid = ANY(pg_blocking_pids(blocked.pid))
 WHERE blocked.wait_event_type = 'Lock';
```

The lesson worth internalising: `ALTER TABLE` needs ACCESS EXCLUSIVE, and
it queues **behind** the open transaction — but crucially, everything that
arrives *after* it queues behind the ALTER. One idle transaction plus one
schema change stops all reads on the table. That is the classic
Friday-afternoon outage, and it is why `lock_timeout` exists:

```sql
SET lock_timeout = '3s';   -- fail fast rather than pile up behind me
```

Find the long transactions:

```sql
SELECT pid, now() - xact_start AS age, state, left(query, 60)
  FROM pg_stat_activity
 WHERE xact_start IS NOT NULL AND now() - xact_start > interval '1 minute'
 ORDER BY age DESC;
```

---

## 6. Zero-downtime schema change — the P3 item

Three migrations this repo has already made, each of which is easy in
SQLite and needs care in Postgres. Do them against the scale database and
time them.

### a. Add a NOT NULL column with a backfill

Migration 0007 added `claims.polarity`. The naive version rewrites the
whole table under an exclusive lock. The safe version, in five steps:

```sql
-- 1. Add it nullable. Instant since Postgres 11 — no table rewrite.
ALTER TABLE claims ADD COLUMN polarity_v2 TEXT;

-- 2. Backfill in batches, committing between them, so no single
--    transaction holds locks or bloats for long.
UPDATE claims SET polarity_v2 = 'ASSERTED'
 WHERE polarity_v2 IS NULL AND id IN (
   SELECT id FROM claims WHERE polarity_v2 IS NULL LIMIT 50000
 );   -- repeat until 0 rows

-- 3. Add the constraint as NOT VALID: instant, applies to new rows only.
ALTER TABLE claims ADD CONSTRAINT polarity_v2_not_null
  CHECK (polarity_v2 IS NOT NULL) NOT VALID;

-- 4. Validate it: takes only a SHARE UPDATE EXCLUSIVE lock, so reads and
--    writes continue while it scans.
ALTER TABLE claims VALIDATE CONSTRAINT polarity_v2_not_null;

-- 5. Now SET NOT NULL is cheap, because the constraint already proves it.
ALTER TABLE claims ALTER COLUMN polarity_v2 SET NOT NULL;
```

Time step 2 as one statement versus batched, and watch `n_dead_tup`.

### b. The case-insensitive unique index

Migration 0009 in this repo is:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_fragrances_canonical_name
    ON fragrances (lower(canonical_name));
```

On a live table that has to be `CREATE UNIQUE INDEX CONCURRENTLY`, and a
unique build **fails** if duplicates exist — so the real procedure is:
find duplicates first, resolve them, then build. Try it: insert a
colliding row into the scale database and watch the build fail after
doing all its work.

### c. Partitioning `comments` by time

The one with real migration cost. `comments` is append-mostly with a
natural time key, which is the textbook case.

```sql
CREATE TABLE comments_new (LIKE comments INCLUDING ALL)
  PARTITION BY RANGE (created_utc);
-- create partitions, copy in batches, swap names in one transaction
```

Measure: how long the copy takes, how much disk you need for both copies
at once, and what a query plan looks like before and after (look for
`Partitions removed by pruning`).

---

## 7. Connection pooling

Nothing in this project needs pgbouncer — one process runs twice a week.
Which is exactly why it has to be demonstrated deliberately rather than
observed.

```bash
docker compose -f docker/compose.pooling.yaml up -d
```

Then:

```bash
# 200 connections straight at Postgres
uv run python -m scripts.scale load --comments 1000   # while running:
psql -c "SELECT count(*), state FROM pg_stat_activity GROUP BY state;"
```

**Transaction vs session pooling, in one sentence each.** *Session*
pooling hands a client a server connection until it disconnects — safe for
everything, saves you only the cost of connecting. *Transaction* pooling
hands it over for the length of one transaction — far higher reuse, and it
breaks anything that assumes state survives between statements: prepared
statements, `SET`, advisory locks, `LISTEN`. psycopg3 uses prepared
statements automatically after five executions of the same query, which is
exactly the trap; `prepare_threshold=None` on the connection turns it off.

---

## 8. Replication and lag

```bash
docker compose -f docker/compose.replica.yaml up -d
```

On the primary:

```sql
SELECT client_addr, state, sent_lsn, write_lsn, flush_lsn, replay_lsn,
       pg_wal_lsn_diff(sent_lsn, replay_lsn) AS replay_bytes_behind
  FROM pg_stat_replication;
```

Generate lag by loading a million rows and watching `replay_bytes_behind`
climb, then drain.

**Streaming vs logical, and when each is the answer.** Streaming ships WAL
bytes: the replica is a physical copy, all-or-nothing, same major version,
and cannot be written to. Logical ships decoded row changes: selective by
table, works across versions, and the target stays writable — which is what
makes it the tool for a zero-downtime major-version upgrade, and useless
for a hot standby you intend to fail over to blindly.

**Failover, the part that bites:** promoting a replica that is behind
loses whatever it had not replayed. `synchronous_commit = on` with a
`synchronous_standby_names` set makes the primary wait for the replica —
no data loss, and now the primary's write latency includes a network
round trip, and the primary *stops accepting writes* if the replica dies.
There is no free version of this trade; there is only choosing which side
of it you want.

---

## What to do first

If you have four hours, do section 6a end to end and time every step. It
is the one on their list marked "do not skip", it is the one that
generates a story with a number in it, and it is the only one here whose
mistakes take a production system down rather than merely slow it.
