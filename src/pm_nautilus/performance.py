"""Low-frequency, observed equity only. Never reconstruct missing market history."""

import asyncio
import time
from uuid import uuid4

from .config import micros, units
from .views import portfolio_view

HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * HOUR_MS
MAX_CURVE_POINTS = 1000


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
        self._curve_generation = None
        self._curve_last_id = 0
        self._curve_last_raw = None
        self._curve_series = {"H": [], "D": []}

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
                at_ms if at_ms is not None else time.time_ns() // 1_000_000,
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
        generation = r.store.generation
        if generation != self._curve_generation:
            self._curve_generation = generation
            self._curve_last_id = 0
            self._curve_last_raw = None
            self._curve_series = {"H": [], "D": []}

        # Read existing minute observations once, then only rows added since the
        # previous view. Both chart resolutions therefore retain older history
        # without transferring or drawing every minute sample on each refresh.
        rows = r.store.db.execute(
            "SELECT id,session,at_ms,pnl,total FROM equity_samples "
            "WHERE generation=? AND id>? ORDER BY id",
            (generation, self._curve_last_id),
        )
        for row_id, session, at_ms, pnl, total in rows:
            valid = pnl is not None and total is not None
            previous = self._curve_last_raw
            broken = previous is not None and (
                not previous[2]
                or not valid
                or session != previous[0]
                or at_ms <= previous[1]
                or at_ms - previous[1] > 90_000
            )
            for mode, interval in (("H", HOUR_MS), ("D", DAY_MS)):
                bucket = at_ms // interval
                series = self._curve_series[mode]
                point = {
                    "session": session,
                    "at": at_ms,
                    "pnl": units(pnl),
                    "total": units(total),
                    "bucket": bucket,
                    "breakBefore": broken,
                }
                if series and series[-1]["bucket"] == bucket:
                    point["breakBefore"] = series[-1]["breakBefore"] or broken
                    series[-1] = point
                else:
                    series.append(point)
                    if len(series) > MAX_CURVE_POINTS:
                        del series[0]
            self._curve_last_raw = (session, at_ms, valid)
            self._curve_last_id = row_id

        hourly = [point.copy() for point in self._curve_series["H"]]
        daily = [point.copy() for point in self._curve_series["D"]]
        return {
            "generation": generation,
            "sampleSeconds": 60,
            "error": self.error,
            "points": hourly,
            "series": {"H": hourly, "D": daily},
        }
