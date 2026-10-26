"""Командная строка: python -m p2p_scanner [scan|rates|inspect] ..."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from . import report
from .analyzer import evaluate, internal_spread, summarize
from .config import Config, load_config
from .cryptobot import CryptoBotClient, CryptoBotError, load_offers_file
from .extract import find_offer_list, locate_offer_list, parse_offer
from .models import BUY, SELL, SIDES, Deal, Reference
from .notify import TelegramNotifier
from .rates import RateProvider, manual_reference


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _sides(value: str) -> list[str]:
    return list(SIDES) if value == "both" else [value]


def _offline_files(items: list[str]) -> dict[str, str]:
    files = {}
    for item in items:
        side, sep, path = item.partition("=")
        if not sep or side not in SIDES:
            raise SystemExit(f"--offers-json ожидает buy=ФАЙЛ или sell=ФАЙЛ, получено: {item}")
        files[side] = path
    return files


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m p2p_scanner",
        description="Поиск выгодных P2P-сделок в CryptoBot относительно реального рыночного курса",
    )
    sub = parser.add_subparsers(dest="command")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", help="путь к config.toml (по умолчанию ./config.toml, если есть)")
    common.add_argument("--coin", type=_csv, help="монеты через запятую, напр. USDT,TON")
    common.add_argument("--fiat", type=_csv, help="фиаты через запятую, напр. RUB")

    scan = sub.add_parser("scan", parents=[common], help="найти выгодные объявления (по умолчанию)")
    scan.add_argument("--side", choices=["buy", "sell", "both"], help="buy — я покупаю, sell — я продаю")
    scan.add_argument("--amount", type=float, help="сумма сделки в фиате: фильтр по лимитам и расчёт прибыли")
    scan.add_argument("--top", type=int, help="сколько лучших объявлений показывать")
    scan.add_argument("--min-profit", type=float, help="только сделки с выгодой не ниже, %% (можно < 0)")
    scan.add_argument("--rate", type=float, help="задать рыночный курс вручную вместо бирж")
    scan.add_argument("--buy-fee", type=float, help="комиссия при покупке, %%")
    scan.add_argument("--sell-fee", type=float, help="комиссия при продаже, %%")
    scan.add_argument("--pay", type=_csv, help="только эти способы оплаты (подстрока), через запятую")
    scan.add_argument("--min-orders", type=int, help="минимум сделок у мерчанта")
    scan.add_argument("--min-completion", type=float, help="минимальный %% успешных сделок мерчанта")
    scan.add_argument(
        "--offers-json", action="append", default=[], metavar="SIDE=FILE",
        help="взять объявления из сохранённого JSON-ответа вместо запроса, напр. buy=resp.json",
    )
    scan.add_argument("--json", dest="json_out", help="сохранить все найденные сделки в JSON")
    scan.add_argument("--csv", dest="csv_out", help="сохранить все найденные сделки в CSV")
    scan.add_argument("--watch", type=float, nargs="?", const=-1, help="повторять каждые N секунд")
    scan.add_argument("--notify", action="store_true", help="слать выгодные сделки в Telegram")
    scan.add_argument("--alert-pct", type=float, help="порог выгоды для уведомления, %%")

    sub.add_parser("rates", parents=[common], help="показать рыночный курс и котировки по источникам")

    inspect = sub.add_parser("inspect", parents=[common], help="проверить, как разбирается ответ CryptoBot")
    inspect.add_argument("--side", choices=list(SIDES), default=BUY)
    inspect.add_argument("--file", help="разобрать сохранённый JSON вместо запроса")
    inspect.add_argument("--raw", action="store_true", help="показать сырой ответ")
    return parser


def _apply_args(cfg: Config, args: argparse.Namespace) -> None:
    if args.coin:
        cfg.coins = [c.upper() for c in args.coin]
    if args.fiat:
        cfg.fiats = [f.upper() for f in args.fiat]
    if getattr(args, "side", None) and args.command == "scan":
        cfg.sides = _sides(args.side)
    a = cfg.analyzer
    for arg, attr in (
        ("amount", "amount"), ("top", "top"), ("min_profit", "min_profit_pct"), ("buy_fee", "buy_fee_pct"),
        ("sell_fee", "sell_fee_pct"), ("pay", "payment_include"), ("min_orders", "min_orders"),
        ("min_completion", "min_completion"),
    ):
        value = getattr(args, arg, None)
        if value is not None:
            setattr(a, attr, value)
    if getattr(args, "alert_pct", None) is not None:
        cfg.alert_pct = args.alert_pct


def _reference(provider: RateProvider | None, coin: str, fiat: str, manual: float | None) -> Reference:
    if manual is not None:
        return manual_reference(coin, fiat, manual)
    assert provider is not None
    return provider.reference(coin, fiat)


def scan_once(
    cfg: Config,
    provider: RateProvider | None,
    client: CryptoBotClient | None,
    offline: dict[str, str],
    manual_rate: float | None,
    notifier: TelegramNotifier | None = None,
) -> tuple[list[Deal], int]:
    """Один проход по всем монетам/фиатам/сторонам. Возвращает сделки и число ошибок."""
    all_deals: list[Deal] = []
    errors = 0
    a = cfg.analyzer
    for coin in cfg.coins:
        for fiat in cfg.fiats:
            print(f"\n=== {coin}/{fiat} " + "=" * 50)
            try:
                ref = _reference(provider, coin, fiat, manual_rate)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {exc}")
                errors += 1
                continue
            print("\n".join(report.reference_lines(ref)))

            per_side: dict[str, list[Deal]] = {}
            for side in cfg.sides:
                print(f"\n{report.side_title(side, coin, fiat)}")
                try:
                    if side in offline:
                        offers = load_offers_file(offline[side], side, coin, fiat, cfg.cryptobot.fields)
                    else:
                        assert client is not None
                        offers = client.fetch_offers(side, coin, fiat, a.amount)
                except (CryptoBotError, ValueError, OSError) as exc:
                    print(f"  ! {exc}")
                    errors += 1
                    continue
                deals = evaluate(offers, ref.value, a)
                per_side[side] = deals
                print(report.summary_line(summarize(offers, ref.value, a, side), fiat))
                print(report.deals_table(deals, a.top, a.amount is not None))
                all_deals += deals

            spread = internal_spread(per_side.get(BUY, []), per_side.get(SELL, []))
            if spread is not None:
                verdict = "есть круг!" if spread > 0 else "круга нет"
                print(f"\nКупить у лучшего продавца → продать лучшему покупателю: {spread:+.2f}% ({verdict})")

            if notifier:
                hot = [d for side_deals in per_side.values() for d in side_deals if d.profit_pct >= cfg.alert_pct]
                try:
                    sent = notifier.notify_new(hot, ref)
                    if sent:
                        print(f"\nОтправлено в Telegram: {sent}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  ! Telegram: {exc}")
    return all_deals, errors


def cmd_scan(cfg: Config, args: argparse.Namespace) -> int:
    offline = _offline_files(args.offers_json)
    if args.rate is not None and (len(cfg.coins) > 1 or len(cfg.fiats) > 1):
        raise SystemExit("--rate можно задать только для одной пары монета/фиат")
    if args.rate is not None and args.rate <= 0:
        raise SystemExit("--rate должен быть больше нуля")
    provider = None if args.rate is not None else RateProvider(cfg.rates)
    online_sides = [s for s in cfg.sides if s not in offline]
    client = None
    if online_sides:
        client = CryptoBotClient(cfg.cryptobot)
        try:
            client.validate(online_sides, cfg.coins, cfg.fiats)
        except (CryptoBotError, ValueError) as exc:
            print(f"Ошибка шаблона запроса: {exc}", file=sys.stderr)
            return 2

    notifier = None
    if args.notify:
        if not (cfg.telegram_token and cfg.telegram_chat_id):
            raise SystemExit("Для --notify задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID (или [telegram] в config.toml)")
        notifier = TelegramNotifier(cfg.telegram_token, cfg.telegram_chat_id)

    interval = None if args.watch is None else (cfg.watch_interval if args.watch < 0 else args.watch)
    while True:
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{started}] Сканирование CryptoBot P2P")
        deals, errors = scan_once(cfg, provider, client, offline, args.rate, notifier)
        if args.json_out:
            report.write_json(args.json_out, deals)
        if args.csv_out:
            report.write_csv(args.csv_out, deals)
        if interval is None:
            return 1 if errors and not deals else 0
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            return 0


def cmd_rates(cfg: Config, args: argparse.Namespace) -> int:
    provider = RateProvider(cfg.rates)
    failed = 0
    for coin in cfg.coins:
        for fiat in cfg.fiats:
            try:
                print("\n".join(report.reference_lines(provider.reference(coin, fiat))))
            except RuntimeError as exc:
                print(f"! {exc}")
                failed += 1
            print()
    return 1 if failed else 0


def cmd_inspect(cfg: Config, args: argparse.Namespace) -> int:
    coin, fiat = cfg.coins[0], cfg.fiats[0]
    data: Any
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            data = json.load(f)
    else:
        client = CryptoBotClient(cfg.cryptobot)
        spec = client.template(args.side)
        print(f"Запрос: {spec.method} {spec.url}")
        print(f"Плейсхолдеры в шаблоне: {', '.join(sorted(spec.placeholders)) or 'нет'}")
        data = client.fetch_raw(args.side, coin, fiat, cfg.analyzer.amount)
    if args.raw:
        print(json.dumps(data, ensure_ascii=False, indent=2)[:5000])

    fields = dict(cfg.cryptobot.fields)
    list_path = fields.pop("list", None)
    if list_path:
        path, items = list_path, find_offer_list(data, list_path)
    else:
        path, items = locate_offer_list(data)
    if not items:
        print("Список объявлений не найден. Посмотрите ответ (--raw) и задайте [cryptobot.fields] list = \"...\"")
        return 1
    print(f"Список объявлений: '{path or '<корень>'}', элементов: {len(items)}")
    print(f"Ключи первого элемента: {', '.join(items[0].keys())}")
    print("\nКак разобраны первые объявления:")
    for item in items[:3]:
        offer = parse_offer(item, args.side, coin, fiat, fields)
        if offer is None:
            print("  — без цены, пропущено")
            continue
        print(
            f"  цена={offer.price} лимиты={offer.min_amount}–{offer.max_amount} доступно={offer.available} "
            f"мерчант={offer.merchant} сделок={offer.orders} успешных={offer.completion} "
            f"оплата={offer.payment_methods} id={offer.offer_id}"
        )
    print("\nЕсли поле разобрано неверно — задайте путь вручную в [cryptobot.fields] config.toml.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv.insert(0, "scan")
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as exc:
        print(f"Ошибка конфига: {exc}", file=sys.stderr)
        return 2
    _apply_args(cfg, args)
    try:
        handler = {"scan": cmd_scan, "rates": cmd_rates, "inspect": cmd_inspect}[args.command]
        return handler(cfg, args)
    except CryptoBotError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Ошибка настроек: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
