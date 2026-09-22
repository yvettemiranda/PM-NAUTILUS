"""Low-frequency, observed equity only. Never reconstruct missing market history."""

import asyncio
import time
from uuid import uuid4

from .config import micros, units
from .views import portfolio_view


class PerformanceSampler:
    def __init__(self, runtime):
        self.runtime = runtime
        self.session = str(uuid4())
        self.error = None
        self.task = None
        runtime.store.db.execute("""CREATE TABLE IF NOT EXISTS equity_samples(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            generation TEXT NOT NULL, session TEXT NOT NULL,
            at_ms INTEGER NOT NULL, pnl INTEGER, total INTEGER)""")
        runtime.store.db.execute(
            "CREATE INDEX IF NOT EXISTS equity_generation ON equity_samples(generation,id)"
        )

    def sample(self, at_ms=None):
        r = self.runtime
        _, portfolio = portfolio_view(r)
        total = portfolio["totalFunds"]
        unrealized = portfolio["unrealizedPnl"]
        # Unknown books/recovery must leave a gap, not zero or a stale valuation.
        valid = total is not None and unrealized is not None and not r.faulted
        if r.mode == "LIVE" and not r.client.ready:
            valid = False
        pnl = micros(portfolio["realizedPnl"]) + micros(unrealized) if valid else None
        r.store.db.execute(
            "INSERT INTO equity_samples(generation,session,at_ms,pnl,total) VALUES(?,?,?,?,?)",
            (
                r.store.generation,
                self.session,
                at_ms or time.time_ns() // 1_000_000,
                pnl,
                micros(total) if valid else None,
            ),
        )
        self.error = None

    async def run(self):
        while True:
            try:
                self.sample()
            except Exception as exc:
                # Analytics cannot turn a successful trading operation into a failure.
                # Expose failure and start a new curve segment when sampling resumes.
                self.error = type(exc).__name__
                self.session = str(uuid4())
            await asyncio.sleep(60)

    def view(self):
        r = self.runtime
        rows = r.store.db.execute(
            "SELECT session,at_ms,pnl,total FROM equity_samples "
            "WHERE generation=? ORDER BY id DESC LIMIT 1440",
            (r.store.generation,),
        ).fetchall()
        return {
            "generation": r.store.generation,
            "sampleSeconds": 60,
            "error": self.error,
            "points": [
                {"session": row[0], "at": row[1], "pnl": units(row[2]), "total": units(row[3])}
                for row in reversed(rows)
            ],
        }
