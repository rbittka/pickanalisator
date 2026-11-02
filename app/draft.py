"""Разбор драфта целиком: вердикт, фазы игры, контроль, пуш, Рошан и главные угрозы.

Всё считается по цифрам: фазы — по винрейту героев в матчах разной длительности,
контроль/строения/Рошан — по средним за игру из разобранных матчей OpenDota,
роли (инициатор, живучий) — из справочника героев OpenDota.
"""
from __future__ import annotations

import statistics

from . import scoring
from .data import Stats

SHRINK = 250
PHASES = (
    ("early", "Ранняя игра", "до 30 мин"),
    ("mid", "Середина игры", "30–45 мин"),
    ("late", "Поздняя игра", "после 45 мин"),
)
PHASE_ADVICE = {
    "early": "играйте агрессивно и старайтесь закончить до 30-й минуты",
    "mid": "ищите драки и цели (вышки, Рошан) с 30-й по 45-ю минуту",
    "late": "не форсируйте драки: к 45-й минуте ваш драфт сильнее",
}
ASPECTS = (
    # поле профиля, подпись, единицы, округление
    ("stuns", "Контроль", "с станов за игру", 0),
    ("tower_damage", "Урон по строениям", "за игру", -2),
    ("roshans_killed", "Рошан", "убийств за игру", 2),
    ("hero_damage", "Урон по героям", "за игру", -3),
    ("hero_healing", "Лечение", "за игру", -2),
)
VERDICT_EDGE = 0.04  # 4 п.п. суммарной оценки — заметное преимущество


def hero_phases(stats: Stats, hero_id: int) -> list[float] | None:
    """Насколько винрейт героя в ранней/средней/поздней игре выше его обычного."""
    pg, pw = stats.phase_games, stats.phase_wins
    if pg is None or hero_id >= len(pg) or not stats.games[hero_id]:
        return None
    base = stats.winrate(hero_id)
    return [float((pw[hero_id, b] + SHRINK * base) / (pg[hero_id, b] + SHRINK) - base) for b in range(3)]


def strongest_phase(phases: list[float] | None) -> str | None:
    if not phases:
        return None
    b = max(range(3), key=lambda i: phases[i])
    return PHASES[b][0] if phases[b] >= 0.01 else None


def _typical(stats: Stats) -> dict[str, float]:
    values = {f: [p[f] for p in stats.profile.values() if f in p] for f, *_ in ASPECTS}
    return {f: statistics.median(v) if v else 0.0 for f, v in values.items()}


def _team(stats: Stats, team: list[scoring.Slot], typical: dict[str, float]) -> dict:
    sums = {f: 0.0 for f, *_ in ASPECTS}
    phases = [0.0, 0.0, 0.0]
    roles: dict[str, int] = {}
    for s in team:
        prof = stats.profile.get(s.id, {})
        for f in sums:
            sums[f] += prof.get(f, typical[f])  # без данных считаем героя обычным
        for i, v in enumerate(hero_phases(stats, s.id) or [0, 0, 0]):
            phases[i] += v
        for r in stats.heroes[s.id].roles if s.id in stats.heroes else []:
            roles[r] = roles.get(r, 0) + 1
    return {"aspects": sums, "phases": phases, "roles": roles, "size": len(team)}


def _names(stats: Stats, ids: list[int]) -> str:
    return ", ".join(stats.heroes[i].name for i in ids if i in stats.heroes)


