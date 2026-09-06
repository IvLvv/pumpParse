"""Сборка настроек в serve(): что именно решает про cookie и прокси.

serve() поднимает сеть и воркер, поэтому проверяем не её саму, а разбор
окружения — ровно ту часть, где легко ошибиться и не заметить на проде.
"""
import pytest

from pumpparse import auth as authmod


def secure_from(env, host, monkeypatch):
    """Повторяет решение serve() про флаг Secure."""
    monkeypatch.delenv(authmod.SECURE_ENV, raising=False)
    if env is not None:
        monkeypatch.setenv(authmod.SECURE_ENV, env)
    import os
    value = os.environ.get(authmod.SECURE_ENV)
    if value in ("0", "1"):
        return value == "1"
    return host not in ("127.0.0.1", "localhost")


class TestSecureCookie:
    @pytest.mark.parametrize("host,expect", [
        ("127.0.0.1", False), ("localhost", False), ("0.0.0.0", True),
    ])
    def test_по_умолчанию_решает_адрес_привязки(self, host, expect, monkeypatch):
        assert secure_from(None, host, monkeypatch) is expect

    def test_за_прокси_флаг_включается_явно(self, monkeypatch):
        """Сервис слушает петлю, а наружу отдаётся https — адрес тут не судья."""
        assert secure_from("1", "127.0.0.1", monkeypatch) is True

    def test_можно_выключить_явно(self, monkeypatch):
        assert secure_from("0", "0.0.0.0", monkeypatch) is False

    @pytest.mark.parametrize("value", ["", "да", "true", "2"])
    def test_мусор_в_переменной_возвращает_к_умолчанию(self, value, monkeypatch):
        assert secure_from(value, "127.0.0.1", monkeypatch) is False
