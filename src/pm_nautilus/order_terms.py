"""Pure outbound CLOB terms shared by TEST and LIVE, in integer micro-units."""

from dataclasses import dataclass

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


def check_signed_buy(signed, notional: int, limit: int, min_size: int) -> tuple[int, int]:
    maker, taker = signed_amounts(signed)
    if maker > notional or taker < min_size or maker * SCALE > taker * limit:
        raise OrderTermsError("签名订单金额/价格/最小份额未通过含费预算核对")
    return maker, taker
