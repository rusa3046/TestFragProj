"""A hard daily spending cap for anything that runs unattended.

    from fragrance_graph.budget import Budget
    budget = Budget.load()
    budget.check()                      # raises BudgetExhausted if today is spent
    budget.record(0.0412, "extract")    # written immediately, not at exit

Attended runs answer to a person watching the number. An unattended loop
answers to nothing, and the failure mode is not one expensive day — it is a
cheap bug repeating on a schedule. So the cap is a hard stop rather than a
warning, and it is enforced *between batches* rather than only before the
run, because "estimate first, then spend" cannot see a batch that costs more
than projected.

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
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

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
#: `enrich.LOOKUP_COST_USD` per request, and at $0.05 each they dominate:
#: 20 lookups cost as much as 2,000 comments of extraction.
DAILY_CAP_USD = 1.00

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

    def record(self, usd: float, what: str, **detail: object) -> None:
        """Append a spend, then account for it. Written before it is counted
        so a crash between the two over-reports rather than under-reports."""
        if usd < 0:
            raise ValueError("spend cannot be negative")
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "date": self.today,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "usd": round(usd, 6),
            "what": what,
            **detail,
        }
        with self.ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
        self.spent_usd += usd

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
