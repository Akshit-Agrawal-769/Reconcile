from api.app import app_from_env, create_app
from api.tests.asgi import Client
from api.tests.conftest import settings_for


def test_serves_web_app_and_keeps_api_json(tmp_path, test_db_url):
    (tmp_path / "index.html").write_text("<!doctype html><title>Reconcile</title>")
    (tmp_path / "app.js").write_text("console.log(1)")
    c = Client(create_app(settings_for(test_db_url).__class__(
        database_url=test_db_url, secret="s", static_dir=str(tmp_path))))
    r = c.get("/")
    assert r.status_code == 200 and "<title>Reconcile</title>" in r.text
    assert c.get("/app.js").text == "console.log(1)"
    r = c.get("/api/nope")
    assert (r.status_code, r.json()) == (404, {"error": "not found"})


def test_missing_static_dir_does_not_break_api(tmp_path, test_db_url):
    s = settings_for(test_db_url).__class__(database_url=test_db_url, secret="s",
                                            static_dir=str(tmp_path / "missing"))
    c = Client(create_app(s))
    assert c.get("/api/me").status_code == 401


def test_app_from_env(monkeypatch, test_db_url, tmp_path):
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("REC_SECRET", "x")
    monkeypatch.setenv("REC_STATIC_DIR", str(tmp_path))
    app = app_from_env()
    assert app.state.settings.static_dir == str(tmp_path)
    monkeypatch.delenv("REC_SECRET")
    try:
        app_from_env()
    except RuntimeError as e:
        assert "REC_SECRET" in str(e)
    else:
        raise AssertionError("expected RuntimeError")