def analyze_draft(stats: Stats, ally: list[scoring.Slot], enemy: list[scoring.Slot]) -> dict | None:
    if not ally or not enemy:
        return None
    typical = _typical(stats)
    us, them = _team(stats, ally, typical), _team(stats, enemy, typical)
    advantage = scoring.draft_advantage(stats, ally, enemy) or 0.0
    notes: list[dict] = []

    def note(kind: str, text: str) -> None:
        notes.append({"kind": kind, "text": text})

    # Фазы игры: где драфт сильнее и где слабее противника.
    diffs = [a - b for a, b in zip(us["phases"], them["phases"])]
    best, worst = max(range(3), key=lambda i: diffs[i]), min(range(3), key=lambda i: diffs[i])
    if diffs[best] >= 0.02:
        note("plus", f"Ваш драфт сильнее в фазе «{PHASES[best][1].lower()}» ({PHASES[best][2]}): {PHASE_ADVICE[PHASES[best][0]]}.")
    if diffs[worst] <= -0.02:
        note("minus", f"Противник сильнее в фазе «{PHASES[worst][1].lower()}» ({PHASES[worst][2]}) — не затягивайте игру туда без нужды."
             if worst > best else
             f"Противник сильнее в фазе «{PHASES[worst][1].lower()}» ({PHASES[worst][2]}) — переживите её аккуратно, без лишних рисков.")

    # Аспекты состава — сравниваем, только когда в командах хотя бы по 3 героя.
    full = us["size"] >= 3 and them["size"] >= 3
    aspects = []
    for f, label, unit, digits in ASPECTS:
        a, e = us["aspects"][f], them["aspects"][f]
        norm = typical[f] * max(us["size"], 1)
        aspects.append({"key": f, "label": label, "unit": unit,
                        "ally": round(a, digits), "enemy": round(e, digits),
                        "typical_team": round(typical[f] * 5, digits)})
        if not full or not norm:
            continue
        ratio, vs = a / norm, a / e if e else 2
        if f == "stuns":
            if ratio < 0.7:
                note("minus", f"Мало контроля: около {a:.0f} с станов за игру при обычных {norm:.0f}. Ловить и добивать будет сложно.")
            elif vs > 1.3:
                note("plus", f"Контроля больше, чем у противника ({a:.0f} с против {e:.0f} с за игру).")
            elif vs < 0.75:
                note("minus", f"У противника заметно больше контроля ({e:.0f} с против ваших {a:.0f} с за игру): держитесь вместе, не попадайтесь поодиночке.")
        elif f == "tower_damage":
            if ratio < 0.7:
                note("minus", "Слабый пуш: героям трудно ломать строения — без драк выиграть будет тяжело.")
            elif vs > 1.3:
                note("plus", "Сильный пуш: ломайте строения после выигранных драк, у противника пуш слабее.")
            elif vs < 0.75:
                note("minus", "У противника пуш сильнее: защищайте вышки и не оставляйте линии без присмотра.")
        elif f == "roshans_killed":
            if vs > 1.4 and ratio > 1:
                note("plus", "Вы быстрее берёте Рошана — используйте Aegis для давления.")
            elif ratio < 0.6:
                note("minus", "Рошана брать долго: лучше делать это после выигранной драки.")
        elif f == "hero_healing" and ratio < 0.3 and them["aspects"][f] > a * 2:
            note("info", "Почти нет лечения: тяжёлые затяжные драки играются хуже, чем у противника.")

    if full:
        if not us["roles"].get("Initiator"):
            note("minus", "Нет явного инициатора: начинать драки будет некому.")
        if not us["roles"].get("Durable"):
            note("minus", "Нет живучего героя для первой линии: в драках будут фокусить хрупких.")

    # Главная угроза у противника и ваш лучший ответ.
    w = scoring.Weights()
    threats = []
    for e in enemy:
        r = scoring.evaluate(stats, e.id, e.pos, [x for x in enemy if x is not e], ally, w)
        threats.append((r["counter"], e.id, [v["id"] for v in sorted(r["vs"], key=lambda v: -v["adv"])[:2] if v["adv"] > 0.01]))
    threats.sort(reverse=True)
    if threats and threats[0][0] > 0.02:
        _, tid, victims = threats[0]
        tail = f": силён против {_names(stats, victims)}" if victims else ""
        note("minus", f"Главная угроза — {stats.heroes[tid].name}{tail}.")
    answers = []
    for a in ally:
        r = scoring.evaluate(stats, a.id, a.pos, [x for x in ally if x is not a], enemy, w)
        answers.append((r["counter"], a.id))
    answers.sort(reverse=True)
    if answers and answers[0][0] > 0.02:
        note("plus", f"Лучший ответ на их драфт у вас — {stats.heroes[answers[0][1]].name}.")

    if advantage > VERDICT_EDGE:
        verdict, headline = "good", "Драфт выигрышный"
    elif advantage < -VERDICT_EDGE:
        verdict, headline = "bad", "Драфт проигрышный"
    else:
        verdict, headline = "even", "Драфт примерно равный"
    if us["size"] < 5 or them["size"] < 5:
        headline += " (пока не все герои выбраны)"

    return {
        "verdict": verdict,
        "headline": headline,
        "advantage": advantage,
        "phases": [{"key": k, "label": label, "minutes": m, "ally": us["phases"][i], "enemy": them["phases"][i]}
                   for i, (k, label, m) in enumerate(PHASES)],
        "aspects": aspects if stats.profile else [],
        "notes": notes,
    }
