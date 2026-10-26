"""Реальный рыночный курс монеты к фиату.

Курс на P2P-маркете CryptoBot обычно завышен, поэтому эталон берётся не оттуда,
а с внешних площадок:

    курс(монета/фиат) = медиана(спот монета/USDT) × медиана(USDT/фиат)

Спот монета/USDT — Binance, Bybit, OKX, CoinGecko (для USDT = 1).
USDT/фиат — Rapira (биржа с парой USDT/RUB), CoinGecko, P2P Bybit (середина стакана),
курс конвертации самого CryptoBot (Crypto Pay API, нужен токен) и, по желанию, ЦБ РФ.
Источники, которые отклоняются от медианы больше чем на ``max_spread_pct``, отбрасываются.
"""

from __future__ import annotations

import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import requests

from .models import Quote, Reference

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
STABLE = {"USDT"}

COINGECKO_IDS = {
    "USDT": "tether",
    "USDC": "usd-coin",
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "TON": "the-open-network",
    "NOT": "notcoin",
    "TRX": "tron",
    "LTC": "litecoin",
    "BNB": "binancecoin",
    "SOL": "solana",
    "DOGE": "dogecoin",
    "XRP": "ripple",
    "DOGS": "dogs-2",
    "HMSTR": "hamster-kombat",
    "MAJOR": "major",
    "CATI": "catizen",
}


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


def _mid(bid: Any, ask: Any, last: Any = None) -> float:
    bid_f, ask_f = _num(bid), _num(ask)
    if bid_f and ask_f:
        return (bid_f + ask_f) / 2
    value = _num(last) or bid_f or ask_f
    if not value:
        raise ValueError("нет цены в ответе")
    return value


