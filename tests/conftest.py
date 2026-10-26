from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class FakeResponse:
    def __init__(self, payload: Any = None, status_code: int = 200, text: str | None = None):
        self.payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)

    def json(self) -> Any:
        if self.payload is None:
            raise ValueError("not json")
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Подменяет requests.Session: ответ выбирается функцией-маршрутизатором по (method, url, kwargs)."""

    def __init__(self, router: Callable[..., FakeResponse]):
        self.router = router
        self.calls: list[dict[str, Any]] = []
        self.headers: dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.router(method, url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("POST", url, **kwargs)


@pytest.fixture
def examples_dir() -> Path:
    return ROOT / "examples"
