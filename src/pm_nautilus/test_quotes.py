"""One immutable integer decode per public quote; no shared simulated state."""

from .config import micros


class PreparedTestQuote:
    """A synchronous message-local cache, never a stored or timed market quote."""

    __slots__ = ("raw", "touched", "_levels")

    @staticmethod
    def supports(raw):
        if type(raw) is not dict or type(raw.get("event_type")) is not str:
            return False
        plain = (str, int, float, bool, type(None))
        if any(type(raw.get(key)) not in plain for key in ("timestamp", "market")):
            return False

        def level_ok(level):
            return type(level) is dict and all(
                type(level.get(key)) in plain for key in ("price", "size")
            )

        if raw.get("event_type") == "book":
            return type(raw.get("asset_id")) in plain and all(
                type(raw.get(side)) is list and all(level_ok(level) for level in raw[side])
                for side in ("bids", "asks")
            )
        if raw.get("event_type") == "price_change":
            return type(raw.get("price_changes", [])) is list and all(
                level_ok(change)
                and type(change.get("asset_id")) in plain
                and type(change.get("side")) in plain
                for change in raw.get("price_changes", [])
            )
        return False

    def __init__(self, raw):
        self.raw = raw
        self.touched = (
            {raw.get("asset_id")}
            if raw["event_type"] == "book"
            else {change.get("asset_id") for change in raw.get("price_changes", [])}
        )
        self._levels = {}

    def book_levels(self, side):
        if side not in self._levels:
            self._levels[side] = tuple(
                (micros(level["price"]), micros(level["size"])) for level in self.raw[side]
            )
        return list(self._levels[side])

    def change_level(self, index):
        if index not in self._levels:
            change = self.raw["price_changes"][index]
            self._levels[index] = micros(change["price"]), micros(change["size"])
        return [self._levels[index]]
