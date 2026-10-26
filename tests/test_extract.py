import json

import pytest

from p2p_scanner.extract import extract_offers, get_path, locate_offer_list, to_float


@pytest.mark.parametrize(
    "raw, expected",
    [
        (97.5, 97.5),
        ("97.50", 97.5),
        ("97,5", 97.5),
        ("1 000,50", 1000.5),
        ("1,000.50", 1000.5),
        ("1.000,50", 1000.5),
        ("95.10 RUB", 95.1),
        ({"value": "96.4", "type": "FIXED"}, 96.4),
        ("", None),
        (None, None),
        (True, None),
    ],
)
def test_to_float(raw, expected):
    assert to_float(raw) == expected


def test_nested_format(examples_dir):
    data = json.loads((examples_dir / "sample_buy.json").read_text(encoding="utf-8"))
    assert locate_offer_list(data)[0] == "data.offers"
    offers = extract_offers(data, "buy", "USDT", "RUB")
    first = offers[0]
    assert first.price == 96.4
    assert (first.min_amount, first.max_amount) == (1000, 150000)
    assert first.available == 2500
    assert first.merchant == "fast_usdt"
    assert first.orders == 840
    assert first.completion == pytest.approx(99)
    assert first.payment_methods == ["СБП", "Т-Банк"]
    assert first.offer_id == "101"


def test_flat_format_like_bybit():
    data = {
        "ret_code": 0,
        "result": {
            "count": 2,
            "items": [
                {"id": "a1", "nickName": "Bob", "price": "95.3", "lastQuantity": "120.5", "minAmount": "500",
                 "maxAmount": "10000", "payments": ["75", "14"], "recentOrderNum": 77, "recentExecuteRate": 98},
                {"id": "a2", "nickName": "Ann", "price": "95.8", "lastQuantity": "10", "minAmount": "100",
                 "maxAmount": "900", "payments": [], "recentOrderNum": 5, "recentExecuteRate": 60},
            ],
        },
    }
    offers = extract_offers(data, "sell", "USDT", "RUB")
    assert [o.merchant for o in offers] == ["Bob", "Ann"]
    assert offers[0].available == 120.5
    assert offers[0].orders == 77
    assert offers[0].completion == 98
    assert offers[0].payment_methods == ["75", "14"]


def test_explicit_fields_override_detection():
    data = {"rows": [{"p": "90", "who": {"login": "x"}, "price": "1"}]}
    fields = {"list": "rows", "price": "p", "merchant": "who.login"}
    [offer] = extract_offers(data, "buy", "USDT", "RUB", fields)
    assert offer.price == 90
    assert offer.merchant == "x"


def test_skips_items_without_price_and_empty_response():
    assert extract_offers({"data": []}, "buy", "USDT", "RUB") == []
    data = {"items": [{"price": "95"}, {"price": None}, {"note": "ad"}, {"price": "96"}]}
    assert [o.price for o in extract_offers(data, "buy", "USDT", "RUB")] == [95, 96]


def test_get_path():
    obj = {"Data": {"items": [{"a": 1}]}}
    assert get_path(obj, "data.items.0.a") == 1
    assert get_path(obj, "data.missing") is None
