"""A hard daily spending cap for anything that runs unattended.

    from fragrance_graph.budget import Budget
    budget = Budget.load()
    budget.check()                      # raises BudgetExhausted if today is spent
    budget.record(0.0412, "extract")    # written immediately, not at exit

    with budget.reserve(0.05, "embed") as claim:   # money committed first
        cost = call_the_paid_api()
        claim.settle(cost)                         # corrected to the real figure

Attended runs answer to a person watching the number. An unattended loop
answers to nothing, and the failure mode is not one expensive day — it is a
cheap bug repeating on a schedule. So the cap is a hard stop rather than a
warning, and it is enforced *between batches* rather than only before the
run, because "estimate first, then spend" cannot see a batch that costs more
than projected.

## Two paths, because the cost is not always known in advance

The distinction runs through everything here:

    reserve -> pay -> settle    cost estimable first; cannot exceed the cap
    pay -> record               cost known only after; exceeds by one unit

`record` and `guard` are the second kind and extraction is inherently that
shape — a batch's price is whatever the model charged, and no amount of
locking un-spends a call that already returned. The guarantee there is that
the run *stops* on the next batch, having overshot once.

`reserve` is the first kind. It writes the estimate to the ledger before the
call, so the money is visible to every other process while the call is still
in flight, and the estimate is corrected by an appended settlement line
afterwards. Nothing is edited: a wrong figure that was written stays
written, because the ledger's value is that it shows what actually happened.

## Concurrency

Every read-modify-write here runs under an exclusive `flock` on a sidecar
lock file, and the running total is re-read from the file inside that lock
rather than trusted from `spent_usd`. Before this, two processes starting
together each read $0.00 and each believed the whole remainder was theirs —
`$1.80 against a $1.50 cap`, pinned for a while as a documented limit rather
than fixed.

The proof is `tests/test_budget.py::TestConcurrentProcesses::
test_the_lock_makes_a_second_process_wait`, and it is written the way it is
for a reason worth repeating: the obvious version — start two processes,
assert one loses — **passes with the locking removed**, because process
startup takes milliseconds and the unprotected window is microseconds. It
was written, checked against a build with the lock disabled, and found to
prove nothing. A concurrency test that has not been run against the broken
version is decoration.

## Why the ledger is a committed file

Each scheduled run gets a fresh container: the repository is cloned, the
database is rebuilt from `data/corpus/`, and the filesystem is reclaimed
afterwards. A cap tracked in the database or in `/tmp` therefore resets
every run, which turns "$1 per day" into "$1 per run" — the one reading that
makes the cap useless precisely when something is looping.

`data/spend.jsonl` is committed for the same reason the corpus is: it
records money that was actually spent, it cannot be regenerated, and it has
to survive the container. It is append-only and one JSON object per line, so
two runs appending concurrently cannot corrupt each other's records and the
file diffs line by line in review.

Dates are UTC. A local-time day boundary would move with the scheduler's
timezone, and a cap whose window depends on where the runner happens to be
is not a cap.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

try:  # POSIX only, which is every platform this runs on.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger("fragrance_graph.budget")

#: The cap, in USD.
#:
#: Sized against the *worst* measured extraction rate rather than the
#: average, because a cap that assumes the good case is not a cap. Across
#: three runs on 2026-08-11 the rate ranged $0.3730-$0.5020 per 1,000
#: comments, so a dollar a day buys roughly **2,000 comments** at the top
#: of that range — still comfortably more than a day of new YouTube
#: discussion produces, so the cap binds on a runaway rather than on
#: ordinary work.
#:
#: The figure this comment used to quote, $0.3656/1k for 2,700 comments,
#: came from the 2026-08-09 corpus and drifted 37% within two days. Cost
#: tracks *claims* rather than comments — output is ~69% of the bill — so
#: a corpus where people assert more per comment costs more to read. Treat
#: any rate here as corpus-specific and recheck it after a source changes.
#:
#: Catalogue lookups bill through this ledger too, at a flat
#: $0.05 per request, and at that price they dominated:
#: 20 lookups cost as much as 2,000 comments of extraction.
#:
#: **Raised from $1.00 to $1.50 on 2026-08-15**, authorized for the
#: attribute-enrichment experiment. Three things this change is not:
#:
#: - It is not a reset. The ledger is untouched and the $1.1104 already
#:   spent on 2026-08-15 counts against the new figure, leaving $0.3896
#:   for that day. Raising a ceiling and clearing the meter are different
#:   acts, and only the first was authorized.
#: - It is not an exemption. Every paid path still charges the same
#:   ledger through `Budget.guard`, and the experiment is refused at
#:   $1.50 exactly as it was at $1.00.
#: - It is not a precedent for raising it again. The number is a
#:   configured ceiling, and moving it is a decision a person makes.
#:
#: A cap raised by editing this constant stays testable, because every
#: enforcement path reads it. A cap raised by special-casing a caller
#: would not, which is the whole reason the change is here rather than in
#: the experiment.
DAILY_CAP_USD = 1.50

def _repo_root() -> Path:
    """The directory holding pyproject.toml, found from this file.

    The ledger used to be the relative string "data/spend.jsonl", resolved
    against the working directory. That made the cap a property of where a
    process happened to be started: a run from another directory read a
    file that was not there, got 0.0, and received a clean allowance. On
    2026-08-11 the day's ledger reached $3.11 against a $1.00 cap.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


