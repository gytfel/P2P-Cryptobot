import pytest

from p2p_scanner.analyzer import AnalyzerSettings, evaluate, internal_spread, summarize
from p2p_scanner.models import BUY, SELL, Offer


def offer(side, price, **kw):
    kw.setdefault("merchant", f"m{price}")
    return Offer(side=side, coin="USDT", fiat="RUB", price=price, **kw)


def test_buy_profit_and_sorting():
    offers = [offer(BUY, 96.0), offer(BUY, 94.0), offer(BUY, 100.0)]
    deals = evaluate(offers, 95.0, AnalyzerSettings(amount=9400))
    assert [d.offer.price for d in deals] == [94.0, 96.0, 100.0]
    best = deals[0]
    assert best.profit_pct == pytest.approx((95 - 94) / 95 * 100)
    # за 9400 ₽ купили 100 USDT, по рынку они стоят 9500 ₽
    assert best.profit_fiat == pytest.approx(100)


def test_sell_fee_is_applied():
    settings = AnalyzerSettings(sell_fee_pct=1.0, amount=10000)
    [deal] = evaluate([offer(SELL, 100.0)], 98.0, settings)
    assert deal.effective_price == pytest.approx(99.0)
    assert deal.profit_pct == pytest.approx((99 - 98) / 98 * 100)
    # продали 100 USDT (по рынку 9800 ₽), получили 9900 ₽
    assert deal.profit_fiat == pytest.approx(100)


def test_buy_fee_is_applied():
    [deal] = evaluate([offer(BUY, 99.0)], 100.0, AnalyzerSettings(buy_fee_pct=1.0))
    assert deal.effective_price == pytest.approx(100.0)
    assert deal.profit_pct == pytest.approx(0.0)


def test_filters():
    offers = [
        offer(BUY, 94.0, min_amount=50000, max_amount=100000),  # сумма ниже минимума
        offer(BUY, 94.1, max_amount=5000),  # сумма выше максимума
        offer(BUY, 94.2, available=50),  # 50 USDT ≈ 4710 ₽ < 10000 ₽
        offer(BUY, 94.3, orders=2),  # мало сделок
        offer(BUY, 94.4, orders=500, completion=80),  # низкий процент успешных
        offer(BUY, 94.5, orders=500, payment_methods=["Сбербанк"]),  # не та оплата
        offer(BUY, 9.0, orders=500),  # опечатка/мусор
        offer(BUY, 94.9, orders=500, completion=99, payment_methods=["СБП", "Т-Банк"]),
    ]
    settings = AnalyzerSettings(amount=10000, min_orders=10, min_completion=95, payment_include=["сбп"])
    deals = evaluate(offers, 95.0, settings)
    assert [d.offer.price for d in deals] == [94.9]


def test_min_profit_and_exclusions():
    offers = [offer(SELL, 96.0), offer(SELL, 95.0, merchant="scam"), offer(SELL, 94.0)]
    settings = AnalyzerSettings(sell_fee_pct=0, min_profit_pct=0, exclude_merchants=["SCAM"])
    assert [d.offer.price for d in evaluate(offers, 95.0, settings)] == [96.0]


def test_vs_cryptobot_median():
    deals = evaluate([offer(BUY, 94.0), offer(BUY, 96.0), offer(BUY, 98.0)], 95.0, AnalyzerSettings())
    assert deals[0].vs_cryptobot_pct == pytest.approx((96 - 94) / 96 * 100)


def test_summary_and_spread():
    s = AnalyzerSettings(sell_fee_pct=1.0)
    buys = [offer(BUY, 96.0), offer(BUY, 98.0)]
    sells = [offer(SELL, 94.0), offer(SELL, 97.5)]
    summary = summarize(buys + sells, 95.0, s, BUY)
    assert summary.count == 2
    assert summary.best_price == 96.0
    assert summary.median_vs_market_pct == pytest.approx((97 - 95) / 95 * 100)
    spread = internal_spread(evaluate(buys, 95.0, s), evaluate(sells, 95.0, s))
    assert spread == pytest.approx((97.5 * 0.99 - 96.0) / 96.0 * 100)
    assert internal_spread([], []) is None
