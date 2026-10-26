"""Разбор команды cURL, скопированной из DevTools («Copy as cURL»).

Поддерживаются форматы «Copy as cURL (bash)» и «Copy as cURL (cmd)» из Chrome/Edge/
Firefox. В любом месте команды (URL, заголовки, тело) можно вставить плейсхолдеры
``{{side}}``, ``{{coin}}``, ``{{fiat}}``, ``{{amount}}``, ``{{page}}``, ``{{offset}}``,
``{{limit}}`` — они подставляются перед каждым запросом.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

_DATA_FLAGS = {"-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode"}
# Флаги curl, у которых есть значение, но для повторения запроса оно не нужно.
_IGNORED_VALUE_FLAGS = {
    "-o", "--output", "-x", "--proxy", "-m", "--max-time", "--connect-timeout",
    "-u", "--user", "-w", "--write-out", "--retry", "-K", "--config",
}
_ANSI_C_RE = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.DOTALL)


@dataclass
class RequestSpec:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None

    @property
    def placeholders(self) -> set[str]:
        parts = [self.url, self.body or "", *self.headers.values()]
        return {m.group(1) for part in parts for m in PLACEHOLDER_RE.finditer(part)}

    def render(self, values: dict[str, object]) -> RequestSpec:
        def fill(text: str) -> str:
            def repl(m: re.Match[str]) -> str:
                name = m.group(1)
                return str(values[name]) if name in values else m.group(0)

            return PLACEHOLDER_RE.sub(repl, text)

        return RequestSpec(
            method=self.method,
            url=fill(self.url),
            headers={k: fill(v) for k, v in self.headers.items()},
            body=None if self.body is None else fill(self.body),
        )


def _decode_ansi_c(m: re.Match[str]) -> str:
    content = m.group(1)
    decoded = content.encode("latin-1", "backslashreplace").decode("unicode_escape")
    return shlex.quote(decoded)


def _normalize(text: str) -> str:
    text = text.strip()
    if re.search(r"\^\s*\r?\n", text) or '^"' in text:
        # Формат cmd.exe: ^ в конце строки — перенос, ^X — экранирование символа X.
        text = re.sub(r"\^\s*\r?\n", " ", text)
        text = re.sub(r"\^(.)", r"\1", text)
    text = re.sub(r"\\\r?\n", " ", text)
    return _ANSI_C_RE.sub(_decode_ansi_c, text)


def parse_curl(text: str) -> RequestSpec:
    tokens = shlex.split(_normalize(text))
    if not tokens or tokens[0] != "curl":
        raise ValueError("Ожидается команда, начинающаяся с 'curl' (DevTools → Copy as cURL)")

    method: str | None = None
    url: str | None = None
    headers: dict[str, str] = {}
    data: list[str] = []

    it = iter(tokens[1:])
    for tok in it:
        if tok in ("-X", "--request"):
            method = next(it, "GET").upper()
        elif tok.startswith("-X") and len(tok) > 2:
            method = tok[2:].upper()
        elif tok in ("-H", "--header"):
            name, sep, value = next(it, "").partition(":")
            if sep and name.strip():
                headers[name.strip()] = value.strip()
        elif tok in _DATA_FLAGS:
            data.append(next(it, ""))
        elif tok in ("-b", "--cookie"):
            headers["Cookie"] = next(it, "")
        elif tok in ("-A", "--user-agent"):
            headers["User-Agent"] = next(it, "")
        elif tok in ("-e", "--referer"):
            headers["Referer"] = next(it, "")
        elif tok == "--url":
            url = next(it, None)
        elif tok in _IGNORED_VALUE_FLAGS:
            next(it, None)
        elif tok.startswith("-"):
            continue  # булевы флаги: --compressed, -k, -s, -L и т.п.
        elif url is None:
            url = tok

    if not url:
        raise ValueError("В команде cURL не найден URL")
    body = "&".join(data) if data else None
    return RequestSpec(method=method or ("POST" if body is not None else "GET"), url=url, headers=headers, body=body)
