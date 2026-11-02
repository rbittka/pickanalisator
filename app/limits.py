"""Ограничение частоты запросов в памяти процесса и определение IP клиента за прокси."""
from __future__ import annotations

import time
from collections import deque

from fastapi import Request

from . import config


class SlidingWindow:
    """Не больше `limit` событий за `window` секунд на ключ (IP или "" для общего лимита)."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = {}
        self._calls = 0

    def hit(self, key: str = "") -> bool:
        """Засчитывает событие; False — лимит исчерпан (событие не засчитано)."""
        now = time.monotonic()
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        self._calls += 1
        if self._calls % 1000 == 0:
            self._purge(now)
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True

    def _purge(self, now: float) -> None:
        # Без чистки словарь копил бы ключи всех когда-либо приходивших IP.
        stale = [k for k, q in self._hits.items() if not q or now - q[-1] > self.window]
        for k in stale:
            del self._hits[k]


def client_ip(request: Request) -> str:
    """IP клиента. За прокси (Render) берём адрес, который дописал сам прокси, —
    последние записи X-Forwarded-For; первые записи клиент может подставить сам."""
    hops = config.TRUSTED_PROXY_HOPS
    forwarded = request.headers.get("x-forwarded-for", "")
    if hops > 0 and forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if len(parts) >= hops:
            return parts[-hops]
    return request.client.host if request.client else "?"
