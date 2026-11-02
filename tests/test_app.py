import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import data, main, scoring
from app.data import Hero, aggregate
from app.recognize import build_references, load_screenshot, match_top_bar

FIXTURES = Path(__file__).parent / "fixtures"
POSITIONS = data._positions_table()


def make_heroes() -> dict[int, Hero]:
    # Портреты в static/heroes названы по npc; берём список героев из таблицы позиций.
    names = json.loads((FIXTURES / "heroes.json").read_text())
    return {int(i): Hero(int(i), n, npc, [], POSITIONS.get(int(i), {1: 1.0})) for i, (n, npc) in names.items()}


@pytest.fixture(scope="module")
def heroes():
    return make_heroes()


def test_top_bar_recognition(heroes):
    refs = build_references(list(heroes.values()))
    im = load_screenshot((FIXTURES / "topbar_1280x720.jpg").read_bytes())
    found = match_top_bar(im, refs)
    assert [h.name for h in found["radiant"]] == ["Anti-Mage", "Vengeful Spirit", "Kunkka", "Dazzle", "Zeus"]
    assert [h.name for h in found["dire"]] == ["Bounty Hunter", "Bristleback", "Bloodseeker", "Bane", "Razor"]


def test_no_top_bar_finds_nothing(heroes):
    from io import BytesIO

    from PIL import Image

    refs = build_references(list(heroes.values()))
    buf = BytesIO()
    Image.open(FIXTURES / "topbar_1280x720.jpg").transpose(Image.Transpose.FLIP_TOP_BOTTOM).save(buf, "JPEG")
    found = match_top_bar(load_screenshot(buf.getvalue()), refs)
    assert found == {"radiant": [], "dire": []}


def synthetic_stats(heroes):
    # В остальных играх все по 50%, но Anti-Mage (1) против Pudge (14) выигрывает 75%.
    matches = []
    t = 1_700_000_000

    def add(r, d, radiant_win):
        matches.append([len(matches), t + len(matches), radiant_win, r, d])

    for i in range(400):
        add([1, 5, 2, 3, 4], [14, 6, 7, 8, 9], i % 4 != 0)
    for i in range(1200):
        add([1, 5, 2, 3, 4], [10, 11, 12, 13, 15], i % 2 == 0)
        add([14, 6, 7, 8, 9], [16, 17, 18, 19, 20], i % 2 == 0)
    return aggregate(heroes, matches)


def test_aggregate_counts(heroes):
    s = synthetic_stats(heroes)
    assert s.games[1] == 1600 and s.wins[1] == 900
    assert s.vs_games[1, 14] == 400 and s.vs_wins[1, 14] == 300
    assert s.vs_wins[14, 1] == 100
    assert s.with_games[1, 5] == 1600 and s.with_games[1, 1] == 0


def test_counter_pick_ranks_first(heroes):
    s = synthetic_stats(heroes)
    w = scoring.Weights()
    recs = scoring.recommend(s, [], [scoring.Slot(14, 4)], {14}, 0, w, limit=60)
    assert recs[0]["id"] == 1
    assert recs[0]["counter"] > 0.05
    # Сглаживание: преимущество меньше «сырых» +20%.
    assert recs[0]["vs"][0]["adv"] < 0.2


def test_api_analyze_and_replace(heroes, monkeypatch):
    s = synthetic_stats(heroes)
    monkeypatch.setattr(main.store, "stats", s)
    client = TestClient(main.app)
    body = {"ally": [{"id": 5, "pos": 5}], "enemy": [{"id": 14, "pos": 4}], "limit": 5}
    r = client.post("/api/analyze", json=body)
    assert r.status_code == 200
    assert r.json()["recommendations"][0]["id"] == 1

    body = {"ally": [{"id": 1, "pos": 1}, {"id": 5, "pos": 5}], "enemy": [{"id": 14, "pos": 4}],
            "replace": {"team": "ally", "index": 0}}
    j = client.post("/api/analyze", json=body).json()
    assert j["current"]["id"] == 1
    assert all(r["pos"] == 1 and "delta" in r for r in j["recommendations"])
    assert client.get("/api/heroes").status_code == 200
    assert client.get("/heroes/antimage.jpg").status_code == 200
    assert client.get("/heroes/..%2Fsecret.jpg").status_code == 404


def test_api_recognize(heroes, monkeypatch):
    monkeypatch.setattr(main.store, "stats", synthetic_stats(heroes))
    client = TestClient(main.app)
    with open(FIXTURES / "topbar_1280x720.jpg", "rb") as f:
        r = client.post("/api/recognize", files={"image": ("s.jpg", f, "image/jpeg")})
    assert r.status_code == 200
    assert r.json()["method"] == "topbar"
    assert len(r.json()["radiant"]) == 5 and len(r.json()["dire"]) == 5
