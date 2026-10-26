import json

from conftest import FakeResponse, FakeSession
from p2p_scanner import cli
from p2p_scanner.models import BUY, Deal, Offer, Reference
from p2p_scanner.notify import TelegramNotifier


def run_demo(examples_dir, *extra):
    return cli.main([
        "scan", "--rate", "95.5", "--amount", "20000",
        "--offers-json", f"buy={examples_dir / 'sample_buy.json'}",
        "--offers-json", f"sell={examples_dir / 'sample_sell.json'}",
        *extra,
    ])


def test_offline_scan(examples_dir, capsys, tmp_path):
    out_json = tmp_path / "deals.json"
    out_csv = tmp_path / "deals.csv"
    assert run_demo(examples_dir, "--json", str(out_json), "--csv", str(out_csv)) == 0
    out = capsys.readouterr().out
    assert "Рыночный курс USDT/RUB: 95.50 RUB (задан вручную)" in out
    assert "ivan_p2p" in out
    assert "typo_price" not in out  # цена 9.65 отброшена как мусор
    assert "whale_otc" not in out  # минимум 100 000 ₽ больше суммы сделки
    deals = json.loads(out_json.read_text(encoding="utf-8"))
    best_buy = next(d for d in deals if d["side"] == "buy")
    assert best_buy["merchant"] == "ivan_p2p" and best_buy["profit_pct"] > 0
    assert "merchant" in out_csv.read_text(encoding="utf-8-sig").splitlines()[0]


def test_min_profit_filter(examples_dir, capsys):
    assert run_demo(examples_dir, "--min-profit", "0") == 0
    out = capsys.readouterr().out
    assert "ivan_p2p" in out and "fast_usdt" not in out


def test_default_command_is_scan(examples_dir, capsys):
    rc = cli.main(["--rate", "95.5", "--side", "buy", "--offers-json", f"buy={examples_dir / 'sample_buy.json'}"])
    assert rc == 0
    assert "ПОКУПКА USDT за RUB" in capsys.readouterr().out


def test_inspect_file(examples_dir, capsys):
    assert cli.main(["inspect", "--file", str(examples_dir / "sample_sell.json")]) == 0
    out = capsys.readouterr().out
    assert "'data.offers', элементов: 4" in out
    assert "мерчант=buyer_one" in out


def test_config_file(tmp_path, examples_dir, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[scan]\ncoins = ["usdt"]\nfiats = ["rub"]\nsides = ["sell"]\ntop = 1\n'
        "[fees]\nsell_pct = 0\n",
        encoding="utf-8",
    )
    rc = cli.main(["scan", "-c", str(cfg), "--rate", "95", "--offers-json", f"sell={examples_dir / 'sample_sell.json'}"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "rich_buyer" in out and "mid_buyer" not in out  # top = 1
    assert "+1.37%" in out  # 96.30 против 95 без комиссии


def test_bad_config_key(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[fees]\nsel_pct = 1\n", encoding="utf-8")
    assert cli.main(["rates", "-c", str(cfg)]) == 2
    assert "sel_pct" in capsys.readouterr().err


def test_notifier_dedup():
    sent = []
    session = FakeSession(lambda m, url, json=None, **k: sent.append(json) or FakeResponse({"ok": True}))
    notifier = TelegramNotifier("T", "42", session)
    ref = Reference("USDT", "RUB", 95.0, 1.0, 95.0)
    deal = Deal(Offer(BUY, "USDT", "RUB", 94.0, merchant="<b>x</b>", offer_id="7"), 95.0, 94.0, 1.05)
    assert notifier.notify_new([deal], ref) == 1
    assert notifier.notify_new([deal], ref) == 0
    assert len(sent) == 1
    assert sent[0]["chat_id"] == "42"
    assert "&lt;b&gt;x&lt;/b&gt;" in sent[0]["text"]


def test_online_scan_end_to_end(tmp_path, examples_dir, monkeypatch, capsys):
    from test_rates import market_router

    from p2p_scanner import cryptobot, rates

    samples = {
        "SALE": json.loads((examples_dir / "sample_buy.json").read_text(encoding="utf-8")),
        "PURCHASE": json.loads((examples_dir / "sample_sell.json").read_text(encoding="utf-8")),
    }

    def router(method, url, **kwargs):
        if "p2p.example.tg" in url:
            return FakeResponse(samples[json.loads(kwargs["data"])["type"]])
        return market_router(method, url, **kwargs)

    session = FakeSession(router)
    monkeypatch.setattr(rates, "make_session", lambda: session)
    monkeypatch.setattr(cryptobot, "make_session", lambda: session)

    (tmp_path / "req.txt").write_text(
        "curl 'https://p2p.example.tg/offers' --data-raw '{\"type\":\"{{side}}\",\"asset\":\"{{coin}}\"}'",
        encoding="utf-8",
    )
    (tmp_path / "config.toml").write_text(
        '[cryptobot]\nrequest_file = "req.txt"\nside_values = { buy = "SALE", sell = "PURCHASE" }\n',
        encoding="utf-8",
    )
    assert cli.main(["scan", "-c", str(tmp_path / "config.toml"), "--amount", "20000"]) == 0
    out = capsys.readouterr().out
    assert "Рыночный курс USDT/RUB: 95.00 RUB" in out
    assert "rapira 95.00 ✓" in out
    assert "ПОКУПКА USDT за RUB" in out and "ПРОДАЖА USDT за RUB" in out
    assert "ivan_p2p" in out and "rich_buyer" in out
    sent_types = [json.loads(c["data"])["type"] for c in session.calls if "p2p.example.tg" in c["url"]]
    assert sent_types == ["SALE", "PURCHASE"]
