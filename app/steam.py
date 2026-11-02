"""Вход через Steam (OpenID 2.0) и статистика игрока из OpenDota.

Пароль игрок вводит на сайте Steam; мы получаем только его Steam ID. Ответ Steam,
пришедший в браузере, мог быть подделан, поэтому каждый вход перепроверяется
прямым запросом к Steam (check_authentication).
"""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlencode

import httpx

from .data import API, _redact

log = logging.getLogger(__name__)

STEAM_OPENID = "https://steamcommunity.com/openid/login"
CLAIMED_ID = re.compile(r"https://steamcommunity\.com/openid/id/(\d{17})")
STEAM64_BASE = 76561197960265728  # Steam ID 64 → account_id Dota (32 бита)
NS = "http://specs.openid.net/auth/2.0"
CACHE_TTL = 600
CACHE_MAX = 2000


def login_url(return_to: str, realm: str) -> str:
    return STEAM_OPENID + "?" + urlencode({
        "openid.ns": NS,
        "openid.mode": "checkid_setup",
        "openid.return_to": return_to,
        "openid.realm": realm,
        "openid.identity": f"{NS}/identifier_select",
        "openid.claimed_id": f"{NS}/identifier_select",
    })


async def verify(params: dict[str, str], return_to: str, client: httpx.AsyncClient | None = None) -> int | None:
    """account_id вошедшего игрока или None, если ответ не настоящий."""
    if params.get("openid.mode") != "id_res" or params.get("openid.op_endpoint") != STEAM_OPENID:
        return None
    # Ответ должен быть выдан именно нашему сайту, а не перенесён с чужого.
    if params.get("openid.return_to") != return_to:
        return None
    m = CLAIMED_ID.fullmatch(params.get("openid.claimed_id", ""))
    if not m or params.get("openid.identity") != params.get("openid.claimed_id"):
        return None
    check = {k: v for k, v in params.items() if k.startswith("openid.")}
    check["openid.mode"] = "check_authentication"
    try:
        async with (client or httpx.AsyncClient(timeout=15)) as c:
            r = await c.post(STEAM_OPENID, data=check)
            r.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("Steam OpenID недоступен: %s", type(e).__name__)
        return None
    if "is_valid:true" not in r.text.splitlines():
        return None
    account_id = int(m.group(1)) - STEAM64_BASE
    return account_id if 0 < account_id < 2**32 else None


class PlayerCache:
    """Профиль и герои игрока из OpenDota с кэшем на 10 минут (OpenDota лимитирует запросы)."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, int], tuple[float, object]] = {}

    async def _get(self, kind: str, account_id: int, url: str, params: dict | None = None):
        key = (kind, account_id)
        hit = self._data.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
        try:
            async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "dota-draft-site"}) as c:
                r = await c.get(url, params=params)
                r.raise_for_status()
                value = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("OpenDota игрок %s: %s: %s", kind, type(e).__name__, _redact(str(e)))
            return None
        if len(self._data) >= CACHE_MAX:
            self._data.clear()
        self._data[key] = (time.monotonic(), value)
        return value

    async def profile(self, account_id: int) -> dict:
        body = await self._get("profile", account_id, f"{API}/players/{account_id}") or {}
        p = body.get("profile") or {}
        avatar = p.get("avatarmedium") or ""
        return {
            "account_id": account_id,
            "name": str(p.get("personaname") or f"Игрок {account_id}")[:64],
            # Аватар только с CDN Steam: его разрешает CSP, чужие адреса не подставить.
            "avatar": avatar if avatar.startswith("https://avatars.steamstatic.com/") else "",
            "rank_tier": body.get("rank_tier") if isinstance(body.get("rank_tier"), int) else None,
            "found": bool(p),
        }

    async def heroes(self, account_id: int, days: int = 180) -> list[dict] | None:
        rows = await self._get("heroes", account_id, f"{API}/players/{account_id}/heroes", {"date": days})
        if rows is None:
            return None
        out = []
        for h in rows if isinstance(rows, list) else []:
            hid, games, wins = h.get("hero_id"), h.get("games"), h.get("win")
            if isinstance(hid, int) and isinstance(games, int) and isinstance(wins, int) and games > 0:
                out.append({"id": hid, "games": games, "wins": wins})
        return sorted(out, key=lambda x: x["games"], reverse=True)
