"""Review UI theme toggle."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_home_includes_theme_toggle():
    page = client.get("/")
    assert page.status_code == 200
    assert "argus-theme-toggle" in page.text
    assert "data-theme" in page.text
    assert "--card-muted" in page.text