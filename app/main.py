"""Анализатор пиков Dota 2: сайт и API."""
from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analysis, config, scoring, steam
from .data import Stats, Store, portrait_path
from .limits import SlidingWindow, client_ip
from .recognize import BadImage, References, build_references, match_top_bar, normalize, open_image
from .vision import Vision, VisionUnavailable

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

store = Store()
vision = Vision()
_refs: tuple[int, References] | None = None
_recognize_slots = asyncio.Semaphore(config.RECOGNIZE_CONCURRENCY)
_recognize_per_ip = SlidingWindow(config.RECOGNIZE_PER_IP_MINUTE, 60)
_analyze_per_ip = SlidingWindow(240, 60)
_me_per_ip = SlidingWindow(30, 60)
players = steam.PlayerCache()


def stats() -> Stats:
    if not store.stats:
        raise HTTPException(503, store.error or "Статистика ещё загружается, попробуй через минуту.")
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

# Страницы сайта: скрипты и стили только свои, встраивать сайт в чужие фреймы нельзя.
# /docs грузит Swagger UI с CDN, поэтому для него CSP не ставим.
CSP = ("default-src 'self'; img-src 'self' blob: data: https://avatars.steamstatic.com; style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


class BodyLimit:
    """Обрывает запросы с телом больше лимита до того, как их разберёт multipart
    (иначе многогигабайтная загрузка сначала целиком ляжет во временный файл)."""

    def __init__(self, app, limit: int) -> None:
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        length = dict(scope["headers"]).get(b"content-length")
        if length and length.isdigit() and int(length) > self.limit:
            return await _too_large(send)
        seen = 0

        async def limited():
            nonlocal seen
            message = await receive()
            seen += len(message.get("body", b""))
            if seen > self.limit:
                raise HTTPException(413, "Файл больше 15 МБ")
            return message

        await self.app(scope, limited, send)


async def _too_large(send) -> None:
    body = '{"detail":"Файл больше 15 МБ"}'.encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


app.add_middleware(BodyLimit, limit=config.MAX_UPLOAD_BYTES + 64 * 1024)
# В cookie лежит только account_id, подписанный секретом. Без SESSION_SECRET (локально)
# секрет случайный: после перезапуска сервера придётся войти заново.
app.add_middleware(
    SessionMiddleware, secret_key=config.SESSION_SECRET or secrets.token_urlsafe(32),
    session_cookie="dd_session", max_age=30 * 86400, same_site="lax",
    https_only=config.PUBLIC_URL.startswith("https://"),
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if not request.url.path.startswith(("/docs", "/redoc")):
        response.headers["Content-Security-Policy"] = CSP
    # Страницу и скрипт браузер должен перепроверять, иначе после обновления сайта
    # он показывает старую версию (по ETag это дёшево: 304 без тела).
    if request.url.path in ("/", "/index.html", "/app.js", "/me.html", "/me.js"):
        response.headers["Cache-Control"] = "no-cache"
    return response


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
    pool: list[int] = Field(default_factory=list, max_length=150)  # личный пул героев игрока


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
def analyze(body: AnalyzeIn, request: Request):
    """Рекомендации для стороны `side`; с `replace` — замена героя в указанном слоте."""
    if not _analyze_per_ip.hit(client_ip(request)):
        raise HTTPException(429, "Слишком много запросов, подожди минуту.")
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
    # Из пула ищем на любой позиции, кроме уже занятых: игрок сам знает, кем играет.
    pool_recs = scoring.recommend(s, allies, enemies, exclude, pos, w, 5, only=set(body.pool)) if body.pool else []
    if current:
        for r in recs + pool_recs:
            r["delta"] = r["score"] - current["score"]
    return {
        "recommendations": recs,
        "pool_recommendations": pool_recs,
        "current": current,
        "advantage": scoring.draft_advantage(s, ally, enemy),
    }


@app.post("/api/recognize")
async def recognize(request: Request, image: UploadFile = File(...),
                    allow_claude: bool = Form(True)):
    """Герои на скриншоте драфта. Сначала верхняя панель, затем (если нет) Claude."""
    ip = client_ip(request)
    if not _recognize_per_ip.hit(ip):
        raise HTTPException(429, "Слишком много скриншотов за минуту, подожди немного.")
    data = await image.read(config.MAX_UPLOAD_BYTES + 1)
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Файл больше 15 МБ")
    refs = references()
    # Разбор нагружает процессор: одновременно не больше нескольких, остальные ждут.
    async with _recognize_slots:
        try:
            original = await asyncio.to_thread(open_image, data)
        except BadImage as e:
            raise HTTPException(422, str(e))
        except Exception:
            log.warning("Не удалось разобрать загруженное изображение", exc_info=True)
            raise HTTPException(422, "Не получилось открыть изображение. Нужен PNG, JPEG или WebP.")
        found = await asyncio.to_thread(match_top_bar, normalize(original), refs)
    method = "topbar"
    bans = []
    if len(found["radiant"]) + len(found["dire"]) < 2:
        if not allow_claude:
            raise HTTPException(422, "Верхняя панель с героями не найдена.")
        try:
            got = await vision.recognize(original, refs, ip)
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
    if not config.ADMIN_TOKEN or not hmac.compare_digest(x_admin_token.encode(), config.ADMIN_TOKEN.encode()):
        raise HTTPException(403, "Нет доступа")
    # Обновление идёт минуту и больше: запускаем в фоне и сразу отвечаем.
    asyncio.create_task(store.refresh())
    return {"started": True}


# --- Вход через Steam ---

def _base_url(request: Request) -> str:
    # За прокси Render сервер сам видит http://, поэтому адрес берём из настроек.
    return config.PUBLIC_URL or str(request.base_url).rstrip("/")


@app.get("/auth/steam/login")
def steam_login(request: Request):
    base = _base_url(request)
    return RedirectResponse(steam.login_url(base + "/auth/steam/callback", base + "/"))


@app.get("/auth/steam/callback")
async def steam_callback(request: Request):
    account_id = await steam.verify(dict(request.query_params), _base_url(request) + "/auth/steam/callback")
    if not account_id:
        return RedirectResponse("/?login=failed")
    request.session.clear()
    request.session["account_id"] = account_id
    return RedirectResponse("/")


@app.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


def _account(request: Request) -> int | None:
    value = request.session.get("account_id")
    return value if isinstance(value, int) else None


@app.get("/api/me")
async def me(request: Request):
    account_id = _account(request)
    if not account_id:
        return {"logged_in": False}
    if not _me_per_ip.hit(client_ip(request)):
        raise HTTPException(429, "Слишком много запросов, подожди минуту.")
    return {"logged_in": True, **await players.profile(account_id)}


Period = Literal["7", "30", "180", "all"]


def _logged_in(request: Request) -> int:
    account_id = _account(request)
    if not account_id:
        raise HTTPException(401, "Нужно войти через Steam.")
    if not _me_per_ip.hit(client_ip(request)):
        raise HTTPException(429, "Слишком много запросов, подожди минуту.")
    return account_id


@app.get("/api/me/heroes")
async def my_heroes(request: Request, period: Period = "180"):
    """Герои игрока за полгода или за всё время: игры и победы (из OpenDota)."""
    account_id = _logged_in(request)
    heroes = await players.heroes(account_id, analysis.period_days(period))
    if heroes is None:
        raise HTTPException(503, "OpenDota сейчас не отвечает, попробуй позже.")
    return {"heroes": heroes}


@app.get("/api/me/analysis")
async def my_analysis(request: Request, period: Period = "180"):
    """Разбор игрока: против кого проигрывает, с кем выигрывает, патч, сравнение с другими."""
    result = await analysis.analyze(players, _logged_in(request), period)
    if result is None:
        raise HTTPException(503, "OpenDota сейчас не отвечает или история матчей закрыта. Попробуй позже.")
    return result


# --- Сайт ---

@app.get("/heroes/{name}.jpg")
def portrait(name: str):
    path = portrait_path(name) if name.replace("_", "").isalnum() else None
    if not path:
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
