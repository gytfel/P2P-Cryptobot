import json

import pytest

from p2p_scanner.curl import parse_curl

BASH = r"""curl 'https://p2p.example.tg/api/v1/offers?asset={{coin}}' \
  -H 'accept: application/json' \
  -H 'authorization: Bearer abc.def' \
  -H 'content-type: application/json' \
  -b 'session=xyz' \
  --data-raw $'{"side":"{{side}}","fiat":"{{fiat}}","note":"Привет it\'s"}' \
  --compressed"""

CMD = (
    'curl ^"https://p2p.example.tg/api/v1/offers^" ^\n'
    '  -H ^"authorization: Bearer abc^" ^\n'
    '  -H ^"content-type: application/json^" ^\n'
    '  --data-raw ^"^{^\\^"side^\\^":^\\^"{{side}}^\\^",^\\^"limit^\\^":50^}^"'
)


def test_bash_format():
    spec = parse_curl(BASH)
    assert spec.method == "POST"
    assert spec.url == "https://p2p.example.tg/api/v1/offers?asset={{coin}}"
    assert spec.headers["authorization"] == "Bearer abc.def"
    assert spec.headers["Cookie"] == "session=xyz"
    body = json.loads(spec.body)
    assert body["note"] == "Привет it's"
    assert spec.placeholders == {"coin", "side", "fiat"}


def test_cmd_format():
    spec = parse_curl(CMD)
    assert spec.url == "https://p2p.example.tg/api/v1/offers"
    assert spec.headers["authorization"] == "Bearer abc"
    assert json.loads(spec.body) == {"side": "{{side}}", "limit": 50}


def test_get_by_default_and_explicit_method():
    assert parse_curl("curl https://x.tg/a").method == "GET"
    assert parse_curl("curl -X PUT https://x.tg/a -d 'q=1'").method == "PUT"
    assert parse_curl("curl -XPATCH https://x.tg/a").method == "PATCH"


def test_render_fills_known_placeholders_only():
    spec = parse_curl(BASH).render({"coin": "USDT", "side": "SALE"})
    assert spec.url.endswith("asset=USDT")
    body = json.loads(spec.body)
    assert body["side"] == "SALE"
    assert body["fiat"] == "{{fiat}}"


def test_not_curl():
    with pytest.raises(ValueError):
        parse_curl("wget https://x.tg")
