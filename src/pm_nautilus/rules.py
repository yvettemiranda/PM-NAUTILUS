"""Deterministic PM rules. No orders, persistence, clocks or network side effects."""

from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

from .config import SCALE, Preferences

Level = tuple[int, int]


def ceil_div(n: int, d: int) -> int:
    return (n + d - 1) // d


def cost(price: int, qty: int) -> int:
    return price * qty // SCALE


def affordable(budget: int, price: int) -> int:
    return (budget * 100 // price) * 10_000 if price > 0 and budget > 0 else 0


def target_price(price: int, tick: int, config: Preferences) -> int:
    if tick <= 0:
        raise ValueError("tick must be positive")
    raw = max(price + config.increase, ceil_div(price * config.multiplier, SCALE))
    return min(990_000, ceil_div(raw, tick) * tick)


@dataclass(frozen=True)
class Fees:
    enabled: bool
    rate: int = 0
    exponent: int = 1

    def __post_init__(self):
        if type(self.enabled) is not bool or not 0 <= self.rate <= SCALE:
            raise ValueError("费用状态/费率无效")
        if type(self.exponent) is not int or not 0 <= self.exponent <= 10:
            raise ValueError("费用指数无效")
        if not self.enabled and self.rate:
            raise ValueError("禁用费用时费率应为0")

    def fee(self, qty: int, price: int) -> int:
        if not self.enabled or not qty or not 0 < price < SCALE:
            return 0
        centered = price * (SCALE - price) // SCALE
        curve = SCALE
        for _ in range(self.exponent):
            curve = curve * centered // SCALE
        return ceil_div(qty * self.rate * curve, SCALE * SCALE)


@dataclass(frozen=True)
class Token:
    token_id: str
    event_id: str
    market_id: str
    condition_id: str
    direction: str
    result_count: int
    neg_risk: bool
    opened_ns: int
    ends_ns: int
    fees: Fees
    min_size: int
    tick: int
    event_title: str = ""
    question: str = ""
    slug: str = ""
    category_ids: tuple[str, ...] = ()
    category_labels: tuple[str, ...] = ()
    game_start_ns: int | None = None
    open: bool = True

    def progress(self, now: int) -> Fraction:
        return Fraction((now - self.opened_ns) * 100, self.ends_ns - self.opened_ns)


def static_reason(t: Token, c: Preferences, now: int, official_tags: set[str]) -> str | None:
    if not t.open or not t.condition_id:
        return "CLOSED"
    kind = "BINARY" if t.result_count == 2 else "TERNARY" if t.result_count == 3 else "MULTI"
    if t.result_count < 2 or kind not in c.marketTypes:
        return "RESULT_COUNT"
    if not c.allCategories and not set(t.category_ids) & set(c.selectedCategoryIds) & official_tags:
        return "CATEGORY"
    if t.game_start_ns is not None and now >= t.game_start_ns:
        return "GAME_START"
    duration = t.ends_ns - t.opened_ns
    day = 86_400_000_000_000
    if duration <= 0:
        return "DURATION_MISSING"
    if duration < c.minMarketDurationDays * day:
        return "DURATION_BELOW_MIN"
    if duration > c.maxMarketDurationDays * day:
        return "DURATION_ABOVE_MAX"
    if now < t.opened_ns:
        return "PROGRESS_BELOW_ZERO"
    if now >= t.ends_ns:
        return "CLOSED"
    if t.progress(now) > c.maxMarketProgressPercent:
        return "PROGRESS_ABOVE_MAX"
    return None


def quote_reason(t: Token, c: Preferences, bids: list[Level], asks: list[Level], budget: int):
    if not asks:
        return "ASK_MISSING"
    if not bids:
        return "BID_MISSING"
    bid, ask = max(p for p, q in bids if q), min(p for p, q in asks if q)
    if ask < c.min_price:
        return "ASK_BELOW_MIN"
    if ask > c.max_price:
        return "ASK_ABOVE_MAX"
    if bid * 100 < ask * c.minBidAskRatioPercent:
        return "BID_ASK_RATIO"
    if t.tick <= 0 or any(p % t.tick for p, q in bids + asks if q):
        return "TICK_SIZE"
    if t.min_size <= 0:
        return "MIN_ORDER_SIZE"
    if affordable(budget, ask) < t.min_size:
        return "ORDER_BUDGET"
    return None


@dataclass(frozen=True)
class Fill:
    price: int
    gross: int
    net: int
    amount: int
    fee: int
    target: int = 0


def plan_buy(
    asks: list[Level],
    budget: int,
    limit: int,
    minimum: int,
    fees: Fees,
    tick: int,
    config: Preferences,
) -> list[Fill]:
    levels = sorted((p, q) for p, q in asks if 0 < p <= limit and q > 0)
    if not levels or affordable(budget, levels[0][0]) < minimum:
        return []
    fills = []
    for p, depth in levels:
        q = min(depth, affordable(budget, p))
        amount = cost(p, q)
        fee = fees.fee(q, p)
        net = q - ceil_div(fee * SCALE, p)
        if amount <= 0 or net <= 0:
            continue
        fills.append(Fill(p, q, net, amount, fee, target_price(p, tick, config)))
        budget -= amount
        if budget == 0:
            break
    return fills


@dataclass(frozen=True)
class Target:
    id: str
    price: int
    qty: int
    created: int


@dataclass(frozen=True)
class SellBatch:
    targets: tuple[Target, ...]
    limit: int
    quantity: int
    fills: tuple[Fill, ...]


def sell_batches(bids: list[Level], targets: list[Target], minimum: int, fees: Fees):
    """Stable grouping and shared depth, used by previews and submitted sell batches."""
    depth = dict(bids)
    batches = []
    grouped: list[Target] = []
    quantity = 0
    for t in sorted(targets, key=lambda x: (x.price, x.created, x.id)):
        if t.qty <= 0:
            continue
        grouped.append(t)
        quantity += t.qty
        if quantity < minimum:
            continue
        limit = max(x.price for x in grouped)
        remain = quantity
        fills = []
        for p in sorted(depth, reverse=True):
            if p < limit or not 0 < p < SCALE:
                continue
            q = min(remain, depth[p])
            if q <= 0:
                continue
            fee = fees.fee(q, p)
            fills.append(Fill(p, q, q, max(0, cost(p, q) - fee), fee))
            depth[p] -= q
            remain -= q
            if remain == 0:
                break
        if fills:
            batches.append(SellBatch(tuple(grouped), limit, quantity, tuple(fills)))
        grouped, quantity = [], 0
    return batches


@dataclass(frozen=True)
class Preview:
    token: Token
    fills: tuple[Fill, ...]
    bid: int
    ask: int
    budget: int
    exit_coverage: int
    target_proceeds: int

    @property
    def spent(self):
        return sum(f.amount for f in self.fills)

    @property
    def quantity(self):
        return sum(f.net for f in self.fills)

    def rank(self, config: Preferences, now: int):
        return (
            -Fraction(self.bid, max(f.target for f in self.fills)),
            -Fraction(self.exit_coverage, self.quantity),
            -Fraction(self.spent, self.budget),
            -Fraction(self.bid, self.ask),
            -Fraction(self.target_proceeds - self.spent, self.spent),
            self.token.progress(now) * (1 if config.candidateSortDirection == "ASC" else -1),
            self.token.market_id,
            self.token.direction,
            self.token.token_id,
        )


def preview(
    t: Token,
    c: Preferences,
    bids: list[Level],
    asks: list[Level],
    available: int,
    cycle_budget: int,
) -> Preview | None:
    if quote_reason(t, c, bids, asks, available):
        return None
    fills = plan_buy(asks, available, c.max_price, t.min_size, t.fees, t.tick, c)
    if not fills:
        return None
    targets = [Target(str(i), f.target, f.net, i) for i, f in enumerate(fills)]
    batches = sell_batches(bids, targets, t.min_size, t.fees)
    coverage = sum(f.net for b in batches for f in b.fills)
    proceeds = sum(max(0, cost(f.target, f.net) - t.fees.fee(f.net, f.target)) for f in fills)
    return Preview(
        t,
        tuple(fills),
        max(p for p, q in bids if q),
        min(p for p, q in asks if q),
        cycle_budget,
        min(coverage, sum(f.net for f in fills)),
        proceeds,
    )


def arbitrate(previews: list[Preview], c: Preferences, now: int) -> Preview | None:
    if not previews:
        return None
    if len({p.token.event_id for p in previews}) != 1:
        raise ValueError("仲裁必须属于同一Event")
    return min(previews, key=lambda p: p.rank(c, now))


@dataclass
class StopLoss:
    enabled: bool
    multiplier: int
    state: Literal["WATCHING", "ARMED", "EXITING", "STOPPED"] = "WATCHING"
    gross: int = 0
    weighted: int = 0
    first_ns: int | None = None
    last_ns: int | None = None
    last_version: str | None = None

    @property
    def threshold(self):
        return max(1, (self.weighted // self.gross) * self.multiplier // SCALE) if self.gross else 0

    def add(self, price: int, gross: int):
        self.gross += gross
        self.weighted += price * gross
        if self.state not in ("EXITING", "STOPPED"):
            self.state = "WATCHING"
            self.first_ns = self.last_ns = self.last_version = None

    def observe(self, bid: int | None, version: str | None, now: int, ready: bool):
        if not self.enabled or self.state in ("EXITING", "STOPPED") or not ready:
            return
        if bid is None or bid <= 0 or not version:
            return
        if self.last_ns is not None and now < self.last_ns:
            return
        if bid >= self.threshold:
            self.state = "WATCHING"
            self.first_ns = self.last_ns = self.last_version = None
            return
        if version == self.last_version:
            return
        if self.first_ns is None:
            self.first_ns = now
            self.state = "ARMED"
        elif now - self.first_ns >= 30_000_000_000:
            self.state = "EXITING"
        self.last_ns, self.last_version = now, version
