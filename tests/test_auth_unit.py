"""Модуль авторизации: хэш пароля, подпись сессии, троттлинг."""
import time

import pytest

from pumpparse import auth as authmod
from pumpparse.auth import Auth, AuthError, hash_password, parse_cookies, verify_password

from .conftest import FAST_ITERS, PASSWORD, USER


class TestPasswordHash:
    def test_верный_пароль_проходит(self, password_hash):
        assert verify_password(PASSWORD, password_hash)

    def test_неверный_не_проходит(self, password_hash):
        assert not verify_password("hunter23", password_hash)
        assert not verify_password("", password_hash)

    def test_соль_разная_у_одинаковых_паролей(self):
        a = hash_password(PASSWORD, iterations=FAST_ITERS)
        b = hash_password(PASSWORD, iterations=FAST_ITERS)
        assert a != b
        assert verify_password(PASSWORD, a) and verify_password(PASSWORD, b)

    def test_число_итераций_записано_в_хэше(self):
        assert hash_password("x", iterations=1234).split("$")[1] == "1234"

    @pytest.mark.parametrize("stored", [
        "", "мусор", "pbkdf2_sha256$нечисло$aa$bb", "pbkdf2_sha256$1000$zz$bb",
        "md5$1000$aa$bb", "pbkdf2_sha256$1000$aa", None, 42,
    ])
    def test_битый_хэш_не_валит_проверку(self, stored):
        assert verify_password("x", stored) is False

    def test_пароль_в_юникоде(self):
        h = hash_password("пароль-ёж-🔑", iterations=FAST_ITERS)
        assert verify_password("пароль-ёж-🔑", h)
        assert not verify_password("пароль-ёж", h)


class TestToken:
    def test_свой_токен_принимается(self, auth):
        assert auth.check_token(auth.make_token(USER)) == USER

    def test_подделка_подписи_отвергается(self, auth):
        payload = auth.make_token(USER).split(".")[0]
        assert auth.check_token(payload + ".подпись") is None

    def test_чужой_ключ_не_подходит(self, auth, password_hash):
        other = Auth("другой-ключ", USER, password_hash, secure=False)
        assert auth.check_token(other.make_token(USER)) is None

    def test_смена_ключа_разлогинивает(self, auth):
        token = auth.make_token(USER)
        auth.secret = "новый-ключ".encode()
        assert auth.check_token(token) is None

    def test_просроченный_отвергается(self, auth):
        auth.ttl = -1
        assert auth.check_token(auth.make_token(USER)) is None

    def test_токен_другого_пользователя_отвергается(self, auth):
        assert auth.check_token(auth.make_token("другой")) is None

    @pytest.mark.parametrize("token", [
        "", None, "точек.нет.совсем", "безточки", ".", "..",
        "a.b", "!!!.???", "eyJ1IjogImFkbWluIn0",
    ])
    def test_мусор_не_проходит(self, auth, token):
        assert auth.check_token(token) is None

    def test_срок_жизни_учитывается(self, auth):
        auth.ttl = 3600
        token = auth.make_token(USER)
        assert auth.check_token(token) == USER
        assert time.time() < 2 ** 31


class TestCookies:
    @pytest.mark.parametrize("header,expect", [
        ("pp_session=abc", {"pp_session": "abc"}),
        ("  pp_session = abc ; other=1", {"pp_session": "abc", "other": "1"}),
        ("", {}),
        (None, {}),
        ("сломанный", {"сломанный": ""}),
        # Имя-надстройка не должно подменять настоящую cookie.
        ("xpp_session=подделка", {"xpp_session": "подделка"}),
    ])
    def test_разбор(self, header, expect):
        assert parse_cookies(header) == expect

    def test_похожее_имя_не_принимается_за_сессию(self, auth):
        token = auth.make_token(USER)
        assert auth.user_from_cookies(f"not_pp_session={token}") is None
        assert auth.user_from_cookies(f"pp_session={token}") == USER

    def test_флаги_безопасности(self, auth):
        auth.secure = True
        h = auth.set_cookie_header("t")
        assert "HttpOnly" in h and "SameSite=Lax" in h and "Secure" in h

    def test_без_tls_флаг_secure_снят(self, auth):
        assert "Secure" not in auth.set_cookie_header("t")

    def test_сброс_cookie_гасит_сессию(self, auth):
        assert "Max-Age=0" in auth.clear_cookie_header()


