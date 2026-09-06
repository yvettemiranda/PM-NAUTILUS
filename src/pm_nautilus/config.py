"""One validated configuration shared by TEST, LIVE and the original UI."""

from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCALE = 1_000_000
SAFE_INTEGER = 9_007_199_254_740_991


def micros(value: object) -> int:
    d = Decimal(str(value))
    if not d.is_finite():
        raise ValueError("数值必须有限")
    n = int((d * SCALE).to_integral_value(rounding=ROUND_HALF_UP))
    if abs(n) > SAFE_INTEGER:
        raise ValueError("数值超出支持范围")
    return n


def units(value: int | None) -> str | None:
    return None if value is None else format(Decimal(value) / SCALE, "f")


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    marketTypes: list[Literal["BINARY", "TERNARY", "MULTI"]] = ["BINARY", "TERNARY"]
    allCategories: bool = True
    selectedCategoryIds: list[str] = []
    candidateSortDirection: Literal["ASC", "DESC"] = "ASC"
    minBuyPriceCents: Decimal = Decimal("0.1")
    maxBuyPriceCents: Decimal = Decimal("99")
    targetSellPriceIncreaseCents: Decimal = Decimal("1")
    targetSellPriceMultiplier: Decimal = Decimal("1.5")
    stopLossEnabled: bool = True
    stopLossMultiplier: Decimal = Decimal("0.4")
    minBidAskRatioPercent: int = Field(50, ge=1, le=100, strict=True)
    minMarketDurationDays: int = Field(1, ge=1, le=365, strict=True)
    maxMarketDurationDays: int = Field(30, ge=1, le=365, strict=True)
    maxMarketProgressPercent: int = Field(20, ge=1, le=100, strict=True)
    orderAmount: Decimal = Decimal("1")

    @model_validator(mode="after")
    def check(self):
        if not self.marketTypes:
            raise ValueError("请至少选择一种市场类型")
        for p in (self.minBuyPriceCents, self.maxBuyPriceCents):
            if not p.is_finite() or not Decimal("0.1") <= p <= 99 or p * 10 % 1:
                raise ValueError("买价须为0.1–99美分，步进0.1美分")
        if self.minBuyPriceCents > self.maxBuyPriceCents:
            raise ValueError("最低买价不得超过最高买价")
        if self.minMarketDurationDays > self.maxMarketDurationDays:
            raise ValueError("最短总时长不得超过最长总时长")
        if not 0 <= self.targetSellPriceIncreaseCents <= 99:
            raise ValueError("目标加价须为0–99美分")
        if self.targetSellPriceMultiplier < 0:
            raise ValueError("目标倍数不得小于0")
        if not 0 < self.stopLossMultiplier < 1:
            raise ValueError("止损倍数必须大于0且小于1")
        for n in (self.targetSellPriceMultiplier, self.stopLossMultiplier):
            micros(n)
        if not 0 < self.orderAmount <= 1_000_000 or micros(self.orderAmount) == 0:
            raise ValueError("每轮金额须大于0且不超过1000000U")
        if not self.allCategories and not self.selectedCategoryIds:
            raise ValueError("请至少选择一个官方栏目")
        if any(not x.strip() or len(x) > 80 for x in self.selectedCategoryIds):
            raise ValueError("栏目ID无效")
        return self

    @property
    def budget(self):
        return micros(self.orderAmount)

    @property
    def min_price(self):
        return micros(self.minBuyPriceCents / 100)

    @property
    def max_price(self):
        return micros(self.maxBuyPriceCents / 100)

    @property
    def increase(self):
        return micros(self.targetSellPriceIncreaseCents / 100)

    @property
    def multiplier(self):
        return micros(self.targetSellPriceMultiplier)

    def public(self):
        out = self.model_dump(mode="json")
        for k in (
            "minBuyPriceCents",
            "maxBuyPriceCents",
            "targetSellPriceIncreaseCents",
            "targetSellPriceMultiplier",
            "stopLossMultiplier",
        ):
            out[k] = float(out[k])
        out["selectedCategories"] = self.selectedCategoryIds
        out["minBuyPrice"] = units(self.min_price)
        out["maxBuyPrice"] = units(self.max_price)
        return out
