"""Статистика героев из OpenDota: состав команд в публичных матчах высокого ранга.

Матчи берутся SQL-запросами к OpenDota Explorer диапазонами match_id (по первичному
ключу это быстро), хранятся на диске и раз в несколько часов догружаются. Из них
считаются винрейты героев, матчапы (герой против героя) и синергии (герой с героем).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import numpy as np

from . import config

log = logging.getLogger(__name__)
# httpx пишет в INFO каждый запрос с полным URL, а в URL — ключ OpenDota.
logging.getLogger("httpx").setLevel(logging.WARNING)

API = "https://api.opendota.com/api"
STEAM_CDN = "https://cdn.cloudflare.steamstatic.com"
# ~1 млн match_id — это примерно сутки матчей; один запрос возвращает 15–25 тыс. строк.
CHUNK_IDS = 500_000
MATCHES_FILE = "matches.json"
HEROES_FILE = "heroes.json"
MAX_HERO_ID = 1000  # id героев в Dota сейчас до ~150; от него зависит размер матриц n×n
MAX_PORTRAIT_BYTES = 2 * 1024 * 1024
MAX_PORTRAIT_SIDE = 2000
PUBLIC_ERROR = "Источник статистики временно недоступен"
PROFILE_FILE = "profiles.json"
# Фазы игры по длительности матча: ранняя (до 30 мин), средняя (30–45), поздняя (45+).
PHASE_EDGES = (30 * 60, 45 * 60)
# Средние за игру по разобранным матчам: контроль, строения, урон, лечение, Рошан.
PROFILE_FIELDS = ("stuns", "tower_damage", "hero_damage", "hero_healing", "roshans_killed")
_NPC = re.compile(r"[a-z0-9_]{1,64}")
_API_KEY = re.compile(r"api_key=[^&\s\"']+")


def _redact(text: str) -> str:
    """Убирает ключ OpenDota из текста ошибки: httpx кладёт в неё полный URL запроса."""
    return _API_KEY.sub("api_key=***", text)


def _valid_hero_id(i) -> bool:
    return isinstance(i, int) and not isinstance(i, bool) and 0 < i < MAX_HERO_ID


def _valid_hero(h: "Hero") -> bool:
    ok = _valid_hero_id(h.id) and isinstance(h.npc, str) and bool(_NPC.fullmatch(h.npc))
    if not ok:
        log.warning("Пропущен герой с некорректными данными: id=%r npc=%r", h.id, h.npc)
    return ok


def _valid_match(m) -> bool:
    """Матч из OpenDota или кэша: [match_id, start_time, radiant_win, radiant, dire, duration]."""
    try:
        teams = (m[3], m[4])
        if not isinstance(m[5], int):
            return False
        return all(isinstance(t, list) and len(t) == 5 and all(_valid_hero_id(i) for i in t) for t in teams)
    except (TypeError, IndexError, KeyError):
        return False


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
    phase_games: np.ndarray | None = None  # [hero, фаза] игр по длительности матча
    phase_wins: np.ndarray | None = None
    profile: dict[int, dict] = field(default_factory=dict)  # средние за игру, см. PROFILE_FIELDS

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
            log.warning("OpenDota explorer: %s: %s, повтор", type(e).__name__, _redact(str(e)))
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
        npc = str(h.get("name", "")).removeprefix("npc_dota_hero_")
        hero = Hero(
            id=h.get("id"),
            name=str(h.get("localized_name", "")),
            npc=npc,
            roles=h.get("roles") or [],
            positions=table.get(h.get("id")) or _positions_from_roles(h.get("roles") or []),
        )
        # npc становится путём к файлу и адресом картинки: пропускаем всё подозрительное.
        if _valid_hero(hero):
            heroes[hero.id] = hero
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


def _save_portrait(content: bytes, path: Path) -> None:
    from io import BytesIO

    from PIL import Image

    im = Image.open(BytesIO(content))
    # Размер виден по заголовку до распаковки пикселей: отсекаем «картинки-бомбы».
    if im.width > MAX_PORTRAIT_SIDE or im.height > MAX_PORTRAIT_SIDE or not im.width or not im.height:
        raise ValueError(f"неожиданный размер {im.width}×{im.height}")
    im = im.convert("RGB")
    im = im.resize((128, max(1, round(128 * im.height / im.width))), Image.Resampling.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, quality=80)


async def _ensure_portraits(client: httpx.AsyncClient, heroes) -> None:
    """Докачивает портреты новых героев (в репозитории лежат уже имеющиеся)."""
    target = config.DATA_DIR / "heroes"
    for h in heroes:
        if not _NPC.fullmatch(h.npc) or portrait_path(h.npc):
            continue
        url = f"{STEAM_CDN}/apps/dota2/images/dota_react/heroes/{h.npc}.png"
        try:
            async with client.stream("GET", url) as r:
                r.raise_for_status()
                if int(r.headers.get("content-length") or 0) > MAX_PORTRAIT_BYTES:
                    raise ValueError("слишком большой файл")
                content = b""
                async for chunk in r.aiter_bytes():
                    content += chunk
                    if len(content) > MAX_PORTRAIT_BYTES:
                        raise ValueError("слишком большой файл")
            await asyncio.to_thread(_save_portrait, content, target / f"{h.npc}.jpg")
            log.info("Скачан портрет %s", h.npc)
        except Exception as e:  # портрет не критичен: без него герой просто не распознаётся
            log.warning("Нет портрета для %s: %s: %s", h.npc, type(e).__name__, _redact(str(e)))


async def fetch_matches(client: httpx.AsyncClient, known: list[list]) -> list[list]:
    """Догружает матчи за окно WINDOW_DAYS. Матч: [match_id, start_time, radiant_win, radiant, dire]."""
    cutoff = int(time.time()) - config.WINDOW_DAYS * 86400
    known = [m for m in known if m[1] >= cutoff]
    latest = int((await _explorer(client, "select max(match_id) as id from public_matches"))[0]["id"])
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
            f"""select match_id, start_time, radiant_win, radiant_team, dire_team, duration
                from public_matches
                where match_id > {lo} and match_id <= {hi}
                  and avg_rank_tier >= {config.MIN_RANK}
                  and duration > 900 and lobby_type = 7 and game_mode in (1, 2, 22)""",
        )
        fresh += [
            m for m in (
                [r["match_id"], r["start_time"], bool(r["radiant_win"]), r["radiant_team"], r["dire_team"], r["duration"]]
                for r in rows
            )
            if _valid_match(m)
        ]
        log.info("OpenDota: %s–%s, +%d матчей", lo, hi, len(rows))
        hi = lo
        # Дошли до матчей старше окна: дальше смотреть незачем.
        if rows and min(r["start_time"] for r in rows) < cutoff:
            break
    return sorted((m for m in known + fresh if m[1] >= cutoff), key=lambda m: m[0])


def aggregate(heroes: dict[int, Hero], matches: list[list]) -> Stats:
    # Сама по себе безопасна: id вне 0 < id < MAX_HERO_ID раздули бы матрицы n×n до гигабайт.
    heroes = {k: h for k, h in heroes.items() if _valid_hero_id(k)}
    matches = [m for m in matches if _valid_match(m)]
    n = max(max(heroes, default=0), max((max(m[3] + m[4]) for m in matches), default=0)) + 1
    games = np.zeros(n, np.int64)
    wins = np.zeros(n, np.int64)
    vs_g = np.zeros((n, n), np.int64)
    vs_w = np.zeros((n, n), np.int64)
    w_g = np.zeros((n, n), np.int64)
    w_w = np.zeros((n, n), np.int64)
    p_g = np.zeros((n, 3), np.int64)
    p_w = np.zeros((n, 3), np.int64)
    if matches:
        phase = np.searchsorted(PHASE_EDGES, np.array([m[5] for m in matches]), side="right")
        rad = np.array([m[3] for m in matches], np.int64)
        dire = np.array([m[4] for m in matches], np.int64)
        rw = np.array([m[2] for m in matches], np.int64)
        for team, other, won in ((rad, dire, rw), (dire, rad, 1 - rw)):
            np.add.at(games, team.ravel(), 1)
            np.add.at(wins, team.ravel(), np.repeat(won, 5))
            np.add.at(p_g, (team.ravel(), np.repeat(phase, 5)), 1)
            np.add.at(p_w, (team.ravel(), np.repeat(phase, 5)), np.repeat(won, 5))
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
        phase_games=p_g, phase_wins=p_w,
    )


async def fetch_profiles(client: httpx.AsyncClient) -> dict[int, dict]:
    """Средние за игру по героям из разобранных публичных матчей примерно за два месяца."""
    latest = int((await _explorer(client, "select max(match_id) as id from public_matches"))[0]["id"])
    cols = ", ".join(f"avg({f}) {f}" for f in PROFILE_FIELDS)
    rows = await _explorer(
        client,
        f"""select hero_id, count(*) n, {cols} from player_matches
            where match_id > {latest - 85_000_000} and stuns is not null group by hero_id""",
    )
    out = {}
    for r in rows:
        try:
            hid, n = int(r["hero_id"]), int(r["n"])
            if _valid_hero_id(hid) and n >= 30:  # по меньшей выборке оценка слишком шумная
                out[hid] = {"n": n, **{f: round(float(r[f] or 0), 3) for f in PROFILE_FIELDS}}
        except (KeyError, TypeError, ValueError):
            continue
    return out


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
        heroes = {}
        for k, v in json.loads(hf.read_text()).items():
            h = Hero(**{**v, "positions": {int(p): s for p, s in v["positions"].items()}})
            if _valid_hero(h) and h.id == int(k):
                heroes[h.id] = h
        matches = [m for m in json.loads(mf.read_text()) if _valid_match(m)]
        return heroes, matches

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
            self.stats.profile = self._load_profiles(max_age=None) or {}
            self.stats.updated = (config.DATA_DIR / MATCHES_FILE).stat().st_mtime
            log.info("Кэш: %d матчей", self.stats.matches)

    def _load_profiles(self, max_age: float | None = 86400) -> dict[int, dict] | None:
        p = config.DATA_DIR / PROFILE_FILE
        if not p.exists() or (max_age and time.time() - p.stat().st_mtime > max_age):
            return None
        try:
            return {int(k): v for k, v in json.loads(p.read_text()).items()}
        except (ValueError, AttributeError):
            return None

    async def _profiles(self, client: httpx.AsyncClient) -> dict[int, dict]:
        """Профили героев: раз в сутки; при сбое OpenDota — прошлые, если есть."""
        fresh = self._load_profiles()
        if fresh:
            return fresh
        try:
            profiles = await fetch_profiles(client)
            if profiles:
                config.DATA_DIR.mkdir(parents=True, exist_ok=True)
                (config.DATA_DIR / PROFILE_FILE).write_text(json.dumps(profiles))
                return profiles
        except Exception as e:
            log.warning("Профили героев не обновились: %s: %s", type(e).__name__, _redact(str(e)))
        return self._load_profiles(max_age=None) or {}

    async def refresh(self) -> None:
        async with self._lock:
            try:
                cached = self._load_cache()
                async with _client() as client:
                    heroes = await fetch_heroes(client)
                    matches = await fetch_matches(client, cached[1] if cached else [])
                    profiles = await self._profiles(client)
                stats = await asyncio.to_thread(aggregate, heroes, matches)
                stats.profile = profiles
                await asyncio.to_thread(self._save_cache, heroes, matches)
                self.stats, self.error = stats, None
                log.info("Статистика обновлена: %d матчей", stats.matches)
            except Exception as e:
                # Наружу (в /api/health и ответы 503) — только общий текст: в тексте ошибки
                # httpx полный URL с ключом OpenDota, у OpenDota — текст SQL-ошибки.
                self.error = PUBLIC_ERROR
                log.error("Не удалось обновить статистику: %s: %s", type(e).__name__, _redact(str(e)))

    async def run_forever(self) -> None:
        while True:
            fresh = self.stats and time.time() - self.stats.updated < config.REFRESH_HOURS * 3600
            if not fresh:
                await self.refresh()
            await asyncio.sleep(600)
