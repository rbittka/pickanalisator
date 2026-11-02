import asyncio

import httpx
from fastapi.testclient import TestClient

from app import main, steam

RETURN_TO = "http://testserver/auth/steam/callback"
STEAM_ID = "76561198047011640"  # account_id 86745912


def params(**over):
    p = {
        "openid.ns": steam.NS,
        "openid.mode": "id_res",
        "openid.op_endpoint": steam.STEAM_OPENID,
        "openid.claimed_id": f"https://steamcommunity.com/openid/id/{STEAM_ID}",
        "openid.identity": f"https://steamcommunity.com/openid/id/{STEAM_ID}",
        "openid.return_to": RETURN_TO,
        "openid.response_nonce": "2026-11-02T00:00:00Zabc",
        "openid.assoc_handle": "1234567890",
        "openid.signed": "signed,op_endpoint,claimed_id,identity,return_to,response_nonce,assoc_handle",
        "openid.sig": "c2lnbmF0dXJl",
    }
    p.update(over)
    return p


def steam_client(valid: bool, seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, text=f"ns:{steam.NS}\nis_valid:{'true' if valid else 'false'}\n")
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run_verify(p, valid=True):
    seen = []
    result = asyncio.run(steam.verify(p, RETURN_TO, steam_client(valid, seen)))
    return result, seen


def test_verify_accepts_genuine_response():
    account_id, seen = run_verify(params())
    assert account_id == 86745912
    assert seen[0]["openid.mode"] == "check_authentication"  # перепроверка у самого Steam


def test_verify_rejects_forgeries():
    assert run_verify(params(), valid=False)[0] is None  # Steam не подтвердил подпись
    # Ответ, выданный другому сайту, не принимаем — даже не спрашивая Steam.
    result, seen = run_verify(params(**{"openid.return_to": "https://evil.example/cb"}))
    assert result is None and not seen
    assert run_verify(params(**{"openid.claimed_id": "https://evil.example/openid/id/76561198047011640"}))[0] is None
    assert run_verify(params(**{"openid.identity": "https://steamcommunity.com/openid/id/76561198000000000"}))[0] is None
    assert run_verify(params(**{"openid.op_endpoint": "https://evil.example/openid/login"}))[0] is None
    assert run_verify(params(**{"openid.mode": "cancel"}))[0] is None


def test_login_session_and_me(monkeypatch):
    async def fake_verify(p, return_to, client=None):
        assert return_to == RETURN_TO
        return 86745912

    async def fake_profile(account_id):
        return {"account_id": account_id, "name": "Рома", "avatar": "", "rank_tier": 80, "found": True}

    async def fake_heroes(account_id, days=180):
        return [{"id": 1, "games": 120, "wins": 66}]

    monkeypatch.setattr(steam, "verify", fake_verify)
    monkeypatch.setattr(main.players, "profile", fake_profile)
    monkeypatch.setattr(main.players, "heroes", fake_heroes)
    client = TestClient(main.app)

    assert client.get("/api/me").json() == {"logged_in": False}
    assert client.get("/api/me/heroes").status_code == 401

    r = client.get("/auth/steam/login", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"].startswith(steam.STEAM_OPENID)
    assert "openid.return_to=http%3A%2F%2Ftestserver%2Fauth%2Fsteam%2Fcallback" in r.headers["location"]

    r = client.get("/auth/steam/callback", params=params(), follow_redirects=False)
    assert r.headers["location"] == "/"
    me = client.get("/api/me").json()
    assert me["logged_in"] and me["name"] == "Рома"
    assert client.get("/api/me/heroes").json()["heroes"][0]["games"] == 120

    client.post("/auth/logout")
    assert client.get("/api/me").json() == {"logged_in": False}


def test_failed_login_redirects_with_flag(monkeypatch):
    async def fake_verify(p, return_to, client=None):
        return None

    monkeypatch.setattr(steam, "verify", fake_verify)
    r = TestClient(main.app).get("/auth/steam/callback", params=params(), follow_redirects=False)
    assert r.headers["location"] == "/?login=failed"
