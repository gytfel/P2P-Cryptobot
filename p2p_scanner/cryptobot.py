"""Получение объявлений P2P-маркета CryptoBot.

Официального API для объявлений у CryptoBot нет: P2P-маркет работает внутри мини-приложения
(t.me/send/app?startapp=p2p), а оно ходит на свой бэкенд с авторизацией Telegram.
Поэтому парсер повторяет тот же запрос, что делает мини-приложение: вы один раз копируете
его из DevTools («Copy as cURL») в файл, заменяете монету/фиат/сторону на плейсхолдеры —
и дальше запрос отправляется с нужными параметрами.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from .curl import RequestSpec, parse_curl
from .extract import extract_offers
from .models import SIDES, Offer
from .rates import make_session

_DROP_HEADERS = {"content-length", "host", "accept-encoding", "connection"}


@dataclass
class CryptoBotSettings:
    # Один шаблон с плейсхолдерами {{side}}, {{coin}}, {{fiat}}...
    request_file: str | None = "cryptobot_request.txt"
    # ...или отдельные файлы для каждой стороны: {"buy": "...", "sell": "..."}.
    request_files: dict[str, str] = field(default_factory=dict)
    # Как сторона называется в API мини-приложения (смотрите, что оно шлёт на вкладках «Купить»/«Продать»).
    side_values: dict[str, str] = field(default_factory=lambda: {"buy": "buy", "sell": "sell"})
    pages: int = 1
    page_size: int = 50
    first_page: int = 1
    timeout: float = 15.0
    delay: float = 0.4
    # Ручная привязка полей ответа (dot-путь): list, price, min_amount, max_amount, available,
    # merchant, orders, completion, payment_methods, id.
    fields: dict[str, str] = field(default_factory=dict)


class CryptoBotError(RuntimeError):
    pass


def load_template(path: str | Path) -> RequestSpec:
    p = Path(path)
    if not p.exists():
        raise CryptoBotError(
            f"Не найден файл запроса {p}. Скопируйте запрос мини-приложения CryptoBot "
            "из DevTools (Copy as cURL) — инструкция в README.md"
        )
    return parse_curl(p.read_text(encoding="utf-8"))


class CryptoBotClient:
    def __init__(self, settings: CryptoBotSettings, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or make_session()
        self._templates: dict[str, RequestSpec] = {}

    def template(self, side: str) -> RequestSpec:
        if side not in self._templates:
            path = self.settings.request_files.get(side) or self.settings.request_file
            if not path:
                raise CryptoBotError(f"Не задан файл запроса для стороны '{side}'")
            self._templates[side] = load_template(path)
        return self._templates[side]

    def validate(self, sides: list[str], coins: list[str], fiats: list[str]) -> None:
        """Проверяет, что шаблон умеет менять то, что меняется между запросами."""
        for side in sides:
            names = self.template(side).placeholders
            shared = side not in self.settings.request_files
            if shared and len(sides) > 1 and "side" not in names:
                raise CryptoBotError(
                    "Сканируются обе стороны, но в шаблоне нет {{side}}. Вставьте {{side}} вместо "
                    "типа объявления или задайте отдельные файлы request_files.buy / request_files.sell"
                )
            if len(coins) > 1 and not names & {"coin", "coin_lower"}:
                raise CryptoBotError("Несколько монет, но в шаблоне нет плейсхолдера {{coin}}")
            if len(fiats) > 1 and not names & {"fiat", "fiat_lower"}:
                raise CryptoBotError("Несколько фиатов, но в шаблоне нет плейсхолдера {{fiat}}")

    def _values(self, side: str, coin: str, fiat: str, amount: float | None, page: int) -> dict[str, object]:
        s = self.settings
        side_value = s.side_values.get(side, side)
        return {
            "side": side_value,
            "side_lower": str(side_value).lower(),
            "side_upper": str(side_value).upper(),
            "coin": coin.upper(),
            "coin_lower": coin.lower(),
            "fiat": fiat.upper(),
            "fiat_lower": fiat.lower(),
            "amount": "" if amount is None else f"{amount:g}",
            "page": s.first_page + page,
            "offset": page * s.page_size,
            "limit": s.page_size,
        }

    def _send(self, spec: RequestSpec) -> Any:
        headers = {k: v for k, v in spec.headers.items() if k.lower() not in _DROP_HEADERS}
        headers["Accept-Encoding"] = "gzip, deflate"
        try:
            resp = self.session.request(
                spec.method,
                spec.url,
                headers=headers,
                data=None if spec.body is None else spec.body.encode("utf-8"),
                timeout=self.settings.timeout,
            )
        except requests.RequestException as exc:
            raise CryptoBotError(f"Запрос к CryptoBot не удался: {exc}") from exc
        if resp.status_code in (401, 403):
            raise CryptoBotError(
                f"CryptoBot ответил {resp.status_code}: авторизация устарела. "
                "Откройте P2P-маркет заново и скопируйте свежий запрос (Copy as cURL)"
            )
        if resp.status_code >= 400:
            raise CryptoBotError(f"CryptoBot ответил {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise CryptoBotError(f"Ответ CryptoBot не JSON: {resp.text[:200]}") from exc

    def fetch_raw(self, side: str, coin: str, fiat: str, amount: float | None = None, page: int = 0) -> Any:
        spec = self.template(side).render(self._values(side, coin, fiat, amount, page))
        return self._send(spec)

    def fetch_offers(self, side: str, coin: str, fiat: str, amount: float | None = None) -> list[Offer]:
        if side not in SIDES:
            raise ValueError(f"Неизвестная сторона: {side}")
        paged = bool(self.template(side).placeholders & {"page", "offset"})
        pages = max(1, self.settings.pages) if paged else 1
        offers: dict[str, Offer] = {}
        for page in range(pages):
            if page:
                time.sleep(self.settings.delay)
            data = self.fetch_raw(side, coin, fiat, amount, page)
            batch = extract_offers(data, side, coin.upper(), fiat.upper(), self.settings.fields)
            new = [o for o in batch if o.key not in offers]
            offers.update((o.key, o) for o in new)
            if not new:
                break
        return list(offers.values())


def load_offers_file(path: str | Path, side: str, coin: str, fiat: str, fields: dict[str, str] | None = None) -> list[Offer]:
    """Объявления из сохранённого JSON-ответа (DevTools → Response → Save)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return extract_offers(data, side, coin.upper(), fiat.upper(), fields)