def _num(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


class Source:
    """Базовый источник котировок. ``quote(base, quote)`` бросает исключение при ошибке."""

    name = "base"

    def __init__(self, session: requests.Session, timeout: float = 10.0):
        self.session = session
        self.timeout = timeout
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def _get_json(self, url: str, ttl: float = 0, **kwargs: Any) -> Any:
        """GET/POST с кэшем: источники «все курсы одним запросом» не дёргаются на каждую пару."""
        method = kwargs.pop("method", "GET")
        key = f"{method} {url} {kwargs.get('params')} {kwargs.get('json')}"
        if ttl:
            with self._lock:
                cached = self._cache.get(key)
                if cached and time.monotonic() - cached[0] < ttl:
                    return cached[1]
        resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        resp.raise_for_status()
        data = resp.json()
        if ttl:
            with self._lock:
                self._cache[key] = (time.monotonic(), data)
        return data

    def quote(self, base: str, quote: str) -> float:
        raise NotImplementedError


class Binance(Source):
    name = "binance"

    def quote(self, base: str, quote: str) -> float:
        data = self._get_json("https://api.binance.com/api/v3/ticker/bookTicker", params={"symbol": base + quote})
        return _mid(data.get("bidPrice"), data.get("askPrice"))


class Bybit(Source):
    name = "bybit"

    def quote(self, base: str, quote: str) -> float:
        data = self._get_json(
            "https://api.bybit.com/v5/market/tickers", params={"category": "spot", "symbol": base + quote}
        )
        items = (data.get("result") or {}).get("list") or []
        if not items:
            raise ValueError(data.get("retMsg") or "пара не найдена")
        t = items[0]
        return _mid(t.get("bid1Price"), t.get("ask1Price"), t.get("lastPrice"))


class OKX(Source):
    name = "okx"

    def quote(self, base: str, quote: str) -> float:
        data = self._get_json("https://www.okx.com/api/v5/market/ticker", params={"instId": f"{base}-{quote}"})
        items = data.get("data") or []
        if not items:
            raise ValueError(data.get("msg") or "пара не найдена")
        t = items[0]
        return _mid(t.get("bidPx"), t.get("askPx"), t.get("last"))


class CoinGecko(Source):
    name = "coingecko"

    def quote(self, base: str, quote: str) -> float:
        base_id = COINGECKO_IDS.get(base)
        if not base_id:
            raise ValueError(f"нет id CoinGecko для {base}")
        if quote in STABLE:
            # монета/USDT = (монета/USD) / (USDT/USD)
            ids = f"{base_id},{COINGECKO_IDS[quote]}"
            data = self._get_json(
                "https://api.coingecko.com/api/v3/simple/price", ttl=30, params={"ids": ids, "vs_currencies": "usd"}
            )
            return float(data[base_id]["usd"]) / float(data[COINGECKO_IDS[quote]]["usd"])
        vs = quote.lower()
        data = self._get_json(
            "https://api.coingecko.com/api/v3/simple/price", ttl=30, params={"ids": base_id, "vs_currencies": vs}
        )
        value = (data.get(base_id) or {}).get(vs)
        if not value:
            raise ValueError(f"CoinGecko не знает пару {base}/{quote}")
        return float(value)


class BybitP2P(Source):
    """Середина P2P-стакана Bybit: медиана топ-N продавцов и покупателей."""

    name = "bybit_p2p"
    top = 10

    def _side_median(self, base: str, quote: str, side: str) -> float:
        payload = {
            "userId": "", "tokenId": base, "currencyId": quote, "payment": [], "side": side,
            "size": str(self.top), "page": "1", "amount": "", "authMaker": False, "canTrade": False,
        }
        data = self._get_json("https://api2.bybit.com/fiat/otc/item/online", method="POST", json=payload)
        items = (data.get("result") or {}).get("items") or []
        prices = [p for p in (_num(i.get("price")) for i in items) if p]
        if not prices:
            raise ValueError(data.get("ret_msg") or "пустой стакан")
        return statistics.median(prices)

    def quote(self, base: str, quote: str) -> float:
        # side "1" — объявления продавцов (я покупаю), "0" — покупателей (я продаю).
        return (self._side_median(base, quote, "1") + self._side_median(base, quote, "0")) / 2


class Rapira(Source):
    name = "rapira"

    def quote(self, base: str, quote: str) -> float:
        data = self._get_json("https://api.rapira.net/open/market/rates", ttl=15)
        symbol = f"{base}/{quote}"
        for item in data.get("data") or []:
            if str(item.get("symbol", "")).upper() == symbol:
                return _mid(item.get("bidPrice"), item.get("askPrice"), item.get("close"))
        raise ValueError(f"нет пары {symbol}")


class CBR(Source):
    """Официальный курс ЦБ РФ (USDT ≈ USD). Часто отличается от рынка на 1–3%."""

    name = "cbr"

    def quote(self, base: str, quote: str) -> float:
        if quote != "RUB" or base not in STABLE | {"USDC"}:
            raise ValueError("только USDT/RUB")
        data = self._get_json("https://www.cbr-xml-daily.ru/daily_json.js", ttl=600)
        usd = data["Valute"]["USD"]
        return float(usd["Value"]) / float(usd.get("Nominal", 1))


class CryptoPay(Source):
    """Курс конвертации самого CryptoBot (Crypto Pay API → getExchangeRates)."""

    name = "cryptopay"

    def __init__(self, session: requests.Session, timeout: float = 10.0, token: str | None = None):
        super().__init__(session, timeout)
        self.token = token

    def _rates(self) -> dict[tuple[str, str], float]:
        if not self.token:
            raise ValueError("не задан CRYPTO_PAY_TOKEN")
        data = self._get_json(
            "https://pay.crypt.bot/api/getExchangeRates", ttl=30, headers={"Crypto-Pay-API-Token": self.token}
        )
        if not data.get("ok"):
            raise ValueError(str(data.get("error") or "ошибка Crypto Pay API"))
        return {
            (r["source"].upper(), r["target"].upper()): float(r["rate"])
            for r in data.get("result") or []
            if r.get("is_valid", True) and _num(r.get("rate"))
        }

    def quote(self, base: str, quote: str) -> float:
        rates = self._rates()
        if (base, quote) in rates:
            return rates[(base, quote)]
        if quote in STABLE and (base, "USD") in rates and (quote, "USD") in rates:
            return rates[(base, "USD")] / rates[(quote, "USD")]
        raise ValueError(f"нет пары {base}/{quote}")


SOURCE_CLASSES: dict[str, type[Source]] = {
    cls.name: cls for cls in (Binance, Bybit, OKX, CoinGecko, BybitP2P, Rapira, CBR, CryptoPay)
}


def robust_median(values: list[float], max_spread_pct: float) -> tuple[float | None, list[bool]]:
    """Медиана без выбросов. Возвращает значение и флаги «использован» для каждого входа."""
    valid = [v for v in values if v and v > 0]
    if not valid:
        return None, [False] * len(values)
    center = statistics.median(valid)
    used = [bool(v and v > 0 and abs(v - center) / center * 100 <= max_spread_pct) for v in values]
    kept = [v for v, u in zip(values, used) if u]
    return statistics.median(kept), used


@dataclass
class RateSettings:
    spot_sources: list[str] = field(default_factory=lambda: ["binance", "bybit", "okx", "coingecko"])
    fiat_sources: list[str] = field(default_factory=lambda: ["rapira", "coingecko", "bybit_p2p", "cryptopay"])
    max_spread_pct: float = 3.0
    timeout: float = 10.0
    crypto_pay_token: str | None = None


class RateProvider:
    def __init__(self, settings: RateSettings | None = None, session: requests.Session | None = None):
        self.settings = settings or RateSettings()
        session = session or make_session()
        self.sources: dict[str, Source] = {}
        for name in {*self.settings.spot_sources, *self.settings.fiat_sources}:
            cls = SOURCE_CLASSES.get(name)
            if cls is None:
                raise ValueError(f"Неизвестный источник курса: {name}. Доступны: {', '.join(SOURCE_CLASSES)}")
            if cls is CryptoPay:
                self.sources[name] = CryptoPay(session, self.settings.timeout, self.settings.crypto_pay_token)
            else:
                self.sources[name] = cls(session, self.settings.timeout)

    def _collect(self, names: list[str], base: str, quote: str) -> list[Quote]:
        def ask(name: str) -> Quote:
            pair = f"{base}/{quote}"
            try:
                return Quote(source=name, pair=pair, value=self.sources[name].quote(base, quote))
            except Exception as exc:  # noqa: BLE001 — любой сбой источника не должен ронять скан
                return Quote(source=name, pair=pair, error=_short_error(exc))

        if name_list := [n for n in names if n != "cryptopay" or self.settings.crypto_pay_token]:
            with ThreadPoolExecutor(max_workers=len(name_list)) as pool:
                return list(pool.map(ask, name_list))
        return []

    def _aggregate(self, quotes: list[Quote]) -> float | None:
        value, used = robust_median([q.value or 0 for q in quotes], self.settings.max_spread_pct)
        for q, u in zip(quotes, used):
            q.used = u
        return value

    def reference(self, coin: str, fiat: str) -> Reference:
        coin, fiat = coin.upper(), fiat.upper()
        quotes: list[Quote] = []

        if coin in STABLE:
            coin_usdt: float | None = 1.0
        else:
            spot = self._collect(self.settings.spot_sources, coin, "USDT")
            coin_usdt = self._aggregate(spot)
            quotes += spot

        if fiat in STABLE:
            usdt_fiat: float | None = 1.0
        else:
            fiat_quotes = self._collect(self.settings.fiat_sources, "USDT", fiat)
            usdt_fiat = self._aggregate(fiat_quotes)
            quotes += fiat_quotes
            if usdt_fiat is None and fiat == "USD":
                usdt_fiat = 1.0

        if coin_usdt is None or usdt_fiat is None:
            failed = "; ".join(f"{q.source} {q.pair}: {q.error}" for q in quotes if q.error)
            raise RuntimeError(f"Не удалось получить рыночный курс {coin}/{fiat}. {failed}")
        return Reference(coin, fiat, coin_usdt * usdt_fiat, coin_usdt, usdt_fiat, quotes)


def manual_reference(coin: str, fiat: str, value: float) -> Reference:
    return Reference(coin.upper(), fiat.upper(), value, 0.0, 0.0, [], manual=True)


def _short_error(exc: Exception) -> str:
    if isinstance(exc, requests.Timeout):
        return "таймаут"
    if isinstance(exc, requests.ConnectionError):
        return "нет соединения"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    text = str(exc) or exc.__class__.__name__
    return text if len(text) <= 120 else text[:117] + "..."