class TestThrottle:
    def test_блокировка_после_лимита(self, auth):
        for _ in range(auth.MAX_FAILS):
            with pytest.raises(AuthError, match="неверный"):
                auth.login(USER, "мимо", ip="1.2.3.4")
        with pytest.raises(AuthError, match="слишком много"):
            auth.login(USER, PASSWORD, ip="1.2.3.4")

    def test_блокировка_только_своего_адреса(self, auth):
        for _ in range(auth.MAX_FAILS):
            with pytest.raises(AuthError):
                auth.login(USER, "мимо", ip="1.2.3.4")
        assert auth.login(USER, PASSWORD, ip="5.6.7.8")

    def test_успех_обнуляет_счётчик(self, auth):
        for _ in range(auth.MAX_FAILS - 1):
            with pytest.raises(AuthError):
                auth.login(USER, "мимо", ip="1.2.3.4")
        assert auth.login(USER, PASSWORD, ip="1.2.3.4")
        with pytest.raises(AuthError, match="неверный"):
            auth.login(USER, "мимо", ip="1.2.3.4")

    def test_блокировка_истекает(self, auth):
        for _ in range(auth.MAX_FAILS):
            with pytest.raises(AuthError):
                auth.login(USER, "мимо", ip="1.2.3.4")
        auth._fails["1.2.3.4"] = (auth.MAX_FAILS, time.time() - auth.LOCK_SEC - 1)
        assert auth.login(USER, PASSWORD, ip="1.2.3.4")

    def test_таблица_неудач_не_растёт_бесконечно(self, auth):
        for i in range(auth.MAX_TRACKED + 500):
            with pytest.raises(AuthError):
                auth.login(USER, "мимо", ip=f"10.0.{i // 256}.{i % 256}")
        assert len(auth._fails) <= auth.MAX_TRACKED

    def test_неверный_логин_тоже_блокируется(self, auth):
        for _ in range(auth.MAX_FAILS):
            with pytest.raises(AuthError):
                auth.login("кто-то", PASSWORD, ip="1.2.3.4")
        with pytest.raises(AuthError, match="слишком много"):
            auth.login(USER, PASSWORD, ip="1.2.3.4")


class TestFromEnv:
    def test_без_хэша_авторизация_выключена(self, monkeypatch, tmp_path):
        from pumpparse.db import Store
        monkeypatch.delenv(authmod.HASH_ENV, raising=False)
        assert authmod.from_env(Store(str(tmp_path / "a.db"))) is None

    def test_секрет_переживает_перезапуск(self, monkeypatch, tmp_path, password_hash):
        from pumpparse.db import Store
        monkeypatch.delenv(authmod.SECRET_ENV, raising=False)
        monkeypatch.setenv(authmod.HASH_ENV, password_hash)
        path = str(tmp_path / "b.db")
        first = authmod.from_env(Store(path))
        token = first.make_token(first.user)
        # Второй запуск сервиса: та же БД, тот же ключ, сессия жива.
        assert authmod.from_env(Store(path)).check_token(token) == first.user

    def test_секрет_из_окружения_важнее_бд(self, monkeypatch, tmp_path, password_hash):
        from pumpparse.db import Store
        monkeypatch.setenv(authmod.HASH_ENV, password_hash)
        monkeypatch.setenv(authmod.SECRET_ENV, "из-окружения")
        assert authmod.from_env(Store(str(tmp_path / "c.db"))).secret == "из-окружения".encode()

    def test_логин_по_умолчанию(self, monkeypatch, tmp_path, password_hash):
        from pumpparse.db import Store
        monkeypatch.delenv(authmod.USER_ENV, raising=False)
        monkeypatch.setenv(authmod.HASH_ENV, password_hash)
        assert authmod.from_env(Store(str(tmp_path / "d.db"))).user == "admin"
