"""Загрузка настроек из config.toml (все поля необязательные) и переменных окружения."""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

from .analyzer import AnalyzerSettings
from .cryptobot import CryptoBotSettings
from .models import SIDES
from .rates import RateSettings

DEFAULT_CONFIG = "config.toml"


@dataclass
class Config:
    coins: list[str] = field(default_factory=lambda: ["USDT"])
    fiats: list[str] = field(default_factory=lambda: ["RUB"])
    sides: list[str] = field(default_factory=lambda: list(SIDES))
    analyzer: AnalyzerSettings = field(default_factory=AnalyzerSettings)
    rates: RateSettings = field(default_factory=RateSettings)
    cryptobot: CryptoBotSettings = field(default_factory=CryptoBotSettings)
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    alert_pct: float = 0.5
    watch_interval: float = 60.0


def _apply(target: Any, values: dict[str, Any], section: str, aliases: dict[str, str] | None = None) -> None:
    names = {f.name for f in dataclasses.fields(target)}
    for key, value in values.items():
        attr = (aliases or {}).get(key, key)
        if attr not in names:
            raise ValueError(f"Неизвестный параметр [{section}] {key}")
        setattr(target, attr, value)


def load_config(path: str | Path | None = None) -> Config:
    cfg = Config()
    if path is None and Path(DEFAULT_CONFIG).exists():
        path = DEFAULT_CONFIG
    if path is not None:
        path = Path(path)
        with path.open("rb") as f:
            data = tomllib.load(f)
        base = path.resolve().parent

        scan = dict(data.get("scan", {}))
        for key in ("coins", "fiats", "sides"):
            if key in scan:
                setattr(cfg, key, [str(v) for v in scan.pop(key)])
        _apply(cfg.analyzer, scan, "scan")
        _apply(cfg.analyzer, data.get("fees", {}), "fees", {"buy_pct": "buy_fee_pct", "sell_pct": "sell_fee_pct"})
        _apply(cfg.analyzer, data.get("filters", {}), "filters")
        _apply(cfg.rates, data.get("rates", {}), "rates")

        cb = dict(data.get("cryptobot", {}))
        if cb.get("request_file"):
            cb["request_file"] = str(base / cb["request_file"])
        if cb.get("request_files"):
            cb["request_files"] = {side: str(base / p) for side, p in cb["request_files"].items()}
        _apply(cfg.cryptobot, cb, "cryptobot")

        tg = data.get("telegram", {})
        cfg.telegram_token = tg.get("bot_token") or None
        cfg.telegram_chat_id = str(tg["chat_id"]) if tg.get("chat_id") else None
        cfg.alert_pct = float(tg.get("alert_pct", cfg.alert_pct))
        cfg.watch_interval = float(data.get("watch", {}).get("interval", cfg.watch_interval))

    cfg.rates.crypto_pay_token = os.environ.get("CRYPTO_PAY_TOKEN") or cfg.rates.crypto_pay_token or None
    cfg.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.telegram_token
    cfg.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID") or cfg.telegram_chat_id

    cfg.coins = [c.upper() for c in cfg.coins]
    cfg.fiats = [f.upper() for f in cfg.fiats]
    bad = [s for s in cfg.sides if s not in SIDES]
    if bad:
        raise ValueError(f"Неизвестная сторона {bad}: допустимо {list(SIDES)}")
    return cfg
