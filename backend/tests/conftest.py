import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Тесты не трогают настоящую базу; вход выключен (его проверяют отдельные тесты, включая AUTH_ENABLED=true)."""
    monkeypatch.setenv("DB_FILE", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUTH_ENABLED", "false")
