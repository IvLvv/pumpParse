"""Гейт авторизации на уровне HTTP: что закрыто, что открыто и чем это обходят."""
import re
import socket
from pathlib import Path

import pytest

from .conftest import PASSWORD, USER

# Всё, что за авторизацией. /api/dev не берём: он ходит в сеть.
PROTECTED = ["/", "/index.html", "/api/status", "/api/feed", "/api/pool"]
OPEN_PATHS = ["/healthz", "/login"]

LOGIN_HTML = Path(__file__).resolve().parent.parent / "pumpparse" / "static" / "login.html"


def _addr(client):
    return ("127.0.0.1", client.port)


def _complete(buf):
    """Пришёл ли ответ целиком — по заголовкам и Content-Length."""
    head, sep, body = buf.partition(b"\r\n\r\n")
    if not sep:
        return False
    m = re.search(rb"Content-Length:\s*(\d+)", head, re.I)
    return m is not None and len(body) >= int(m.group(1))


def _read(conn, limit=8192, drain=False):
    """Читает ответ из сокета.

    По умолчанию останавливается на первом полном ответе: сервер держит
    keep-alive, и иначе каждое чтение упиралось бы в таймаут. drain=True
    дочитывает всё до закрытия — так ловится лишний второй ответ.
    """
    buf = b""
    try:
        while len(buf) < limit:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            if not drain and _complete(buf):
                break
    except (TimeoutError, OSError):
        pass
    return buf


class TestGate:
    @pytest.mark.parametrize("path", PROTECTED)
    def test_без_сессии_не_пускает(self, server, path):
        r = server.get(path)
        assert r.status in (401, 302)
        assert b"creators" not in r.raw
        assert b"pump<span>" not in r.raw

    @pytest.mark.parametrize("path", OPEN_PATHS)
    def test_публичное_открыто(self, server, path):
        assert server.get(path).status == 200

    def test_страница_уводит_на_вход_с_возвратом(self, server):
        r = server.get("/index.html")
        assert r.status == 302
        assert r.headers["Location"] == "/login?next=/index.html"

    def test_api_отвечает_кодом_а_не_редиректом(self, server):
        r = server.get("/api/status")
        assert r.status == 401
        assert r.json["error"]

    @pytest.mark.parametrize("path", ["/api/pool/seen", "/api/pool/clear",
                                      "/api/worker", "/api/pool/config"])
    def test_post_маршруты_закрыты(self, server, path):
        assert server.post(path, {}).status == 401

    def test_воркер_не_трогается_без_входа(self, server, worker):
        server.post("/api/worker", {"running": False})
        assert worker.toggled == []
        assert worker.state["running"] is True

    def test_без_авторизации_всё_открыто(self, open_server):
        assert open_server.get("/api/status").status == 200
        assert open_server.get("/").status == 200

    def test_login_редиректит_когда_авторизации_нет(self, open_server):
        r = open_server.get("/login")
        assert r.status == 302
        assert r.headers["Location"] == "/"

    def test_в_статусе_видно_включена_ли_авторизация(self, open_server):
        assert open_server.get("/api/status").json["auth"] is None


class TestLoginFlow:
    def test_вход_и_доступ(self, server):
        r = server.login()
        assert r.status == 200
        assert r.json["user"] == USER
        assert server.get("/api/status").json["auth"] == USER
        assert server.get("/").status == 200

    def test_cookie_защищена_флагами(self, server):
        ck = server.login().headers["Set-Cookie"]
        assert "HttpOnly" in ck
        assert "SameSite=Lax" in ck
        assert "Path=/" in ck

    @pytest.mark.parametrize("user,pw", [
        (USER, "мимо"),
        ("чужой", PASSWORD),
        ("", ""),
        (USER, ""),
        (USER, PASSWORD + " "),
        (USER.upper(), PASSWORD),
    ])
    def test_неверные_данные(self, server, user, pw):
        assert server.post("/api/login", {"username": user, "password": pw}).status == 401

    def test_не_ascii_логин_не_валит_сервер(self, server):
        """compare_digest на строках с кириллицей бросал TypeError → 500."""
        r = server.post("/api/login", {"username": "админ", "password": PASSWORD})
        assert r.status == 401

    @pytest.mark.parametrize("body", [
        {},
        {"username": USER},
        {"password": PASSWORD},
        {"username": None, "password": None},
        {"username": 1, "password": 2},
        {"username": ["список"], "password": {"словарь": 1}},
    ])
    def test_кривое_тело_даёт_отказ_а_не_500(self, server, body):
        """Нестроковые поля роняли .encode() внутри сравнения логина."""
        assert server.post("/api/login", body).status in (400, 401)

    def test_выход_гасит_cookie(self, server):
        server.login()
        r = server.post("/api/logout")
        assert r.status == 200
        assert "Max-Age=0" in r.headers["Set-Cookie"]

    def test_выход_не_отзывает_сам_токен(self, server):
        """Осознанный размен: сессии нигде не хранятся, отзывать нечего.

        Выход чистит cookie в браузере, но уже украденный токен остаётся
        валидным до истечения срока. Отозвать всё разом можно только сменой
        PUMPPARSE_SECRET. Фиксируем поведение явно, чтобы оно не выглядело
        как недосмотр.
        """
        server.login()
        stolen = server.cookie
        server.post("/api/logout")
        assert server.get("/api/status", cookie=stolen).status == 200

    def test_выход_доступен_без_входа(self, server):
        assert server.post("/api/logout").status == 200


