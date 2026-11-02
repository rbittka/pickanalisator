"""Запасное распознавание через Claude: для фото экрана и скриншотов без верхней панели.

Claude получает снимок, его верхнюю полосу в высоком разрешении и эталонный лист
со всеми портретами под номерами и возвращает номера героев по слотам.
"""
from __future__ import annotations

import base64
import json
import logging
from io import BytesIO

import anthropic
from PIL import Image

from . import config
from .data import Hero
from .limits import SlidingWindow
from .recognize import References, reference_sheet

log = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "properties": {
        "slots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "team": {"type": "string", "enum": ["radiant", "dire", "ban"]},
                    "seen": {"type": "string"},
                    "n": {"type": "integer"},
                },
                "required": ["team", "seen", "n"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["slots"],
    "additionalProperties": False,
}

PROMPT = """Задача: определить героев Dota 2 на снимке драфта.
Изображение 1 — снимок целиком (скриншот или фото монитора). Изображение 2 — его верхние 25% в высоком
разрешении (там обычно портреты верхней панели). Изображение 3 — эталонный лист: портреты всех героев,
номер в левом верхнем углу каждой плитки.

На снимке экран пиков (All Pick, Captains Mode и т.п.) или верхняя панель героев в матче.
Команда Света (Radiant) — левая половина, команда Тьмы (Dire) — правая.
Баны — отдельные портреты, обычно серые, затемнённые или перечёркнутые; если банов нет, не придумывай их.
Пустые слоты и слоты, где герой ещё не выбран (силуэт, вопросительный знак), пропускай.

Как определять: для каждого слота посмотри на портрет (цвет кожи, волосы, головной убор, оружие, фон),
найди самую похожую плитку на эталонном листе и проверь совпадение по деталям. Подпись с именем героя
на слоте надёжнее картинки. Не ставь героя, если не уверен.

Список героев (номер = номер плитки на эталонном листе):
{numbered}

Для каждого распознанного слота верни team, коротко что видно на портрете (seen, 3–8 слов) и номер героя n.
Слоты перечисляй слева направо."""


class VisionUnavailable(Exception):
    """Распознавание через Claude выключено или упёрлось в лимит; текст — для пользователя."""


class Vision:
    def __init__(self) -> None:
        self.client = anthropic.AsyncAnthropic() if self.configured() else None
        self._sheet: tuple[int, bytes] | None = None
        self._daily = SlidingWindow(config.CLAUDE_DAILY_LIMIT, 86400)
        self._per_ip = SlidingWindow(config.CLAUDE_PER_IP_HOURLY, 3600)

    @staticmethod
    def configured() -> bool:
        import os

        return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))

    def _check_limits(self, ip: str) -> None:
        # Сначала личный лимит: иначе отказ по нему съедал бы общий дневной.
        if not self._per_ip.hit(ip):
            raise VisionUnavailable("Слишком много фото за час. Попробуй позже или пришли скриншот всего экрана.")
        if not self._daily.hit():
            raise VisionUnavailable("Дневной лимит распознавания фото исчерпан. Пришли скриншот всего экрана игры.")

    def _sheet_for(self, refs: References) -> bytes:
        if not self._sheet or self._sheet[0] != id(refs):
            self._sheet = (id(refs), reference_sheet(refs))
        return self._sheet[1]

    async def recognize(self, im: Image.Image, refs: References, ip: str) -> dict[str, list[Hero]]:
        if not self.client:
            raise VisionUnavailable("Верхняя панель с героями не найдена. Пришли скриншот всего экрана игры без обрезки.")
        self._check_limits(ip)

        def jpeg(img: Image.Image, max_side: int) -> dict:
            img = img.copy()
            img.thumbnail((max_side, max_side))
            buf = BytesIO()
            img.save(buf, "JPEG", quality=90)
            return _image_block(buf.getvalue())

        top = im.crop((0, 0, im.width, round(im.height * 0.25)))
        numbered = "\n".join(f"{i + 1}. {h.name}" for i, h in enumerate(refs.heroes))
        content = [
            jpeg(im, 1568),
            jpeg(top, 2000),
            _image_block(self._sheet_for(refs)),
            {"type": "text", "text": PROMPT.format(numbered=numbered)},
        ]
        try:
            response = await self.client.beta.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.RateLimitError:
            raise VisionUnavailable("Сервис распознавания перегружен, попробуй через минуту.")
        except anthropic.APIStatusError as e:
            log.error("Claude API %s: %s (request %s)", e.status_code, e.message, e.response.headers.get("request-id"))
            raise VisionUnavailable("Не удалось распознать фото. Попробуй ещё раз или пришли скриншот.")
        except anthropic.APIConnectionError:
            raise VisionUnavailable("Нет связи с сервисом распознавания, попробуй ещё раз.")

        if response.stop_reason == "refusal":
            raise VisionUnavailable("Это изображение не удалось обработать. Пришли скриншот всего экрана игры.")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            slots = json.loads(text)["slots"]
        except (ValueError, KeyError):
            log.error("Claude вернул неразборчивый ответ: %r", text[:300])
            raise VisionUnavailable("Не удалось разобрать ответ. Попробуй ещё раз.")

        out: dict[str, list[Hero]] = {"radiant": [], "dire": [], "ban": []}
        for s in slots:
            n = s.get("n")
            if isinstance(n, int) and 1 <= n <= len(refs.heroes) and s.get("team") in out:
                out[s["team"]].append(refs.heroes[n - 1])
        return out


def _image_block(data: bytes) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(data).decode()},
    }
