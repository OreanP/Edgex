"""Real Streamlit AppTest; skipped only when optional UI dependency is absent."""
import socket
from pathlib import Path
import pytest

pytest.importorskip('streamlit')
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / 'portfolio_app.py'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('MANIFOLD_API_KEY', raising=False)
    def denied(*args, **kwargs):
        raise AssertionError('No network in UI tests')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    # Never load a developer's .env while testing.
    monkeypatch.setattr('dotenv.load_dotenv', lambda *a, **k: None)


def test_ui_opens_without_keys():
    app = AppTest.from_file(str(APP)).run(timeout=20)
    assert not app.exception
    assert app.title[0].value == 'EdgeX · Portefeuille'


def test_ui_builds_demo_without_network():
    app = AppTest.from_file(str(APP)).run(timeout=20)
    app.button[0].click().run(timeout=20)
    assert not app.exception
    assert len(app.metric) >= 4
    assert any('DÉMONSTRATION' in w.value for w in app.warning)


def test_ui_live_mode_requires_explicit_confirmation():
    app = AppTest.from_file(str(APP)).run(timeout=20)
    app.radio[0].set_value('Manifold + OpenAI')
    app.button[0].click().run(timeout=20)
    assert not app.exception
    assert any('Confirme' in e.value for e in app.error)
