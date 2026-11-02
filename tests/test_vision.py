import asyncio
import json
from io import BytesIO

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config, main
from app.recognize import build_references, load_screenshot
from app.vision import Vision, VisionUnavailable

from .test_app import make_heroes, synthetic_stats


@pytest.fixture(scope="module")
def heroes():
    return make_heroes()


@pytest.fixture(scope="module")
def refs(heroes):
    return build_references(list(heroes.values()))


def message(text: str, stop_reason: str = "end_turn") -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": config.CLAUDE_MODEL,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


def fake_vision(reply: dict, requests: list) -> Vision:
    """Vision с настоящим SDK, но без сети: запросы попадают в `requests`, ответ — `reply`."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=reply)

    v = Vision()
    v.client = anthropic.AsyncAnthropic(
        api_key="test", max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return v


def photo() -> Image.Image:
    buf = BytesIO()
    Image.new("RGB", (1280, 720), (40, 45, 50)).save(buf, "JPEG")
    return load_screenshot(buf.getvalue())


SLOTS = {"slots": [
    {"team": "radiant", "seen": "синий маг", "n": 5},
    {"team": "ban", "seen": "серый портрет", "n": 2},
    {"team": "dire", "seen": "непонятно", "n": 9999},
]}


def test_request_shape_and_parsing(refs):
    requests = []
    v = fake_vision(message(json.dumps(SLOTS)), requests)
    out = asyncio.run(v.recognize(photo(), refs, "1.1.1.1"))

    (req,) = requests
    assert req.url.path == "/v1/messages"
    assert "server-side-fallback-2026-07-01" in req.headers["anthropic-beta"]
    body = json.loads(req.content)
    assert body["model"] == config.CLAUDE_MODEL
    assert body["fallbacks"] == "default"
    assert body["output_config"]["format"]["type"] == "json_schema"
    content = body["messages"][0]["content"]
    assert [b["type"] for b in content] == ["image", "image", "image", "text"]
    assert all(b["source"]["type"] == "base64" for b in content[:3])

    assert [h.name for h in out["radiant"]] == [refs.heroes[4].name]
    assert [h.name for h in out["ban"]] == [refs.heroes[1].name]
    assert out["dire"] == []  # номер вне списка отброшен


def test_refusal_is_reported(refs):
    v = fake_vision(message("", stop_reason="refusal"), [])
    with pytest.raises(VisionUnavailable):
        asyncio.run(v.recognize(photo(), refs, "1.1.1.1"))


def test_api_error_is_reported(refs):
    def handler(request):
        return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}})

    v = Vision()
    v.client = anthropic.AsyncAnthropic(api_key="test", max_retries=0,
                                        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    with pytest.raises(VisionUnavailable):
        asyncio.run(v.recognize(photo(), refs, "1.1.1.1"))


def test_per_ip_limit(refs, monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_PER_IP_HOURLY", 2)
    requests = []
    v = fake_vision(message(json.dumps(SLOTS)), requests)
    for _ in range(2):
        asyncio.run(v.recognize(photo(), refs, "2.2.2.2"))
    with pytest.raises(VisionUnavailable):
        asyncio.run(v.recognize(photo(), refs, "2.2.2.2"))
    asyncio.run(v.recognize(photo(), refs, "3.3.3.3"))  # другой IP не задет
    assert len(requests) == 3


def test_api_recognize_falls_back_to_claude(heroes, monkeypatch):
    monkeypatch.setattr(main.store, "stats", synthetic_stats(heroes))
    monkeypatch.setattr(main, "vision", fake_vision(message(json.dumps(SLOTS)), []))
    buf = BytesIO()
    Image.new("RGB", (1280, 720), (40, 45, 50)).save(buf, "JPEG")
    r = TestClient(main.app).post("/api/recognize", files={"image": ("p.jpg", buf.getvalue(), "image/jpeg")})
    assert r.status_code == 200
    j = r.json()
    refs = main.references()
    assert j["method"] == "claude"
    assert j["radiant"] == [refs.heroes[4].id]
    assert j["bans"] == [refs.heroes[1].id]
    assert j["dire"] == []
