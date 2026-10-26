"""Модели данных: объявление P2P, оценённая сделка, рыночный курс."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Сторона сделки всегда указывается с точки зрения пользователя:
#   BUY  — я покупаю монету (смотрю объявления продавцов);
#   SELL — я продаю монету (смотрю объявления покупателей).
BUY = "buy"
SELL = "sell"
SIDES = (BUY, SELL)

SIDE_TITLES = {
    BUY: "ПОКУПКА",
    SELL: "ПРОДАЖА",
}


@dataclass
class Offer:
    side: str
    coin: str
    fiat: str
    price: float
    min_amount: float | None = None
    max_amount: float | None = None
    available: float | None = None
    merchant: str = "?"
    orders: int | None = None
    completion: float | None = None
    payment_methods: list[str] = field(default_factory=list)
    offer_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def key(self) -> str:
        """Стабильный ключ объявления для дедупликации уведомлений."""
        if self.offer_id:
            return f"{self.side}:{self.coin}:{self.fiat}:{self.offer_id}"
        return f"{self.side}:{self.coin}:{self.fiat}:{self.merchant}:{self.price}"

    def max_fiat_volume(self) -> float | None:
        """Максимальный объём сделки в фиате с учётом лимита и остатка монет."""
        limits = []
        if self.max_amount:
            limits.append(self.max_amount)
        if self.available:
            limits.append(self.available * self.price)
        return min(limits) if limits else None


@dataclass
class Quote:
    """Одна котировка от одного источника."""

    source: str
    pair: str
    value: float | None = None
    error: str | None = None
    used: bool = False


@dataclass
class Reference:
    """Справедливый рыночный курс монеты к фиату."""

    coin: str
    fiat: str
    value: float
    coin_usdt: float
    usdt_fiat: float
    quotes: list[Quote] = field(default_factory=list)
    manual: bool = False


@dataclass
class Deal:
    offer: Offer
    reference: float
    effective_price: float
    profit_pct: float
    profit_fiat: float | None = None
    vs_cryptobot_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        o = self.offer
        return {
            "side": o.side,
            "coin": o.coin,
            "fiat": o.fiat,
            "merchant": o.merchant,
            "price": o.price,
            "effective_price": round(self.effective_price, 6),
            "reference": round(self.reference, 6),
            "profit_pct": round(self.profit_pct, 3),
            "profit_fiat": None if self.profit_fiat is None else round(self.profit_fiat, 2),
            "vs_cryptobot_pct": None if self.vs_cryptobot_pct is None else round(self.vs_cryptobot_pct, 3),
            "min_amount": o.min_amount,
            "max_amount": o.max_amount,
            "available": o.available,
            "orders": o.orders,
            "completion": o.completion,
            "payment_methods": o.payment_methods,
            "offer_id": o.offer_id,
        }