DEFAULT_LEDGER = Path(
    os.environ.get("FRAGRANCE_SPEND_LEDGER") or _repo_root() / "data" / "spend.jsonl"
)


class BudgetExhausted(RuntimeError):
    """Raised when a spend would cross the daily cap.

    Deliberately an exception rather than a return value: every caller that
    could ignore a boolean is a caller that could spend money by forgetting
    to check one.
    """


class NoFileLocking(RuntimeError):
    """Raised when `fcntl` is unavailable, so the cap cannot be atomic.

    Refusing is the point. Falling back to an unlocked read-modify-write
    would restore the exact race this module was changed to remove, and it
    would do it silently on the one platform nobody here tests.
    """


@contextmanager
def _locked(ledger: Path) -> Iterator[None]:
    """Hold an exclusive lock for one read-check-append.

    A sidecar `.lock` file rather than the ledger itself, because the
    ledger is opened and closed per append and a lock tied to that handle
    would be released halfway through the sequence it is protecting.

    `flock` is advisory, which is sufficient here: every writer goes
    through this module, and nothing else in the system opens the ledger
    for writing. It is also released automatically when the process dies,
    so a crash mid-reservation frees the lock rather than wedging every
    later run.
    """
    if fcntl is None:  # pragma: no cover - Windows
        raise NoFileLocking(
            "fcntl is unavailable, so the daily cap cannot be enforced "
            "atomically across processes. Refusing rather than racing."
        )
    ledger.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger.with_suffix(ledger.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass
class Reservation:
    """Money claimed before a paid call, settled to its real cost after.

    The estimate is written to the ledger *first*, so a second process
    reading the ledger sees the money as already gone while the call is
    still in flight. That is what makes the cap hold across processes:
    the window between "decided to spend" and "know what it cost" is
    exactly where the old race lived.

    A process that dies mid-call leaves its reservation standing. The day
    then looks more expensive than it was, which is the safe direction and
    the same choice `record` makes by writing before it counts.
    """

    budget: Budget
    what: str
    estimate_usd: float
    ref: str
    settled: bool = False

    def settle(self, actual_usd: float) -> None:
        """Correct the reservation to what the call really cost.

        Appends the difference rather than editing the reservation line —
        the ledger is append-only, and a row that was written is evidence
        the call happened even when the figure it carried was wrong.
        The adjustment may be negative; that is the unused remainder being
        released, and it is the one place a negative line is legitimate.
        """
        if self.settled:
            raise RuntimeError(f"reservation {self.ref} was already settled")
        if actual_usd < 0:
            raise ValueError("actual spend cannot be negative")
        self.settled = True
        self.budget._append(
            actual_usd - self.estimate_usd,
            self.what,
            kind="settlement",
            ref=self.ref,
            actual_usd=round(actual_usd, 12),
        )

    def __enter__(self) -> Reservation:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # An unsettled reservation on the way out of the block stays on the
        # ledger at its estimate. Releasing it here would hand the money
        # back after a failure whose cost is unknown.
        return None


@dataclass
class Budget:
    """Today's remaining allowance, backed by an append-only ledger."""

    cap_usd: float = DAILY_CAP_USD
    ledger: Path = field(default_factory=lambda: DEFAULT_LEDGER)
    today: str = field(default_factory=lambda: _utc_today().isoformat())
    #: Spend already recorded for `today` before this process started.
    spent_usd: float = 0.0
    #: Whether the ledger file was found. A missing ledger is **unknown
    #: spend, not zero spend** — the same reading the publishing gate gives
    #: `queries == 0`. Absence of a record is not evidence that nothing was
    #: spent, and treating it as zero is what turned a per-day cap into a
    #: per-container one.
    ledger_present: bool = True
    #: When true, a missing ledger blocks spending instead of permitting
    #: it. Off by default so library use and tests are unaffected; every
    #: entry point that spends real money turns it on.
    require_ledger: bool = False

    @classmethod
    def load(
        cls,
        ledger: Path | str | None = None,
        *,
        cap_usd: float = DAILY_CAP_USD,
        today: str | None = None,
        require_ledger: bool = False,
    ) -> Budget:
        path = Path(ledger) if ledger is not None else DEFAULT_LEDGER
        day = today or _utc_today().isoformat()
        return cls(
            cap_usd=cap_usd,
            ledger=path,
            today=day,
            spent_usd=spent_on(path, day),
            ledger_present=path.exists(),
            require_ledger=require_ledger,
        )

    def _refuse_if_ledger_missing(self) -> None:
        if self.require_ledger and not self.ledger_present:
            raise BudgetExhausted(
                f"no spend ledger at {self.ledger}, so today's spend is "
                "unknown rather than zero — refusing to spend.\n\n"
                "  uv run python -m fragrance_graph.daily spend init\n\n"
                "Creating it is a deliberate act precisely because a "
                "missing ledger is what lets a fresh container hand itself "
                "a fresh allowance."
            )

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.spent_usd)

    @property
    def exhausted(self) -> bool:
        return self.remaining_usd <= 0

    def check(self, projected_usd: float = 0.0) -> None:
        """Raise unless `projected_usd` more can still be spent today."""
        self._refuse_if_ledger_missing()
        if self.spent_usd + projected_usd > self.cap_usd:
            raise BudgetExhausted(
                f"daily cap ${self.cap_usd:.2f} would be exceeded: "
                f"${self.spent_usd:.4f} already spent on {self.today}"
                + (f", ${projected_usd:.4f} projected" if projected_usd else "")
            )

    def refresh(self) -> float:
        """Re-read today's total from the file. Returns the new figure.

        `spent_usd` is a cache of what this process knew at load time. Any
        decision made from the cache alone is a decision made without the
        other process in the room.
        """
        self.spent_usd = spent_on(self.ledger, self.today)
        self.ledger_present = self.ledger.exists()
        return self.spent_usd

    def reserve(self, estimate_usd: float, what: str, **detail: object) -> Reservation:
        """Claim `estimate_usd` before making a paid call.

        Read, check and append happen under one lock, so two processes
        arriving together are serialised: the second re-reads a ledger that
        already contains the first's claim and is refused. Without this the
        pair each read $0.00 and each believed the whole remainder was
        theirs — `$1.80 against a $1.50 cap`, which
        `tests/test_budget.py` used to pin as a documented limit.

        Raises `BudgetExhausted` before writing anything if the estimate
        does not fit. Settle the returned reservation once the real cost is
        known.
        """
        if estimate_usd < 0:
            raise ValueError("an estimate cannot be negative")
        with _locked(self.ledger):
            self.refresh()
            self._refuse_if_ledger_missing()
            if self.spent_usd + estimate_usd > self.cap_usd:
                raise BudgetExhausted(
                    f"daily cap ${self.cap_usd:.2f} would be exceeded by a "
                    f"reservation of ${estimate_usd:.6f}: ${self.spent_usd:.6f} "
                    f"already committed on {self.today}. Nothing was reserved."
                )
            ref = uuid.uuid4().hex[:12]
            self._write(
                estimate_usd, what, kind="reservation", ref=ref, **detail
            )
        return Reservation(self, what, estimate_usd, ref)

    def record(self, usd: float, what: str, **detail: object) -> None:
        """Append a spend, then account for it. Written before it is counted
        so a crash between the two over-reports rather than under-reports.

        The whole read-modify-write is under the ledger lock, and the
        running total is re-read from the file rather than trusted from
        this process's cache. That is what makes `guard` — which learns a
        batch's cost only after paying it — see a concurrent run's spend
        and stop. Before this it did not, and the cap was per-process
        whenever two runs overlapped.
        """
        if usd < 0:
            raise ValueError("spend cannot be negative")
        self._append(usd, what, **detail)

    def _append(self, usd: float, what: str, **detail: object) -> None:
        """Lock, re-read, append, and re-cache. Permits a negative `usd`,
        which only `Reservation.settle` passes."""
        with _locked(self.ledger):
            before = spent_on(self.ledger, self.today)
            self._write(usd, what, **detail)
            self.spent_usd = before + usd
            self.ledger_present = True

    def _write(self, usd: float, what: str, **detail: object) -> None:
        """The append itself. Assumes the caller holds the lock."""
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "date": self.today,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            # 12 places, not 6. `spent_usd` accumulates the exact figure in
            # this process, but the *file* is what a fresh container reads,
            # and rounding each line to six places wrote real spend away.
            #
            # Measured: the OpenAI embedding arm makes one call per
            # descriptor, 1,092 of them, each costing about $0.00000005.
            # Every line rounded to 0.0 and the ledger recorded $0.000001
            # against roughly $0.000058 actually charged — 98% of it gone.
            #
            # It is a rounding error worth six thousandths of a cent and it
            # is the fifth escape of this class, which is the reason it is
            # being fixed rather than noted. A cap that cannot see many
            # small calls is a cap a loop of small calls runs straight
            # through: the per-call cost is what makes it invisible, not
            # the total.
            "usd": round(usd, 12),
            "what": what,
            **detail,
        }
        with self.ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
            # Flushed and synced inside the lock. A reservation still
            # sitting in a buffer is a reservation the next process cannot
            # see, which would defeat the lock it was written under.
            fh.flush()
            os.fsync(fh.fileno())

    def guard(self, what: str):
        """A per-batch callback for `extract.llm.extract`.

        Records what the batch cost, then stops the run the moment the cap
        is crossed. Extraction commits per batch and leaves `extracted_at`
        NULL on anything unprocessed, so stopping here loses no work and
        the next run resumes exactly where this one halted.
        """

        def on_spend(usd: float, comments: int) -> None:
            self._refuse_if_ledger_missing()
            self.record(usd, what, comments=comments)
            if self.exhausted:
                raise BudgetExhausted(
                    f"daily cap ${self.cap_usd:.2f} reached "
                    f"(${self.spent_usd:.4f} spent on {self.today}); "
                    "stopping. Un-extracted comments are untouched and "
                    "resume on the next run."
                )

        return on_spend


