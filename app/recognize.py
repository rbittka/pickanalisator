"""Распознавание героев на верхней панели Dota 2 по скриншоту.

Портреты верхней панели — та же картинка, что и эталоны в static/heroes, поэтому
каждый слот сравнивается попиксельно со всеми героями. Панель масштабируется по
высоте экрана и стоит по центру: портрет шириной ~0.108 высоты, центры слотов на
±(0.182 + 0.115·k) высоты от середины. Сравнение чувствительно к сдвигу в 2–3
пикселя, поэтому каждый слот ищется отдельно: грубо в окрестности ожидаемого
места, затем точнее вокруг лучшего попадания.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import numpy as np
from PIL import Image, ImageOps

from .data import Hero, portrait_path

COARSE = (16, 9)
FINE = (32, 18)
NORM_HEIGHT = 720  # скриншот приводим к этой высоте: дальше все размеры в пикселях 720p
# Настоящий портрет даёт среднее отличие ~9–18 (из 255) и отрыв 20+ от второго кандидата.
MAX_DIST = 22
MIN_MARGIN = 14


@dataclass
class References:
    heroes: list[Hero]
    coarse: np.ndarray  # [n, 16*9*3]
    fine: np.ndarray  # [n, 32*18*3]


def _vec(im: Image.Image, size: tuple[int, int], box=None) -> np.ndarray:
    return np.asarray(im.resize(size, Image.Resampling.BILINEAR, box=box, reducing_gap=None), np.float32).ravel()


def build_references(heroes: list[Hero]) -> References:
    kept, coarse, fine = [], [], []
    for h in heroes:
        path = portrait_path(h.npc)
        if not path:
            continue
        im = Image.open(path).convert("RGB")
        kept.append(h)
        coarse.append(_vec(im, COARSE))
        fine.append(_vec(im, FINE))
    return References(kept, np.stack(coarse), np.stack(fine))


def _closest(v: np.ndarray, refs: np.ndarray) -> tuple[int, float, float]:
    d = np.abs(refs - v).mean(axis=1)
    order = np.argpartition(d, 1)[:2]
    i, j = (order if d[order[0]] <= d[order[1]] else order[::-1])
    return int(i), float(d[i]), float(d[j] - d[i])


def _inside(im: Image.Image, x: float, y: float, w: float, h: float) -> bool:
    return x >= 0 and y >= 0 and x + w <= im.width and y + h <= im.height


def load_screenshot(data: bytes) -> Image.Image:
    im = ImageOps.exif_transpose(Image.open(BytesIO(data))).convert("RGB")
    if im.height != NORM_HEIGHT:
        im = im.resize((round(im.width * NORM_HEIGHT / im.height), NORM_HEIGHT), Image.Resampling.BOX)
    return im


def match_top_bar(im: Image.Image, refs: References) -> dict[str, list[Hero]]:
    """Возвращает героев Света и Тьмы слева направо; нераспознанные слоты пропускаются."""
    H, mid = im.height, im.width / 2
    # Работаем только с верхней полосой: так ресайзы не трогают остальной экран.
    strip = im.crop((0, 0, im.width, 100))
    out: dict[str, list[Hero]] = {"radiant": [], "dire": []}
    for side, team in ((-1, "radiant"), (1, "dire")):
        for i in range(5):
            k = 4 - i if side < 0 else i  # слева направо
            cx0 = mid + side * (0.182 + 0.115 * k) * H
            best = None
            for w in range(70, 87, 4):
                h = w * 9 / 16
                for y in range(0, 41, 2):  # с запасом на заголовок окна
                    for dx in range(-18, 19, 2):
                        x = cx0 + dx - w / 2
                        if not _inside(strip, x, y, w, h):
                            continue
                        idx, d, margin = _closest(_vec(strip, COARSE, (x, y, x + w, y + h)), refs.coarse)
                        if best is None or d < best[1]:
                            best = (idx, d, margin, x + w / 2, y, w)
            if best is None:  # слот за краем картинки (узкий экран или обрезка)
                continue
            _, _, _, cx, cy, cw = best
            best = None
            for dw in (-2, 0, 2):
                w = cw + dw
                h = w * 9 / 16
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        x, y = cx + dx - w / 2, cy + dy
                        if not _inside(strip, x, y, w, h):
                            continue
                        idx, d, margin = _closest(_vec(strip, FINE, (x, y, x + w, y + h)), refs.fine)
                        if best is None or d < best[1]:
                            best = (idx, d, margin)
            idx, d, margin = best
            if d < MAX_DIST and margin > MIN_MARGIN:
                out[team].append(refs.heroes[idx])
    return out


def reference_sheet(refs: References) -> bytes:
    """Эталонный лист для Claude: все портреты с номерами (номер = индекс + 1 в refs.heroes)."""
    from PIL import ImageDraw, ImageFont

    cols, w, h = 12, 96, 54
    rows = -(-len(refs.heroes) // cols)
    sheet = Image.new("RGB", (cols * w, rows * h))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=15)
    for i, hero in enumerate(refs.heroes):
        x, y = (i % cols) * w, (i // cols) * h
        sheet.paste(Image.open(portrait_path(hero.npc)).convert("RGB").resize((w, h)), (x, y))
        label = str(i + 1)
        draw.rectangle((x, y, x + 9 * len(label) + 6, y + 18), fill=(0, 0, 0))
        draw.text((x + 3, y + 1), label, fill=(255, 211, 77), font=font)
    buf = BytesIO()
    sheet.save(buf, "JPEG", quality=90)
    return buf.getvalue()
