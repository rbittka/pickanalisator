"""Разбор игрока по его матчам (OpenDota): против кого проигрывает, с кем выигрывает,
как изменились его герои в текущем патче и как его показатели выглядят на фоне других.

Только цифры, без «кто виноват»: личные выборки маленькие, поэтому винрейты против
и вместе с героями сглаживаются к общему винрейту игрока, а в списки попадают герои
с достаточным числом игр.
"""
from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime

from .data import API
from .steam import PlayerCache

SHRINK = 15  # «виртуальных» игр: при 15 играх против героя отклонение уменьшается вдвое
MIN_GAMES = 8  # меньше — в списки «против/вместе» не попадает
TOP = 6
BENCH_HEROES = 3
DAY = 86400

# Показатель из totals → ключ benchmarks OpenDota; per_min — делить на длительность матчей.
METRICS = [
    ("gold_per_min", "gold_per_min", "Золото в минуту", False, True),
    ("xp_per_min", "xp_per_min", "Опыт в минуту", False, True),
    ("last_hits", "last_hits_per_min", "Добивания в минуту", True, True),
    ("deaths", "deaths_per_min", "Смерти в минуту", True, False),
]


PERIODS = {"7": 7, "30": 30, "180": 180, "all": None}


def period_days(period: str) -> int | None:
    return PERIODS.get(period)


def _wr(wins: int, games: int) -> float:
    return wins / games if games else 0.0


def _ranked(rows: list[dict], games_key: str, wins_key: str, base: float, worst: bool) -> list[dict]:
    out = []
    for h in rows:
        g, w = h.get(games_key) or 0, h.get(wins_key) or 0
        if g < MIN_GAMES:
            continue
        adj = (w + SHRINK * base) / (g + SHRINK)
        out.append({"id": h["hero_id"], "games": g, "wins": w, "winrate": _wr(w, g), "delta": adj - base})
    out.sort(key=lambda x: x["delta"], reverse=not worst)
    return [x for x in out if (x["delta"] < 0) == worst][:TOP]


def percentile(value: float, table: list[dict]) -> float | None:
    """Доля игроков на герое с показателем ниже `value` (по перцентилям OpenDota)."""
    points = sorted((p["percentile"], p["value"]) for p in table if isinstance(p.get("value"), (int, float)))
    if not points:
        return None
    if value <= points[0][1]:
        return points[0][0] * value / points[0][1] if points[0][1] > 0 else 0.0
    for (p1, v1), (p2, v2) in zip(points, points[1:]):
        if value <= v2:
            return p1 + (p2 - p1) * ((value - v1) / (v2 - v1) if v2 != v1 else 0)
    return points[-1][0]


def _patches(raw) -> tuple[dict, dict | None]:
    patches = []
    for p in raw if isinstance(raw, list) else []:
        try:
            patches.append({"name": str(p["name"]), "start": datetime.fromisoformat(p["date"].replace("Z", "+00:00")).timestamp()})
        except (KeyError, ValueError, AttributeError):
            continue
    patches.sort(key=lambda p: p["start"])
    if not patches:
        return {"name": "?", "start": time.time() - 30 * DAY}, None
    return patches[-1], patches[-2] if len(patches) > 1 else None


def _since(ts: float) -> int:
    return max(1, math.ceil((time.time() - ts) / DAY))


async def analyze(players: PlayerCache, account_id: int, period: str) -> dict | None:
    days = period_days(period)
    q = {"date": days} if days else {}
    url = f"{API}/players/{account_id}"
    wl, heroes, patch_raw = await asyncio.gather(
        players.get("wl", account_id, f"{url}/wl", q),
        players.get("heroes", account_id, f"{url}/heroes", q),
        players.get("patch", 0, f"{API}/constants/patch", None, ttl=DAY),
    )
    if not isinstance(wl, dict) or not isinstance(heroes, list):
        return None
    heroes = [h for h in heroes if isinstance(h, dict) and isinstance(h.get("hero_id"), int)]
    total = (wl.get("win") or 0) + (wl.get("lose") or 0)
    base = _wr(wl.get("win") or 0, total)

    current, previous = _patches(patch_raw)
    since_cur = _since(current["start"])
    patch_calls = [players.get("heroes", account_id, f"{url}/heroes", {"date": since_cur})]
    if previous:
        patch_calls.append(players.get("heroes", account_id, f"{url}/heroes", {"date": _since(previous["start"])}))
    patch_rows = await asyncio.gather(*patch_calls)
    cur = {h["hero_id"]: h for h in patch_rows[0] or [] if isinstance(h, dict)}
    upto_prev = {h["hero_id"]: h for h in (patch_rows[1] if len(patch_rows) > 1 else None) or [] if isinstance(h, dict)}
    patch_heroes = []
    for hid in set(cur) | set(upto_prev):
        c = cur.get(hid, {})
        g, w = c.get("games") or 0, c.get("win") or 0
        # Прошлый патч = (с начала прошлого патча) − (с начала текущего).
        pg = (upto_prev.get(hid, {}).get("games") or 0) - g
        pw = (upto_prev.get(hid, {}).get("win") or 0) - w
        if g >= 5 or pg >= 5:
            patch_heroes.append({"id": hid, "games": g, "wins": w, "prev_games": max(pg, 0), "prev_wins": max(pw, 0)})
    patch_heroes.sort(key=lambda x: x["games"] + x["prev_games"], reverse=True)

    played = sorted((h for h in heroes if (h.get("games") or 0) > 0), key=lambda h: h["games"], reverse=True)
    benchmarks = await asyncio.gather(*(_bench(players, account_id, h, q) for h in played[:BENCH_HEROES]))

    return {
        "period": period,
        "games": total,
        "wins": wl.get("win") or 0,
        "heroes": [{"id": h["hero_id"], "games": h["games"], "wins": h.get("win") or 0} for h in played[:15]],
        "against": _ranked(heroes, "against_games", "against_win", base, worst=True),
        "with": _ranked(heroes, "with_games", "with_win", base, worst=False),
        "patch": {"name": current["name"], "since": current["start"],
                  "prev_name": previous["name"] if previous else None, "heroes": patch_heroes[:12]},
        "benchmarks": [b for b in benchmarks if b],
    }


async def _bench(players: PlayerCache, account_id: int, hero: dict, q: dict) -> dict | None:
    hid = hero["hero_id"]
    totals, bench = await asyncio.gather(
        players.get(f"totals{hid}", account_id, f"{API}/players/{account_id}/totals", {**q, "hero_id": hid}),
        players.get(f"bench{hid}", 0, f"{API}/benchmarks", {"hero_id": hid}, ttl=DAY),
    )
    if not isinstance(totals, list) or not isinstance(bench, dict):
        return None
    sums = {t.get("field"): t for t in totals if isinstance(t, dict)}
    duration = sums.get("duration", {})
    minutes = (duration.get("sum") or 0) / 60
    table = bench.get("result") or {}
    metrics = []
    for field, bkey, label, per_min, higher_better in METRICS:
        t = sums.get(field)
        if not t or not t.get("n"):
            continue
        if per_min:
            # Делим на суммарную длительность тех же матчей; если выборки разные — пропускаем.
            if not minutes or t["n"] != duration.get("n"):
                continue
            value = t["sum"] / minutes
        else:
            value = t["sum"] / t["n"]
        pct = percentile(value, table.get(bkey) or [])
        if pct is None:
            continue
        metrics.append({"key": bkey, "label": label, "value": round(value, 2),
                        "better_than": pct if higher_better else 1 - pct})
    return {"id": hid, "games": hero["games"], "metrics": metrics} if metrics else None
