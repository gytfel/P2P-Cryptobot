"""Вывод результатов: таблица в терминал, CSV/JSON, текст для Telegram."""

from __future__ import annotations

import csv
import html
import json
from pathlib import Path

from .analyzer import MarketSummary
from .models import BUY, SIDE_TITLES, Deal, Reference


def fmt_num(value: float | None, digits: int | None = None) -> str:
    if value is None:
        return "—"
    if digits is None:
        digits = 2 if abs(value) >= 10 else 4 if abs(value) >= 0.01 else 8
    return f"{value:,.{digits}f}".replace(",", " ")


def fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}%"


def _limits(deal: Deal) -> str:
    o = deal.offer
    if o.min_amount is None and o.max_amount is None:
        return "—"
    return f"{fmt_num(o.min_amount, 0)}–{fmt_num(o.max_amount, 0)}"


def _trader(deal: Deal) -> str:
    o = deal.offer
    parts = []
    if o.orders is not None:
        parts.append(str(o.orders))
    if o.completion is not None:
        parts.append(f"{o.completion:.0f}%")
    return "/".join(parts) or "—"


def _cut(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def side_title(side: str, coin: str, fiat: str) -> str:
    verb = "покупаю" if side == BUY else "продаю"
    return f"{SIDE_TITLES[side]} {coin} за {fiat} (я {verb})"


def reference_lines(ref: Reference) -> list[str]:
    head = f"Рыночный курс {ref.coin}/{ref.fiat}: {fmt_num(ref.value)} {ref.fiat}"
    if ref.manual:
        return [head + " (задан вручную)"]
    lines = [head]
    by_pair: dict[str, list[str]] = {}
    for q in ref.quotes:
        mark = fmt_num(q.value) + (" ✓" if q.used else " ✗") if q.value else f"ошибка ({q.error})"
        by_pair.setdefault(q.pair, []).append(f"{q.source} {mark}")
    for pair, items in by_pair.items():
        lines.append(f"  {pair}: " + " | ".join(items))
    if ref.coin != "USDT" and ref.fiat != "USDT":
        lines.append(f"  = {fmt_num(ref.coin_usdt)} USDT × {fmt_num(ref.usdt_fiat)} {ref.fiat}")
    return lines


def summary_line(s: MarketSummary, fiat: str) -> str:
    if not s.count:
        extra = f" (отброшено как мусор: {s.dropped_outliers})" if s.dropped_outliers else ""
        return f"CryptoBot: объявлений нет{extra}"
    best = "дешевле всех" if s.side == BUY else "дороже всех"
    line = (
        f"CryptoBot: {s.count} объявл., медиана {fmt_num(s.median_price)} {fiat} "
        f"({fmt_pct(s.median_vs_market_pct)} к рынку), {best}: {fmt_num(s.best_price)}"
    )
    if s.dropped_outliers:
        line += f", отброшено выбросов: {s.dropped_outliers}"
    return line


def deals_table(deals: list[Deal], top: int, with_amount: bool) -> str:
    if not deals:
        return "  Подходящих объявлений нет."
    header = ["#", "Мерчант", "Цена", "С комисс.", "Выгода", "К медиане", "Лимиты", "Доступно", "Сделки", "Оплата"]
    if with_amount:
        header.insert(6, "Прибыль")
    rows = []
    for i, d in enumerate(deals[:top], 1):
        row = [
            str(i),
            _cut(d.offer.merchant, 18),
            fmt_num(d.offer.price),
            fmt_num(d.effective_price),
            fmt_pct(d.profit_pct),
            fmt_pct(d.vs_cryptobot_pct),
            _limits(d),
            fmt_num(d.offer.available),
            _trader(d),
            _cut(", ".join(d.offer.payment_methods) or "—", 32),
        ]
        if with_amount:
            row.insert(6, fmt_num(d.profit_fiat, 0) if d.profit_fiat is not None else "—")
        rows.append(row)
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    left = {1, len(header) - 1}  # мерчант и оплата — по левому краю, числа — по правому

    def line(cells: list[str]) -> str:
        cells = [c.ljust(w) if i in left else c.rjust(w) for i, (c, w) in enumerate(zip(cells, widths))]
        return ("  " + "  ".join(cells)).rstrip()

    sep = "  " + "  ".join("-" * w for w in widths)
    return "\n".join([line(header), sep, *map(line, rows)])


def write_json(path: str | Path, deals: list[Deal]) -> None:
    Path(path).write_text(json.dumps([d.to_dict() for d in deals], ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: str | Path, deals: list[Deal]) -> None:
    rows = [d.to_dict() for d in deals]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        if not rows:
            return
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter=";")
        writer.writeheader()
        for row in rows:
            row["payment_methods"] = ", ".join(row["payment_methods"])
            writer.writerow(row)


def telegram_message(deals: list[Deal], ref: Reference) -> str:
    """HTML-сообщение для Telegram Bot API об одной или нескольких выгодных сделках."""
    lines = []
    for d in deals:
        o = d.offer
        lines.append(
            f"<b>{html.escape(SIDE_TITLES[o.side])} {o.coin}/{o.fiat}</b> {fmt_pct(d.profit_pct)} к рынку\n"
            f"Цена {fmt_num(o.price)} (с комиссией {fmt_num(d.effective_price)}), рынок {fmt_num(ref.value)}\n"
            f"Мерчант: {html.escape(o.merchant)} · сделок {_trader(d)}\n"
            f"Лимиты: {_limits(d)} {o.fiat} · {html.escape(', '.join(o.payment_methods) or '—')}"
            + (f"\nПрибыль на сумму: {fmt_num(d.profit_fiat, 0)} {o.fiat}" if d.profit_fiat is not None else "")
        )
    return "\n\n".join(lines)
