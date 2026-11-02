import asyncio
import json

import httpx

from app import config, data
from app.data import Hero, Store, _redact, aggregate


def test_redact_removes_api_key():
    text = "Client error '429' for url 'https://api.opendota.com/api/explorer?sql=select&api_key=SECRET-123'"
    out = _redact(text)
    assert "SECRET-123" not in out
    assert "api_key=***" in out


def test_store_error_does_not_leak_key(monkeypatch, caplog):
    secret = "SECRET-KEY-42"
    url = f"https://api.opendota.com/api/heroStats?api_key={secret}"

    def handler(request):
        return httpx.Response(429, request=request)

    def client():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fetch_heroes(c):
        r = await c.get(url)
        r.raise_for_status()

    monkeypatch.setattr(data, "_client", client)
    monkeypatch.setattr(data, "fetch_heroes", fetch_heroes)
    monkeypatch.setattr(Store, "_load_cache", lambda self: None)
    store = Store()
    with caplog.at_level("INFO"):
        asyncio.run(store.refresh())
    assert store.error == data.PUBLIC_ERROR
    assert secret not in store.error
    assert secret not in caplog.text
    assert "api_key=***" in caplog.text


def hero(i, npc):
    return Hero(i, f"Hero {i}", npc, [], {1: 1.0})


def test_load_cache_skips_bad_heroes_and_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    heroes = {
        "1": vars(hero(1, "antimage")),
        "2": vars(hero(2, "../../etc/x")),
        "3": vars(hero(3, 'x" onerror="alert(1)')),
        "5000": vars(hero(5000, "huge")),
    }
    (tmp_path / data.HEROES_FILE).write_text(json.dumps(heroes))
    matches = [
        [1, 100, True, [1, 2, 3, 4, 5], [6, 7, 8, 9, 10], 2400],
        [2, 100, True, [1, 2, 3, 4, 100000], [6, 7, 8, 9, 10], 2400],
        [3, 100, True, [1, 2, 3, 4], [6, 7, 8, 9, 10], 2400],
        [4, 100, True, [1, 2, 3, 4, 0], [6, 7, 8, 9, 10], 2400],
    ]
    (tmp_path / data.MATCHES_FILE).write_text(json.dumps(matches))
    loaded_heroes, loaded_matches = Store()._load_cache()
    assert list(loaded_heroes) == [1]
    assert [m[0] for m in loaded_matches] == [1]


def test_aggregate_ignores_huge_ids():
    heroes = {1: hero(1, "antimage"), 10**6: hero(10**6, "huge")}
    matches = [
        [1, 100, True, [1, 2, 3, 4, 5], [6, 7, 8, 9, 10], 2400],
        [2, 100, False, [1, 2, 3, 4, 10**6], [6, 7, 8, 9, 10], 2400],
    ]
    s = aggregate(heroes, matches)
    assert s.games.shape[0] <= data.MAX_HERO_ID
    assert s.matches == 1
    assert s.games[1] == 1 and s.wins[1] == 1


def test_fetch_heroes_skips_invalid_npc(monkeypatch):
    payload = [
        {"id": 1, "name": "npc_dota_hero_antimage", "localized_name": "Anti-Mage", "roles": ["Carry"]},
        {"id": 2, "name": "npc_dota_hero_../../evil", "localized_name": "Evil", "roles": []},
        {"id": 3, "name": 'npc_dota_hero_x" onerror="1', "localized_name": "X", "roles": []},
        {"id": 99999, "name": "npc_dota_hero_big", "localized_name": "Big", "roles": []},
    ]

    def handler(request):
        if request.url.path.endswith("/heroStats"):
            return httpx.Response(200, json=payload)
        return httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await data.fetch_heroes(c)

    heroes = asyncio.run(run())
    assert list(heroes) == [1]