def _utc_today() -> date:
    return datetime.now(UTC).date()


def spent_on(ledger: Path | str, day: str) -> float:
    """Total recorded spend for one UTC date.

    A malformed line is skipped loudly rather than crashing the run: the
    ledger's job is to stop overspending, and an unparseable line from some
    older format must not become a reason to spend nothing forever. It is
    counted as unknown, not as zero — see the warning.
    """
    path = Path(ledger)
    if not path.exists():
        return 0.0
    total = 0.0
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            if record.get("date") == day:
                total += float(record["usd"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            log.warning("%s:%d is not a readable spend record; ignoring", path, lineno)
    return total


def summary(ledger: Path | str = DEFAULT_LEDGER, *, days: int = 7) -> str:
    """Recent daily totals, newest first."""
    path = Path(ledger)
    if not path.exists():
        return f"No ledger at {path}; nothing has been spent."
    totals: dict[str, float] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            totals[record["date"]] = totals.get(record["date"], 0.0) + float(
                record["usd"]
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    rows = sorted(totals.items(), reverse=True)[:days]
    width = max((len(d) for d, _ in rows), default=10)
    return "\n".join(
        f"  {day:<{width}}  ${amount:.4f}"
        + ("  <- at cap" if amount >= DAILY_CAP_USD else "")
        for day, amount in rows
    )
