import json

import pytest

from conftest import FakeResponse, FakeSession
from p2p_scanner.cryptobot import CryptoBotClient, CryptoBotError, CryptoBotSettings

TEMPLATE = """curl 'https://p2p.example.tg/offers' \\
  -H 'authorization: tma query_id=1&user=2&hash=3' \\
  -H 'content-type: application/json' \\
  -H 'content-length: 99' \\
  -H 'accept-encoding: gzip, deflate, br, zstd' \\
  --data-raw '{"type":"{{side}}","asset":"{{coin}}","fiat":"{{fiat}}","offset":{{offset}},"limit":{{limit}}}'"""


def page(offset, total=5, size=2):
    items = [{"id": i, "price": str(95 + i / 10), "nickname": f"u{i}"} for i in range(offset, min(offset + size, total))]
    return {"ok": True, "data": {"items": items}}


@pytest.fixture
def template(tmp_path):
    path = tmp_path / "req.txt"
    path.write_text(TEMPLATE, encoding="utf-8")
    return path


def test_placeholders_side_mapping_and_pagination(template):
    def router(method, url, data=None, headers=None, **_):
        body = json.loads(data)
        return FakeResponse(page(body["offset"]))

    session = FakeSession(router)
    settings = CryptoBotSettings(
        request_file=str(template), side_values={"buy": "SALE", "sell": "PURCHASE"}, pages=10, page_size=2, delay=0
    )
    offers = CryptoBotClient(settings, session).fetch_offers("buy", "usdt", "rub")
    assert [o.offer_id for o in offers] == ["0", "1", "2", "3", "4"]
    first = json.loads(session.calls[0]["data"])
    assert first == {"type": "SALE", "asset": "USDT", "fiat": "RUB", "offset": 0, "limit": 2}
    headers = session.calls[0]["headers"]
    assert "content-length" not in {k.lower() for k in headers}
    assert headers["Accept-Encoding"] == "gzip, deflate"
    assert headers["authorization"].startswith("tma ")
    # 3 страницы с данными + одна пустая, после которой обход останавливается
    assert len(session.calls) == 4


def test_expired_auth(template):
    session = FakeSession(lambda *a, **k: FakeResponse({"error": "unauthorized"}, status_code=401))
    client = CryptoBotClient(CryptoBotSettings(request_file=str(template)), session)
    with pytest.raises(CryptoBotError, match="авторизация устарела"):
        client.fetch_offers("sell", "USDT", "RUB")


def test_validate_requires_side_placeholder(tmp_path):
    path = tmp_path / "req.txt"
    path.write_text("curl 'https://p2p.example.tg/offers?asset={{coin}}'", encoding="utf-8")
    client = CryptoBotClient(CryptoBotSettings(request_file=str(path)), FakeSession(lambda *a, **k: None))
    with pytest.raises(CryptoBotError, match="side"):
        client.validate(["buy", "sell"], ["USDT"], ["RUB"])
    with pytest.raises(CryptoBotError, match="fiat"):
        client.validate(["buy"], ["USDT"], ["RUB", "KZT"])
    client.validate(["buy"], ["USDT", "TON"], ["RUB"])


def test_separate_files_per_side(tmp_path):
    buy, sell = tmp_path / "buy.txt", tmp_path / "sell.txt"
    buy.write_text("curl 'https://p2p.example.tg/buy'", encoding="utf-8")
    sell.write_text("curl 'https://p2p.example.tg/sell'", encoding="utf-8")
    session = FakeSession(lambda m, url, **k: FakeResponse({"items": [{"price": "1" if "buy" in url else "2"}]}))
    settings = CryptoBotSettings(request_file=None, request_files={"buy": str(buy), "sell": str(sell)})
    client = CryptoBotClient(settings, session)
    client.validate(["buy", "sell"], ["USDT"], ["RUB"])
    assert client.fetch_offers("buy", "USDT", "RUB")[0].price == 1
    assert client.fetch_offers("sell", "USDT", "RUB")[0].price == 2


def test_missing_template_file(tmp_path):
    client = CryptoBotClient(CryptoBotSettings(request_file=str(tmp_path / "nope.txt")), FakeSession(lambda *a, **k: None))
    with pytest.raises(CryptoBotError, match="Copy as cURL"):
        client.fetch_offers("buy", "USDT", "RUB")