class TestForgedSessions:
    @pytest.mark.parametrize("cookie", [
        "pp_session=forged",
        "pp_session=eyJ1IjogImFkbWluIiwgImV4cCI6IDk5OTk5OTk5OTl9.sig",
        "pp_session=",
        "pp_session=.",
        "pp_session=a.b.c.d",
        "PP_SESSION=x",
        "pp_sessionx=x",
        "junk",
    ])
    def test_поддельная_cookie_не_пускает(self, server, cookie):
        assert server.get("/api/status", cookie=cookie).status == 401

    def test_cookie_с_не_ascii_не_пускает(self, server):
        """Такое не пошлёт браузер, но пошлёт кто угодно сырым сокетом."""
        conn = socket.create_connection(_addr(server), timeout=5)
        conn.sendall("GET /api/status HTTP/1.1\r\nHost: x\r\n"
                     "Cookie: pp_session=подделка\r\n\r\n".encode())
        buf = _read(conn)
        conn.close()
        assert b"401" in buf
        assert b"creators" not in buf

    def test_токен_без_подписи_не_проходит(self, server, auth):
        payload = auth.make_token(USER).split(".")[0]
        assert server.get("/api/status", cookie=f"pp_session={payload}").status == 401

    def test_обрезанная_подпись_не_проходит(self, server, auth):
        payload, sig = auth.make_token(USER).split(".")
        assert server.get("/api/status", cookie=f"pp_session={payload}.{sig[:10]}").status == 401

    def test_чужой_ключ_не_пускает(self, server, password_hash):
        from pumpparse.auth import Auth
        alien = Auth("не-тот-ключ", USER, password_hash, secure=False)
        token = alien.make_token(USER)
        assert server.get("/api/status", cookie=f"pp_session={token}").status == 401

    def test_имя_надстройка_не_принимается_за_сессию(self, server, auth):
        token = auth.make_token(USER)
        assert server.get("/api/status", cookie=f"xpp_session={token}").status == 401


class TestThrottleOverHttp:
    def test_блокировка_срабатывает(self, server):
        for _ in range(5):
            server.post("/api/login", {"username": USER, "password": "мимо"})
        r = server.post("/api/login", {"username": USER, "password": PASSWORD})
        assert r.status == 401
        assert "слишком много" in r.json["error"]

    def test_подделка_xff_не_обходит_лимит(self, server):
        """Без доверенного прокси заголовок клиента игнорируется."""
        for i in range(5):
            server.post("/api/login", {"username": USER, "password": "мимо"},
                        headers={"X-Forwarded-For": f"9.9.9.{i}"})
        r = server.post("/api/login", {"username": USER, "password": PASSWORD},
                        headers={"X-Forwarded-For": "9.9.9.100"})
        assert r.status == 401
        assert "слишком много" in r.json["error"]

    def test_за_прокси_адреса_считаются_раздельно(self, proxied_server):
        for _ in range(5):
            proxied_server.post("/api/login", {"username": USER, "password": "мимо"},
                                headers={"X-Forwarded-For": "9.9.9.1"})
        # Сосед по прокси не должен страдать за чужие попытки.
        r = proxied_server.post("/api/login", {"username": USER, "password": PASSWORD},
                                headers={"X-Forwarded-For": "9.9.9.2"})
        assert r.status == 200

    def test_не_ascii_логин_считается_в_лимит(self, server):
        """Раньше такая попытка падала в 500 мимо счётчика — PBKDF2 без лимита."""
        for _ in range(5):
            server.post("/api/login", {"username": "админ", "password": PASSWORD})
        r = server.post("/api/login", {"username": USER, "password": PASSWORD})
        assert "слишком много" in r.json["error"]


