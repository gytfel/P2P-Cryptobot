"""Извлечение объявлений из JSON-ответа P2P-маркета.

Формат внутреннего API мини-приложения CryptoBot не документирован и может меняться,
поэтому поля ищутся по набору типичных имён (price, minAmount, user.nickname и т.д.).
Если автоопределение ошиблось, любое поле можно задать явно dot-путём в конфиге:
``price = "price.value"``, ``merchant = "user.nickname"``, ``list = "data.items"``.
"""

from __future__ import annotations

import re
from typing import Any

from .models import Offer

PRICE_KEYS = ("price", "rate", "exchangerate", "unitprice", "fixedprice", "pricevalue", "cost")
MIN_KEYS = (
    "minamount", "minlimit", "minorderamount", "minfiat", "minfiatamount", "lowlimit",
    "limitmin", "minsingletransamount", "minordersize", "minvolume", "min",
)
MAX_KEYS = (
    "maxamount", "maxlimit", "maxorderamount", "maxfiat", "maxfiatamount", "highlimit",
    "limitmax", "maxsingletransamount", "maxordersize", "maxvolume", "max",
)
LIMIT_CONTAINERS = ("limits", "orderamountlimits", "amountlimits", "limit", "orderlimits", "fiatlimits")
AVAILABLE_KEYS = (
    "availablevolume", "availableamount", "available", "lastquantity", "remainingamount",
    "remaining", "surplusamount", "cryptoamount", "quantity", "volume", "amount",
)
MERCHANT_KEYS = (
    "nickname", "merchantname", "tradername", "makername", "sellername", "username",
    "displayname", "name",
)
MERCHANT_CONTAINERS = ("user", "merchant", "maker", "trader", "seller", "buyer", "owner", "advertiser", "author")
ORDERS_KEYS = (
    "totalorderscount", "orderscount", "ordercount", "completedorders", "completedorderscount",
    "tradescount", "totaltrades", "recentordernum", "dealscount", "deals", "trades", "orders",
)
COMPLETION_KEYS = (
    "successrate", "completionrate", "successpercent", "recentexecuterate", "finishrate",
    "completedrate", "successfulrate",
)
STATS_CONTAINERS = ("statistics", "stats", "rating", *MERCHANT_CONTAINERS)
PAYMENT_KEYS = ("paymentmethods", "payments", "paymentmethod", "payment", "paytypes", "banks", "methods")
PAYMENT_NAME_KEYS = ("name", "title", "bankname", "nameeng", "code", "id")
ID_KEYS = ("id", "offerid", "adid", "itemid", "advid", "uuid", "number")

_NUMBER_RE = re.compile(r"-?\d(?:[\d\s\u00a0\u202f.,]*\d)?")


def _norm(key: str) -> str:
    return re.sub(r"[_\-\s]", "", key).lower()


def to_float(value: Any) -> float | None:
    """Число из int/float/строки вида '1 000,50', '97.5 RUB' или {'value': '97.5'}."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("value", "amount", "price", "rate"):
            for k, v in value.items():
                if _norm(k) == key:
                    return to_float(v)
        return None
    if isinstance(value, str):
        m = _NUMBER_RE.search(value)
        if not m:
            return None
        num = re.sub(r"[\s\u00a0\u202f]", "", m.group(0))
        if "," in num and "." in num:
            # Десятичный разделитель — последний из двух: '1,000.50' или '1.000,50'.
            thousands = "," if num.rfind(".") > num.rfind(",") else "."
            num = num.replace(thousands, "").replace(",", ".")
        elif num.count(",") == 1:
            num = num.replace(",", ".")
        elif num.count(",") > 1 or num.count(".") > 1:
            num = num.replace(",", "").replace(".", "")
        try:
            return float(num)
        except ValueError:
            return None
    return None


def get_path(obj: Any, path: str) -> Any:
    """Значение по dot-пути: 'data.items', 'user.nickname', 'limits.0.min'."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
                continue
            matches = [v for k, v in cur.items() if _norm(k) == _norm(part)]
            if not matches:
                return None
            cur = matches[0]
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _present(value: Any) -> bool:
    return value not in (None, "", [], {})


def _numeric(value: Any) -> bool:
    return to_float(value) is not None


def _texty(value: Any) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip() != ""


def _lookup(obj: dict[str, Any], keys: tuple[str, ...], accept=_present) -> Any:
    """Первое подходящее значение по списку ключей (порядок ключей = приоритет)."""
    normed = {_norm(k): v for k, v in obj.items()}
    for key in keys:
        if key in normed and accept(normed[key]):
            return normed[key]
    return None


def _containers(obj: dict[str, Any], names: tuple[str, ...]) -> list[dict[str, Any]]:
    normed = {_norm(k): v for k, v in obj.items()}
    return [normed[n] for n in names if isinstance(normed.get(n), dict)]


