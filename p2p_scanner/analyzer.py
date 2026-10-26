"""Оценка и ранжирование объявлений относительно реального рыночного курса.

Покупка (я покупаю монету по цене P, комиссия f с полученных монет):
    эффективная цена = P / (1 - f);   выгода % = (рынок - эфф. цена) / рынок × 100
Продажа (я продаю монету по цене P, комиссия f с проданных монет):
    эффективная цена = P × (1 - f);   выгода % = (эфф. цена - рынок) / рынок × 100

Выгода > 0 — сделка лучше рынка, < 0 — хуже (переплата при покупке / недополучение при продаже).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from .models import BUY, Deal, Offer


@dataclass
class AnalyzerSettings:
    # Сумма сделки в фиате: отсекает объявления, в лимиты которых она не входит,
    # и используется для расчёта прибыли в деньгах.
    amount: float | None = None
    # Комиссии P2P-маркета CryptoBot: покупателю 0%, продавцу 1% (help.send.tg, «Fees and limits»).
    buy_fee_pct: float = 0.0
    sell_fee_pct: float = 1.0
    # Показывать только сделки с выгодой не ниже этого порога (None — все, отсортированные).
    min_profit_pct: float | None = None
    # Цены дальше этого порога от рынка считаются мусором/ошибкой разбора.
    max_deviation_pct: float = 30.0
    min_orders: int = 0
    min_completion: float = 0.0
    # Фильтр по способам оплаты (подстрока без учёта регистра).
    payment_include: list[str] = field(default_factory=list)
    payment_exclude: list[str] = field(default_factory=list)
    exclude_merchants: list[str] = field(default_factory=list)
    top: int = 10


def effective_price(offer: Offer, settings: AnalyzerSettings) -> float:
    if offer.side == BUY:
        return offer.price / (1 - settings.buy_fee_pct / 100)
    return offer.price * (1 - settings.sell_fee_pct / 100)


def profit_pct(side: str, effective: float, reference: float) -> float:
    if side == BUY:
        return (reference - effective) / reference * 100
    return (effective - reference) / reference * 100


def profit_fiat(offer: Offer, effective: float, reference: float, amount: float) -> float:
    """Прибыль/убыток в фиате на сделку объёмом ``amount`` относительно рыночного курса."""
    if offer.side == BUY:
        # Купили amount / eff монет, по рынку они стоят amount / eff × ref.
        return amount * (reference / effective - 1)
    # Продали amount / P монет: получили amount × (1 - f), по рынку они стоили amount / P × ref.
    return amount * (effective - reference) / offer.price


def _matches(offer: Offer, s: AnalyzerSettings) -> bool:
    if s.amount is not None:
        if offer.min_amount and s.amount < offer.min_amount:
            return False
        limit = offer.max_fiat_volume()
        if limit and s.amount > limit:
            return False
    if s.min_orders and (offer.orders or 0) < s.min_orders:
        return False
    if s.min_completion and offer.completion is not None and offer.completion < s.min_completion:
        return False
    methods = " | ".join(offer.payment_methods).lower()
    if s.payment_include and not any(p.lower() in methods for p in s.payment_include):
        return False
    if s.payment_exclude and any(p.lower() in methods for p in s.payment_exclude):
        return False
    if s.exclude_merchants and offer.merchant.lower() in {m.lower() for m in s.exclude_merchants}:
        return False
    return True


def evaluate(offers: list[Offer], reference: float, settings: AnalyzerSettings) -> list[Deal]:
    """Все подходящие объявления, отсортированные от самого выгодного к худшему."""
    sane = [o for o in offers if abs(o.price - reference) / reference * 100 <= settings.max_deviation_pct]
    median = statistics.median(o.price for o in sane) if sane else None

    deals = []
    for offer in sane:
        if not _matches(offer, settings):
            continue
        eff = effective_price(offer, settings)
        vs_median = None
        if median:
            vs_median = (median - offer.price) / median * 100 if offer.side == BUY else (offer.price - median) / median * 100
        deals.append(
            Deal(
                offer=offer,
                reference=reference,
                effective_price=eff,
                profit_pct=profit_pct(offer.side, eff, reference),
                profit_fiat=None if settings.amount is None else profit_fiat(offer, eff, reference, settings.amount),
                vs_cryptobot_pct=vs_median,
            )
        )
    deals.sort(key=lambda d: d.profit_pct, reverse=True)
    if settings.min_profit_pct is not None:
        deals = [d for d in deals if d.profit_pct >= settings.min_profit_pct]
    return deals


@dataclass
class MarketSummary:
    """Насколько цены CryptoBot в целом отличаются от рынка."""

    side: str
    count: int
    best_price: float | None
    median_price: float | None
    median_vs_market_pct: float | None  # >0 — на CryptoBot дороже рынка
    dropped_outliers: int


def summarize(offers: list[Offer], reference: float, settings: AnalyzerSettings, side: str) -> MarketSummary:
    side_offers = [o for o in offers if o.side == side]
    sane = [o for o in side_offers if abs(o.price - reference) / reference * 100 <= settings.max_deviation_pct]
    if not sane:
        return MarketSummary(side, 0, None, None, None, len(side_offers))
    prices = [o.price for o in sane]
    median = statistics.median(prices)
    best = min(prices) if side == BUY else max(prices)
    return MarketSummary(
        side=side,
        count=len(sane),
        best_price=best,
        median_price=median,
        median_vs_market_pct=(median - reference) / reference * 100,
        dropped_outliers=len(side_offers) - len(sane),
    )


def internal_spread(buy_deals: list[Deal], sell_deals: list[Deal]) -> float | None:
    """Круг внутри CryptoBot: купить у лучшего продавца и продать лучшему покупателю, % с учётом комиссий."""
    if not buy_deals or not sell_deals:
        return None
    best_buy = min(d.effective_price for d in buy_deals)
    best_sell = max(d.effective_price for d in sell_deals)
    return (best_sell - best_buy) / best_buy * 100
