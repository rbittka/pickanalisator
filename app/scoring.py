"""Оценка героя для драфта.

Оценка = сила в мете (винрейт героя − 50%) + сумма преимуществ против каждого врага
+ сумма синергий с каждым союзником, в долях винрейта. Малые выборки сглаживаются
к ожидаемому значению (эмпирический Байес): реальный разброс матчапов ~±3%, шум
винрейта на n играх ~0.5/√n, отсюда «виртуальных» игр K ≈ 0.25 / 0.03² ≈ 250.
"""
from __future__ import annotations

from dataclasses import dataclass

from .data import Stats

SHRINK = 250
MIN_ROLE_SHARE = 0.08  # при «любой позиции» не предлагаем роли, которые герой почти не играет
MIN_POS_SHARE = 0.03  # при выбранной позиции — хотя бы изредка играет её


@dataclass
class Slot:
    id: int
    pos: int


@dataclass
class Weights:
    counter: float = 1.0
    synergy: float = 0.7
    meta: float = 1.0


def base_winrate(stats: Stats, hero_id: int) -> float:
    g, w = stats.games[hero_id], stats.wins[hero_id]
    return float((w + 0.5 * SHRINK) / (g + SHRINK))


def _pair(games: int, wins: int, expected: float) -> float:
    return (wins + SHRINK * expected) / (games + SHRINK) - expected


def evaluate(stats: Stats, hero_id: int, pos: int, allies: list[Slot], enemies: list[Slot], w: Weights) -> dict:
    base = base_winrate(stats, hero_id)
    vs = []
    for e in enemies:
        # Против сильного героя ожидаемый винрейт ниже — поправка на его общую силу.
        expected = base - (stats.winrate(e.id) - 0.5)
        g, wins = int(stats.vs_games[hero_id, e.id]), int(stats.vs_wins[hero_id, e.id])
        vs.append({"id": e.id, "adv": _pair(g, wins, expected), "games": g})
    syn = []
    for a in allies:
        expected = base + (stats.winrate(a.id) - 0.5)
        g, wins = int(stats.with_games[hero_id, a.id]), int(stats.with_wins[hero_id, a.id])
        syn.append({"id": a.id, "adv": _pair(g, wins, expected), "games": g})
    counter = sum(x["adv"] for x in vs)
    synergy = sum(x["adv"] for x in syn)
    meta = base - 0.5
    hero = stats.heroes[hero_id]
    return {
        "id": hero_id,
        "name": hero.name,
        "npc": hero.npc,
        "pos": pos,
        "winrate": stats.winrate(hero_id),
        "games": int(stats.games[hero_id]),
        "counter": counter,
        "synergy": synergy,
        "meta": meta,
        "score": w.counter * counter + w.synergy * synergy + w.meta * meta,
        "vs": vs,
        "with": syn,
    }


def recommend(stats: Stats, allies: list[Slot], enemies: list[Slot], exclude: set[int],
              pos: int, w: Weights, limit: int = 15) -> list[dict]:
    taken_pos = {s.pos for s in allies}
    out = []
    for hero in stats.heroes.values():
        if hero.id in exclude or hero.id >= len(stats.games) or not stats.games[hero.id]:
            continue
        best = None
        for p, share in hero.positions.items():
            if pos and (p != pos or share < MIN_POS_SHARE):
                continue
            if not pos and (share < MIN_ROLE_SHARE or p in taken_pos):
                continue
            r = evaluate(stats, hero.id, p, allies, enemies, w)
            if not best or r["score"] > best["score"]:
                best = r
        if best:
            out.append(best)
    out.sort(key=lambda r: r["score"], reverse=True)
    return out[:limit]


def draft_advantage(stats: Stats, ally: list[Slot], enemy: list[Slot]) -> float | None:
    """Разница сумм оценок команд; осмысленна, когда героев поровну."""
    if not ally or not enemy:
        return None
    w = Weights(1.0, 0.5, 1.0)

    def side(us: list[Slot], them: list[Slot]) -> float:
        return sum(evaluate(stats, s.id, s.pos, [x for x in us if x is not s], them, w)["score"] for s in us)

    return side(ally, enemy) - side(enemy, ally)
