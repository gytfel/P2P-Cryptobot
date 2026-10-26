"""Уведомления о выгодных сделках через Telegram Bot API."""

from __future__ import annotations

import requests

from .models import Deal, Reference
from .report import telegram_message


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, session: requests.Session | None = None, timeout: float = 10.0):
        self.token = token
        self.chat_id = chat_id
        self.session = session or requests.Session()
        self.timeout = timeout
        self._sent: set[str] = set()

    def send(self, text: str) -> None:
        resp = self.session.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Telegram Bot API ответил {resp.status_code}: {resp.text[:200]}")

    def notify_new(self, deals: list[Deal], ref: Reference) -> int:
        """Отправляет только сделки, о которых ещё не сообщали. Возвращает их число."""
        fresh = [d for d in deals if d.offer.key not in self._sent]
        if not fresh:
            return 0
        for i in range(0, len(fresh), 5):  # не упираться в лимит 4096 символов на сообщение
            chunk = fresh[i : i + 5]
            self.send(telegram_message(chunk, ref))
            self._sent.update(d.offer.key for d in chunk)
        return len(fresh)
