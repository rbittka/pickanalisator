"""Анализатор пиков Dota 2: сайт и API."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, scoring
from .data import Stats, Store, portrait_path
from .recognize import References, build_references, load_screenshot, match_top_bar
from .vision import Vision, VisionUnavailable

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

store = Store()
vision = Vision()
_refs: tuple[int, References] | None = None


def stats() -> Stats:
    if not store.stats:
        detail = "Статистика ещё загружается, попробуй через минуту."
        if store.error:
            detail = f"Не удалось загрузить статистику: {store.error}"
        raise HTTPException(503, detail)
    return store.stats


def references() -> References:
    global _refs
    s = stats()
    if not _refs or _refs[0] != id(s):
        _refs = (id(s), build_references(list(s.heroes.values())))
    return _refs[1]


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.load_from_disk()
    task = asyncio.create_task(store.run_forever())
    yield
    task.cancel()


app = FastAPI(title="Анализатор пиков Dota 2", version="1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])


# --- Модели запросов ---

class SlotIn(BaseModel):
    id: int
    pos: int = Field(ge=1, le=5)


class Replace(BaseModel):
    team: Literal["ally", "enemy"]
    index: int = Field(ge=0, le=4)


class WeightsIn(BaseModel):
    counter: float = Field(1.0, ge=0, le=3)
    synergy: float = Field(0.7, ge=0, le=3)
    meta: float = Field(1.0, ge=0, le=3)


class AnalyzeIn(BaseModel):
    ally: list[SlotIn] = Field(default_factory=list, max_length=5)
    enemy: list[SlotIn] = Field(default_factory=list, max_length=5)
    bans: list[int] = Field(default_factory=list, max_length=24)
    side: Literal["ally", "enemy"] = "ally"
    pos: int = Field(0, ge=0, le=5)
    replace: Replace | None = None
    weights: WeightsIn = Field(default_factory=WeightsIn)
    limit: int = Field(15, ge=1, le=60)


# --- API ---

@app.get("/api/health")
def health():
    return {"ok": True, "ready": store.stats is not None, "error": store.error}


@app.get("/api/meta")
def meta():
    s = stats()
    return {
        "source": "OpenDota, публичные матчи",
        "min_rank": config.MIN_RANK,
        "window_days": config.WINDOW_DAYS,
        "matches": s.matches,
        "first_match_time": s.first_match_time,
        "last_match_time": s.last_match_time,
        "updated": s.updated,
        "photo_recognition": vision.configured(),
    }


@app.get("/api/heroes")
def heroes():
    s = stats()
    return [
        {
            "id": h.id,
            "name": h.name,
            "npc": h.npc,
            "img": f"/heroes/{h.npc}.jpg",
            "positions": h.positions,
            "winrate": s.winrate(h.id),
            "games": int(s.games[h.id]) if h.id < len(s.games) else 0,
        }
        for h in sorted(s.heroes.values(), key=lambda h: h.name)
    ]


@app.post("/api/analyze")
def analyze(body: AnalyzeIn):
    """Рекомендации для стороны `side`; с `replace` — замена героя в указанном слоте."""
    s = stats()
    for slot in body.ally + body.enemy:
        if slot.id not in s.heroes:
            raise HTTPException(422, f"Неизвестный герой {slot.id}")
    ally = [scoring.Slot(x.id, x.pos) for x in body.ally]
    enemy = [scoring.Slot(x.id, x.pos) for x in body.enemy]
    teams = {"ally": ally, "enemy": enemy}
    side, pos, current_slot = body.side, body.pos, None
    if body.replace:
        if body.replace.index >= len(teams[body.replace.team]):
            raise HTTPException(422, "В этом слоте нет героя")
        side = body.replace.team
        current_slot = teams[side][body.replace.index]
        pos = current_slot.pos
    allies = [x for x in teams[side] if x is not current_slot]
    enemies = teams["enemy" if side == "ally" else "ally"]
    w = scoring.Weights(**body.weights.model_dump())
    exclude = {x.id for x in ally + enemy} | set(body.bans)

    current = scoring.evaluate(s, current_slot.id, current_slot.pos, allies, enemies, w) if current_slot else None
    recs = scoring.recommend(s, allies, enemies, exclude, pos, w, body.limit)
    if current:
        for r in recs:
            r["delta"] = r["score"] - current["score"]
    return {
        "recommendations": recs,
        "current": current,
        "advantage": scoring.draft_advantage(s, ally, enemy),
    }


@app.post("/api/recognize")
async def recognize(request: Request, image: UploadFile = File(...),
                    allow_claude: bool = Form(True)):
    """Герои на скриншоте драфта. Сначала верхняя панель, затем (если нет) Claude."""
    data = await image.read(config.MAX_UPLOAD_BYTES + 1)
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Файл больше 15 МБ")
    try:
        im = await asyncio.to_thread(load_screenshot, data)
    except Exception:
        raise HTTPException(422, "Не получилось открыть изображение. Нужен PNG, JPEG или WebP.")
    refs = references()
    found = await asyncio.to_thread(match_top_bar, im, refs)
    method = "topbar"
    bans = []
    if len(found["radiant"]) + len(found["dire"]) < 2:
        if not allow_claude:
            raise HTTPException(422, "Верхняя панель с героями не найдена.")
        try:
            got = await vision.recognize(im, refs, request.client.host if request.client else "?")
        except VisionUnavailable as e:
            raise HTTPException(422, str(e))
        found, bans, method = got, got["ban"], "claude"
    return {
        "method": method,
        "radiant": [h.id for h in found["radiant"]],
        "dire": [h.id for h in found["dire"]],
        "bans": [h.id for h in bans],
    }


@app.post("/api/admin/refresh")
async def admin_refresh(x_admin_token: str = Header("")):
    if not config.ADMIN_TOKEN or x_admin_token != config.ADMIN_TOKEN:
        raise HTTPException(403, "Нет доступа")
    await store.refresh()
    return {"ok": store.error is None, "error": store.error}


# --- Сайт ---

@app.get("/heroes/{name}.jpg")
def portrait(name: str):
    path = portrait_path(name) if name.replace("_", "").isalnum() else None
    if not path:
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
