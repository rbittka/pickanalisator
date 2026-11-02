"""Статистика героев из OpenDota: состав команд в публичных матчах высокого ранга.

Матчи берутся SQL-запросами к OpenDota Explorer диапазонами match_id (по первичному
ключу это быстро), хранятся на диске и раз в несколько часов догружаются. Из них
считаются винрейты героев, матчапы (герой против героя) и синергии (герой с героем).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import numpy as np

from . import config

log = logging.getLogger(__name__)

API = "https://api.opendota.com/api"
STEAM_CDN = "https://cdn.cloudflare.steamstatic.com"
# ~1 млн match_id — это примерно сутки матчей; один запрос возвращает 15–25 тыс. строк.
CHUNK_IDS = 500_000
MATCHES_FILE = "matches.json"
HEROES_FILE = "heroes.json"


@dataclass
class Hero:
    id: int
    name: str
    npc: str
    roles: list[str]
    positions: dict[int, float]  # доля игр героя на каждой позиции 1–5


@dataclass
class Stats:
    heroes: dict[int, Hero]
    games: np.ndarray  # [hero] игр
    wins: np.ndarray  # [hero] побед
    vs_games: np.ndarray  # [hero, other] игр друг против друга
    vs_wins: np.ndarray  # [hero, other] побед hero
    with_games: np.ndarray  # [hero, other] игр в одной команде
    with_wins: np.ndarray
    matches: int
    updated: float
    first_match_time: int = 0
    last_match_time: int = 0
    by_name: dict[str, Hero] = field(default_factory=dict)

    def winrate(self, hero_id: int) -> float:
        g = self.games[hero_id]
        return float(self.wins[hero_id] / g) if g else 0.5


def _client() -> httpx.AsyncClient:
    params = {"api_key": config.OPENDOTA_API_KEY} if config.OPENDOTA_API_KEY else None
    return httpx.AsyncClient(timeout=120, params=params, headers={"User-Agent": "dota-draft-site"})


async def _explorer(client: httpx.AsyncClient, sql: str) -> list[dict]:
    for attempt in range(4):
        try:
            r = await client.get(f"{API}/explorer", params={"sql": sql})
            r.raise_for_status()
            body = r.json()
            if body.get("err"):
                raise RuntimeError(body["err"])
            return body["rows"]
        except (httpx.HTTPError, RuntimeError) as e:
            if attempt == 3:
                raise
            log.warning("OpenDota explorer: %s, повтор", e)
            await asyncio.sleep(5 * (attempt + 1))
    return []


def _positions_table() -> dict[int, dict[int, float]]:
    raw = json.loads((config.RESOURCES_DIR / "positions.json").read_text())
    return {int(h): {int(p): s for p, s in shares.items()} for h, shares in raw["shares"].items()}


# Для героев, которых нет в таблице позиций (например, новых), прикидываем позиции по ролям.
def _positions_from_roles(roles: list[str]) -> dict[int, float]:
    if "Carry" in roles and "Support" not in roles:
        return {1: 0.5, 2: 0.25, 3: 0.25}
    if "Support" in roles:
        return {4: 0.5, 5: 0.5}
    return {2: 0.4, 3: 0.4, 4: 0.2}


async def fetch_heroes(client: httpx.AsyncClient) -> dict[int, Hero]:
    r = await client.get(f"{API}/heroStats")
    r.raise_for_status()
    table = _positions_table()
    heroes = {}
    for h in r.json():
        npc = h["name"].removeprefix("npc_dota_hero_")
        heroes[h["id"]] = Hero(
            id=h["id"],
            name=h["localized_name"],
            npc=npc,
            roles=h.get("roles") or [],
            positions=table.get(h["id"]) or _positions_from_roles(h.get("roles") or []),
        )
    await _ensure_portraits(client, heroes.values())
    return heroes


def portrait_dirs() -> list[Path]:
    return [config.STATIC_DIR / "heroes", config.DATA_DIR / "heroes"]


def portrait_path(npc: str) -> Path | None:
    for d in portrait_dirs():
        p = d / f"{npc}.jpg"
        if p.exists():
            return p
    return None


async def _ensure_portraits(client: httpx.AsyncClient, heroes) -> None:
    """Докачивает портреты новых героев (в репозитории лежат уже имеющиеся)."""
    from io import BytesIO

    from PIL import Image

    target = config.DATA_DIR / "heroes"
    for h in heroes:
        if portrait_path(h.npc):
            continue
        url = f"{STEAM_CDN}/apps/dota2/images/dota_react/heroes/{h.npc}.png"
        try:
            r = await client.get(url)
            r.raise_for_status()
            im = Image.open(BytesIO(r.content)).convert("RGB")
            im = im.resize((128, round(128 * im.height / im.width)), Image.Resampling.LANCZOS)
            target.mkdir(parents=True, exist_ok=True)
            im.save(target / f"{h.npc}.jpg", quality=80)
            log.info("Скачан портрет %s", h.npc)
        except Exception as e:  # портрет не критичен: без него герой просто не распознаётся
            log.warning("Нет портрета для %s: %s", h.npc, e)


async def fetch_matches(client: httpx.AsyncClient, known: list[list]) -> list[list]:
    """Догружает матчи за окно WINDOW_DAYS. Матч: [match_id, start_time, radiant_win, radiant, dire]."""
    cutoff = int(time.time()) - config.WINDOW_DAYS * 86400
    known = [m for m in known if m[1] >= cutoff]
    latest = (await _explorer(client, "select max(match_id) as id from public_matches"))[0]["id"]
    floor_id = max((m[0] for m in known), default=0)

    fresh: list[list] = []
    hi = latest
    # Предохранитель: суток матчей хватает на ~2 диапазона, берём с запасом.
    for _ in range(config.WINDOW_DAYS * 4 + 4):
        if hi <= floor_id:
            break
        lo = max(hi - CHUNK_IDS, floor_id)
        rows = await _explorer(
            client,
            f"""select match_id, start_time, radiant_win, radiant_team, dire_team
                from public_matches
                where match_id > {lo} and match_id <= {hi}
                  and avg_rank_tier >= {config.MIN_RANK}
                  and duration > 900 and lobby_type = 7 and game_mode in (1, 2, 22)""",
        )
        fresh += [
            [r["match_id"], r["start_time"], bool(r["radiant_win"]), r["radiant_team"], r["dire_team"]]
            for r in rows
            if len(r["radiant_team"] or []) == 5 and len(r["dire_team"] or []) == 5
            and 0 not in r["radiant_team"] and 0 not in r["dire_team"]
        ]
        log.info("OpenDota: %s–%s, +%d матчей", lo, hi, len(rows))
        hi = lo
        # Дошли до матчей старше окна: дальше смотреть незачем.
        if rows and min(r["start_time"] for r in rows) < cutoff:
            break
    return sorted((m for m in known + fresh if m[1] >= cutoff), key=lambda m: m[0])


def aggregate(heroes: dict[int, Hero], matches: list[list]) -> Stats:
    n = max(max(heroes, default=0), max((max(m[3] + m[4]) for m in matches), default=0)) + 1
    games = np.zeros(n, np.int64)
    wins = np.zeros(n, np.int64)
    vs_g = np.zeros((n, n), np.int64)
    vs_w = np.zeros((n, n), np.int64)
    w_g = np.zeros((n, n), np.int64)
    w_w = np.zeros((n, n), np.int64)
    if matches:
        rad = np.array([m[3] for m in matches], np.int64)
        dire = np.array([m[4] for m in matches], np.int64)
        rw = np.array([m[2] for m in matches], np.int64)
        for team, other, won in ((rad, dire, rw), (dire, rad, 1 - rw)):
            np.add.at(games, team.ravel(), 1)
            np.add.at(wins, team.ravel(), np.repeat(won, 5))
            # Каждый герой команды против каждого героя соперника.
            a = np.repeat(team, 5, axis=1).ravel()
            b = np.tile(other, (1, 5)).ravel()
            np.add.at(vs_g, (a, b), 1)
            np.add.at(vs_w, (a, b), np.repeat(won, 25))
            # Пары внутри команды (кроме самого себя).
            a = np.repeat(team, 5, axis=1).ravel()
            b = np.tile(team, (1, 5)).ravel()
            keep = a != b
            np.add.at(w_g, (a[keep], b[keep]), 1)
            np.add.at(w_w, (a[keep], b[keep]), np.repeat(won, 25)[keep])
    times = [m[1] for m in matches]
    return Stats(
        heroes=heroes, games=games, wins=wins, vs_games=vs_g, vs_wins=vs_w,
        with_games=w_g, with_wins=w_w, matches=len(matches), updated=time.time(),
        first_match_time=min(times, default=0), last_match_time=max(times, default=0),
        by_name={h.name.lower(): h for h in heroes.values()},
    )


class Store:
    """Текущая статистика плюс фоновое обновление."""

    def __init__(self) -> None:
        self.stats: Stats | None = None
        self.error: str | None = None
        self._lock = asyncio.Lock()

    def _load_cache(self) -> tuple[dict[int, Hero], list[list]] | None:
        hf, mf = config.DATA_DIR / HEROES_FILE, config.DATA_DIR / MATCHES_FILE
        if not (hf.exists() and mf.exists()):
            return None
        heroes = {int(k): Hero(**{**v, "positions": {int(p): s for p, s in v["positions"].items()}})
                  for k, v in json.loads(hf.read_text()).items()}
        return heroes, json.loads(mf.read_text())

    def _save_cache(self, heroes: dict[int, Hero], matches: list[list]) -> None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (config.DATA_DIR / HEROES_FILE).write_text(json.dumps({k: vars(h) for k, h in heroes.items()}))
        tmp = config.DATA_DIR / (MATCHES_FILE + ".tmp")
        tmp.write_text(json.dumps(matches, separators=(",", ":")))
        tmp.replace(config.DATA_DIR / MATCHES_FILE)

    def load_from_disk(self) -> None:
        cached = self._load_cache()
        if cached:
            self.stats = aggregate(*cached)
            self.stats.updated = (config.DATA_DIR / MATCHES_FILE).stat().st_mtime
            log.info("Кэш: %d матчей", self.stats.matches)

    async def refresh(self) -> None:
        async with self._lock:
            try:
                cached = self._load_cache()
                async with _client() as client:
                    heroes = await fetch_heroes(client)
                    matches = await fetch_matches(client, cached[1] if cached else [])
                stats = await asyncio.to_thread(aggregate, heroes, matches)
                await asyncio.to_thread(self._save_cache, heroes, matches)
                self.stats, self.error = stats, None
                log.info("Статистика обновлена: %d матчей", stats.matches)
            except Exception as e:
                self.error = str(e)
                log.exception("Не удалось обновить статистику")

    async def run_forever(self) -> None:
        while True:
            fresh = self.stats and time.time() - self.stats.updated < config.REFRESH_HOURS * 3600
            if not fresh:
                await self.refresh()
            await asyncio.sleep(600)
