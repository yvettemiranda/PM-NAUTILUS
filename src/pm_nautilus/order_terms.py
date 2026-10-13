"""Pure outbound CLOB terms shared by TEST and LIVE, in integer micro-units."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from .config import SCALE


SHARE_STEP = 10_000  # The pinned CLOB SDK signs SELL sizes to two decimal places.
SUPPORTED_CLOB_TICKS = frozenset({100_000, 10_000, 1_000, 100})


class OrderTermsError(ValueError):
    """An application-owned, safe reason to reject an unposted order."""


@dataclass(frozen=True)
class SellTerms:
    quantity: int
    limit: int
    tick: int


def sell_minimum(min_size: int) -> int:
    return (min_size + SHARE_STEP - 1) // SHARE_STEP * SHARE_STEP


def normalize_sell(quantity: int, limit: int, tick: int, min_size: int) -> SellTerms:
    """Never round a sell below its requested floor or above its owned quantity."""
    if tick not in SUPPORTED_CLOB_TICKS:
        raise OrderTermsError("市场价格档位超出锁定SDK支持范围")
    quantity = quantity // SHARE_STEP * SHARE_STEP
    limit = (max(1, limit) + tick - 1) // tick * tick
    if quantity <= 0 or quantity < min_size:
        raise OrderTermsError("可签名卖出份额不足交易所最小订单")
    if not 0 < limit <= SCALE - tick:
        raise OrderTermsError("卖出最低价没有合法市场档位")
    return SellTerms(quantity, limit, tick)


def signed_amounts(signed) -> tuple[int, int]:
    try:
        values = (signed.makerAmount, signed.takerAmount)
        if any(type(v) not in (int, str) for v in values):
            raise ValueError
        if any(isinstance(v, str) and not v.isdecimal() for v in values):
            raise ValueError
        maker, taker = map(int, values)
    except (AttributeError, TypeError, ValueError):
        raise OrderTermsError("签名订单金额无效") from None
    if maker <= 0 or taker <= 0:
        raise OrderTermsError("签名订单金额无效")
    return maker, taker


def check_signed_sell(signed, terms: SellTerms) -> tuple[int, int]:
    maker, taker = signed_amounts(signed)
    if maker != terms.quantity or taker * SCALE < maker * terms.limit:
        raise OrderTermsError("签名卖单份额或最低价与出站意图不一致")
    if taker * SCALE > maker * (SCALE - terms.tick):
        raise OrderTermsError("签名卖单价格没有合法市场档位")
    return maker, taker


def canonical_buy_taker(maker: int, limit: int, tick: int) -> int:
    """Pinned SDK BUY division, floored to its exact token amount precision."""
    if tick not in SUPPORTED_CLOB_TICKS or limit <= 0 or limit % tick:
        raise OrderTermsError("买入签名价格档位无效")
    step = tick // 100  # ROUNDING_CONFIG.amount = price decimals + 2.
    return maker * SCALE // (limit * step) * step


def exact_sdk_buy_amounts(amount, price, price_digits: int, amount_digits: int):
    """Retain SDK cent/price/taker floors without binary float underflow."""
    try:
        quote, limit = Decimal(str(amount)), Decimal(str(price))
        if not quote.is_finite() or not limit.is_finite() or quote <= 0 or limit <= 0:
            raise ValueError
        quote = quote.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        rounded_limit = limit.quantize(Decimal(1).scaleb(-price_digits), rounding=ROUND_DOWN)
        if limit != rounded_limit:
            raise ValueError  # The program must supply its original legal tick.
        limit = rounded_limit
        maker, price_micro = int(quote * SCALE), int(limit * SCALE)
        tick = 10 ** (6 - price_digits)
        if amount_digits != price_digits + 2 or not 0 < price_micro < SCALE:
            raise ValueError
        return maker, canonical_buy_taker(maker, price_micro, tick)
    except (ArithmeticError, TypeError, ValueError):
        raise OrderTermsError("买入签名金额或价格无效") from None


def check_signed_buy(
    signed, notional: int, limit: int, min_size: int, tick: int | None = None
) -> tuple[int, int]:
    maker, taker = signed_amounts(signed)
    price_ok = maker * SCALE <= taker * limit
    if not price_ok and tick is not None:
        # The official SDK floors taker precision, making the ratio very slightly
        # higher than its original tick. Accept only that canonical quantization;
        # the next executable tick must remain strictly out of reach.
        price_ok = taker == canonical_buy_taker(maker, limit, tick) and maker * SCALE < taker * (
            limit + tick
        )
    if maker > notional or maker % SHARE_STEP or taker < min_size or not price_ok:
        raise OrderTermsError("签名订单金额/价格/最小份额未通过含费预算核对")
    return maker, taker
