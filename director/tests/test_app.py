from fastapi.testclient import TestClient

from dungeon_director.app import app

client = TestClient(app)


def test_health_reports_ok():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "dungeon-director"}


def test_health_rejects_non_get():
    assert client.post("/health").status_code == 405


def test_unknown_route_is_404():
    assert client.get("/no-such-route").status_code == 404