def _find(obj: dict[str, Any], keys: tuple[str, ...], containers: tuple[str, ...] = (), accept=_present, depth: int = 2) -> Any:
    """Ищет ключ в объекте, затем во вложенных словарях-контейнерах (user.statistics и т.п.)."""
    value = _lookup(obj, keys, accept)
    if value is not None or depth == 0:
        return value
    for sub in _containers(obj, containers):
        value = _find(sub, keys, containers, accept, depth - 1)
        if value is not None:
            return value
    return None


def _has_price(item: Any) -> bool:
    return isinstance(item, dict) and _lookup(item, PRICE_KEYS, _numeric) is not None


def locate_offer_list(data: Any) -> tuple[str, list[dict[str, Any]]]:
    """Самый длинный список словарей, где хотя бы у половины есть цена, и dot-путь к нему."""
    best_path, best = "", []
    stack: list[tuple[Any, str]] = [(data, "")]
    while stack:
        node, path = stack.pop()
        if path.count(".") > 6:
            continue
        if isinstance(node, list):
            dicts = [x for x in node if isinstance(x, dict)]
            if dicts and sum(_has_price(x) for x in dicts) * 2 >= len(dicts) and len(dicts) > len(best):
                best_path, best = path, dicts
            stack.extend((x, f"{path}.{i}".lstrip(".")) for i, x in enumerate(node) if isinstance(x, dict))
        elif isinstance(node, dict):
            stack.extend((v, f"{path}.{k}".lstrip(".")) for k, v in node.items() if isinstance(v, (dict, list)))
    return best_path, best


def find_offer_list(data: Any, path: str | None = None) -> list[dict[str, Any]]:
    """Список объявлений по явному пути или найденный автоматически."""
    if path:
        found = get_path(data, path)
        if not isinstance(found, list):
            raise ValueError(f"По пути '{path}' в ответе нет списка объявлений")
        return [x for x in found if isinstance(x, dict)]
    return locate_offer_list(data)[1]


def _merchant(item: dict[str, Any]) -> Any:
    # Голое "name" на верхнем уровне часто означает что-то другое, поэтому ищем его
    # только внутри user/merchant/...; в конце — вариант "user": "nickname".
    value = _lookup(item, MERCHANT_KEYS[:-1], _texty)
    if value is not None:
        return value
    for sub in _containers(item, MERCHANT_CONTAINERS):
        value = _lookup(sub, MERCHANT_KEYS, _texty)
        if value is not None:
            return value
    return _lookup(item, MERCHANT_CONTAINERS, _texty)


def _payment_names(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    names = []
    for item in items:
        if isinstance(item, dict):
            name = _lookup(item, PAYMENT_NAME_KEYS, _texty)
            if name is not None:
                names.append(str(name))
        elif _texty(item):
            names.append(str(item))
    return names


def _completion(value: Any) -> float | None:
    num = to_float(value)
    if num is None:
        return None
    return num * 100 if 0 < num <= 1 else num


def parse_offer(item: dict[str, Any], side: str, coin: str, fiat: str, fields: dict[str, str] | None = None) -> Offer | None:
    """Превращает один элемент ответа в Offer. Возвращает None, если нет цены."""
    fields = fields or {}

    def pick(name: str, finder):
        return get_path(item, fields[name]) if name in fields else finder()

    price = to_float(pick("price", lambda: _find(item, PRICE_KEYS, (), _numeric)))
    if price is None or price <= 0:
        return None

    merchant = pick("merchant", lambda: _merchant(item))
    orders = to_float(pick("orders", lambda: _find(item, ORDERS_KEYS, STATS_CONTAINERS, _numeric)))
    offer_id = pick("id", lambda: _lookup(item, ID_KEYS, _texty))
    return Offer(
        side=side,
        coin=coin,
        fiat=fiat,
        price=price,
        min_amount=to_float(pick("min_amount", lambda: _find(item, MIN_KEYS, LIMIT_CONTAINERS, _numeric))),
        max_amount=to_float(pick("max_amount", lambda: _find(item, MAX_KEYS, LIMIT_CONTAINERS, _numeric))),
        available=to_float(pick("available", lambda: _find(item, AVAILABLE_KEYS, (), _numeric))),
        merchant=str(merchant) if _texty(merchant) else "?",
        orders=None if orders is None else int(orders),
        completion=_completion(pick("completion", lambda: _find(item, COMPLETION_KEYS, STATS_CONTAINERS, _numeric))),
        payment_methods=_payment_names(pick("payment_methods", lambda: _find(item, PAYMENT_KEYS))),
        offer_id=str(offer_id) if _texty(offer_id) else None,
        raw=item,
    )


def extract_offers(
    data: Any, side: str, coin: str, fiat: str, fields: dict[str, str] | None = None
) -> list[Offer]:
    fields = dict(fields or {})
    items = find_offer_list(data, fields.pop("list", None))
    offers = (parse_offer(item, side, coin, fiat, fields) for item in items)
    return [o for o in offers if o is not None]
