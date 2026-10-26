import pytest

from conftest import FakeResponse, FakeSession
from p2p_scanner.rates import RateProvider, RateSettings, robust_median


def market_router(method, url, params=None, json=None, headers=None, **_):
    if "binance" in url:
        return FakeResponse({"symbol": params["symbol"], "bidPrice": "4.99", "askPrice": "5.01"})
    if "bybit.com/v5" in url:
        return FakeResponse({"retCode": 0, "result": {"list": [{"bid1Price": "5.0", "ask1Price": "5.02"}]}})
    if "okx" in url:
        return FakeResponse({"code": "0", "data": [{"bidPx": "7.0", "askPx": "7.1"}]})  # выброс
    if "coingecko" in url:
        if params["vs_currencies"] == "usd":
            return FakeResponse({"the-open-network": {"usd": 5.0}, "tether": {"usd": 1.0}})
        return FakeResponse({"tether": {"rub": 95.0}})
    if "rapira" in url:
        return FakeResponse({"data": [{"symbol": "USDT/RUB", "bidPrice": 94.9, "askPrice": 95.1, "close": 95}]})
    if "api2.bybit.com" in url:
        prices = ["96.0", "96.2", "96.4"] if json["side"] == "1" else ["94.0", "94.2", "94.4"]
        return FakeResponse({"ret_code": 0, "result": {"items": [{"price": p} for p in prices]}})
    if "crypt.bot" in url:
        assert headers["Crypto-Pay-API-Token"] == "tok"
        return FakeResponse({"ok": True, "result": [
            {"is_valid": True, "source": "USDT", "target": "RUB", "rate": "95.3"},
            {"is_valid": True, "source": "TON", "target": "USD", "rate": "5.0"},
            {"is_valid": True, "source": "USDT", "target": "USD", "rate": "1.0"},
        ]})
    if "cbr" in url:
        return FakeResponse({"Valute": {"USD": {"Nominal": 1, "Value": 92.0}}})
    raise AssertionError(url)


def test_robust_median_drops_outliers():
    value, used = robust_median([95.0, 95.2, 101.0, 0], max_spread_pct=3)
    assert value == pytest.approx(95.1)
    assert used == [True, True, False, False]


def test_stablecoin_reference():
    provider = RateProvider(RateSettings(), FakeSession(market_router))
    ref = provider.reference("usdt", "rub")
    assert ref.coin_usdt == 1.0
    # rapira 95.0, coingecko 95.0, bybit_p2p (96.2 + 94.2) / 2 = 95.2; cryptopay без токена не опрашивается
    assert ref.value == pytest.approx(95.0)
    assert {q.source for q in ref.quotes} == {"rapira", "coingecko", "bybit_p2p"}


def test_cross_rate_with_outlier_and_cryptopay():
    settings = RateSettings(crypto_pay_token="tok")
    provider = RateProvider(settings, FakeSession(market_router))
    ref = provider.reference("TON", "RUB")
    okx = next(q for q in ref.quotes if q.source == "okx")
    assert okx.value == pytest.approx(7.05) and not okx.used
    assert ref.coin_usdt == pytest.approx(5.0)
    assert ref.usdt_fiat == pytest.approx(95.1)  # медиана 95.0, 95.0, 95.2, 95.3
    assert ref.value == pytest.approx(5.0 * 95.1)


def test_failing_sources_are_tolerated():
    def router(method, url, **kwargs):
        if "rapira" in url or "binance" in url:
            return FakeResponse(status_code=503, text="down")
        return market_router(method, url, **kwargs)

    ref = RateProvider(RateSettings(), FakeSession(router)).reference("TON", "RUB")
    errors = {q.source for q in ref.quotes if q.error}
    assert errors == {"rapira", "binance"}
    assert ref.value > 0


def test_all_sources_failed_raises():
    session = FakeSession(lambda *a, **k: FakeResponse(status_code=500, text="x"))
    with pytest.raises(RuntimeError, match="Не удалось"):
        RateProvider(RateSettings(), session).reference("USDT", "RUB")


def test_unknown_source():
    with pytest.raises(ValueError, match="Неизвестный источник"):
        RateProvider(RateSettings(spot_sources=["nope"]), FakeSession(market_router))


def test_cbr_source():
    settings = RateSettings(fiat_sources=["cbr"])
    ref = RateProvider(settings, FakeSession(market_router)).reference("USDT", "RUB")
    assert ref.value == 92.0