class TestMalformedRequests:
    def test_огромное_тело_отбивается(self, server):
        """Тело не вычитывается, соединение рвётся — ответ ловим сокетом.

        Снаружи такое режет ещё nginx (client_max_body_size); проверка здесь
        нужна на случай прямого доступа к порту.
        """
        conn = socket.create_connection(_addr(server), timeout=5)
        body = b"x" * (128 * 1024)
        conn.sendall(b"POST /api/login HTTP/1.1\r\nHost: x\r\nContent-Length: "
                     + str(len(body)).encode() + b"\r\n\r\n")
        assert b"413" in _read(conn)
        conn.close()

    def test_врущий_content_length_не_вешает_сервер(self, server):
        conn = socket.create_connection(_addr(server), timeout=5)
        conn.sendall(b"POST /api/login HTTP/1.1\r\nHost: x\r\n"
                     b"Content-Length: 99999999\r\n\r\n{}")
        assert b"413" in _read(conn)
        conn.close()
        assert server.get("/healthz").status == 200

    @pytest.mark.parametrize("value", [b"-1", b"abc", b"1 1"])
    def test_кривой_content_length(self, server, value):
        conn = socket.create_connection(_addr(server), timeout=5)
        conn.sendall(b"POST /api/login HTTP/1.1\r\nHost: x\r\nContent-Length: "
                     + value + b"\r\n\r\n")
        head = _read(conn)
        assert b"400" in head or b"413" in head
        conn.close()

    def test_битый_json(self, server):
        r = server.post("/api/login", "{не json".encode())
        assert r.status == 400
        assert "JSON" in r.json["error"]

    @pytest.mark.parametrize("body", [b"[1,2,3]", '"строка"'.encode(), b"42", b"null"])
    def test_json_не_объект(self, server, body):
        assert server.post("/api/login", body).status == 400

    def test_неизвестный_маршрут_под_сессией(self, server):
        server.login()
        assert server.get("/api/no-such-route").status == 404
        assert server.post("/api/no-such-route", {}).status == 404

    def test_несуществующий_маршрут_не_раскрывается_без_входа(self, server):
        # 404 вместо 401 подсказал бы, какие маршруты вообще есть.
        assert server.get("/api/secret-endpoint").status == 401


class TestPathTricks:
    @pytest.mark.parametrize("path", [
        "/API/status", "/api/status/", "//api/status", "/api//status",
        "/./api/status", "/api/../api/status", "/%61pi/status",
        "/api/status?x=1", "/api/status%00",
        "/static/index.html", "/../pump.db", "/index.html/../index.html",
    ])
    def test_обходные_пути_не_отдают_данных(self, server, path):
        r = server.get(path)
        assert r.status in (401, 302, 404), f"{path} -> {r.status}"
        assert b"creators" not in r.raw

    @pytest.mark.parametrize("method", ["HEAD", "PUT", "DELETE", "OPTIONS", "PATCH"])
    def test_прочие_методы_не_отдают_данных(self, server, method):
        r = server.request(method, "/api/status")
        assert r.status in (400, 401, 403, 404, 405, 501)
        assert b"creators" not in r.raw


class TestResponseLeak:
    def test_один_запрос_один_ответ(self, server):
        """Регрессия: гейт отправлял 401 и продолжал обработку.

        _send возвращал None, проверка сравнивала результат с None — и следом
        за 401 в тот же keep-alive сокет дописывался полный ответ с данными.
        """
        conn = socket.create_connection(_addr(server), timeout=0.7)
        conn.sendall(b"GET /api/status HTTP/1.1\r\nHost: x\r\n\r\n")
        buf = _read(conn, 16384, drain=True)
        conn.close()
        assert buf.count(b"HTTP/1.1 ") == 1, "в сокет ушёл лишний ответ"
        assert b"creators" not in buf

    def test_конвейер_из_двух_запросов(self, server):
        conn = socket.create_connection(_addr(server), timeout=1.5)
        conn.sendall(b"GET /api/status HTTP/1.1\r\nHost: x\r\n\r\n"
                     b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        buf = _read(conn, 16384, drain=True)
        conn.close()
        assert b"creators" not in buf
        assert buf.count(b"401") == 1

    def test_в_ответах_нет_секретов(self, server, auth):
        server.login()
        for path in ("/api/status", "/api/feed", "/api/pool"):
            body = server.get(path).raw
            assert auth.password_hash.encode() not in body
            assert b"pbkdf2" not in body
            assert auth.secret not in body


class TestRedirectTarget:
    """Возврат после входа. Сам переход делает JS, поэтому проверяем обе стороны."""

    @pytest.mark.parametrize("path", ["/index.html", "/api/feed"])
    def test_сервер_кладёт_в_next_только_свой_путь(self, server, path):
        r = server.get(path)
        if r.status == 302:
            assert r.headers["Location"] == f"/login?next={path}"

    def test_заголовок_location_без_переноса_строк(self, server):
        r = server.get("/index.html")
        assert "\r" not in r.headers["Location"]
        assert "\n" not in r.headers["Location"]

    def test_страница_входа_сверяет_origin(self):
        """Регрессия: проверка префикса строкой пропускала '/\\evil.com'.

        Браузер в http(s)-адресах считает обратный слэш обычным разделителем,
        поэтому такой next уезжал на чужой хост. Разбор через URL + сверка
        origin — единственная надёжная проверка, и подменять её обратно
        регэкспом нельзя.
        """
        src = LOGIN_HTML.read_text(encoding="utf-8")
        assert "u.origin !== location.origin" in src
        assert not re.search(r"next.*startsWith\('/'\)", src)
        assert "/^\\/[^\\/]/" not in src
