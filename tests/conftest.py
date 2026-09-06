"""Общая обвязка: поднятый в процессе сервер и клиент к нему.

Сервер настоящий (тот же QuietServer, что и на проде) — часть проверок
касается разбора HTTP, и подменять его заглушкой было бы бессмысленно.
"""
import http.client
import json
import threading
from pathlib import Path

import pytest

from pumpparse import auth as authmod
from pumpparse import web

# PBKDF2 на боевых 600k итераций превращает набор тестов в минуты ожидания.
# Стойкость хэша здесь не проверяется, проверяется логика вокруг него.
ROOT = Path(__file__).resolve().parent.parent

FAST_ITERS = 1000
PASSWORD = "hunter22"
USER = "admin"


class FakeWorker:
    """Воркер без сети и без потока: обработчику нужно только его состояние."""

    def __init__(self):
        self.state = {"running": True, "last_poll": None, "new_last_poll": 0,
                      "profiled": 0, "errors": 0, "last_error": None,
                      "busy": False, "pooled": 0}
        self.interval = 20
        self.api = None
        self.toggled = []

    def toggle(self, on):
        self.toggled.append(bool(on))
        self.state["running"] = bool(on)


class Client:
    """Тонкая обёртка над http.client: нужен контроль над сырым запросом."""

    def __init__(self, port):
        self.port = port
        self.cookie = None

    def request(self, method, path, body=None, headers=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = dict(headers or {})
        ck = cookie if cookie is not None else self.cookie
        if ck:
            h["Cookie"] = ck
        payload = None
        if body is not None:
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        try:
            conn.request(method, path, body=payload, headers=h)
            r = conn.getresponse()
            return Response(r.status, r.read(), dict(r.getheaders()))
        finally:
            conn.close()

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body=body if body is not None else {}, **kw)

    def login(self, user=USER, password=PASSWORD, **kw):
        r = self.post("/api/login", {"username": user, "password": password}, **kw)
        if r.status == 200:
            self.cookie = r.headers["Set-Cookie"].split(";")[0]
        return r


class Response:
    def __init__(self, status, raw, headers):
        self.status, self.raw, self.headers = status, raw, headers

    @property
    def json(self):
        return json.loads(self.raw)

    @property
    def text(self):
        return self.raw.decode("utf-8", "replace")


def _serve(handler):
    srv = web.QuietServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def password_hash():
    return authmod.hash_password(PASSWORD, iterations=FAST_ITERS)


@pytest.fixture
def auth(password_hash):
    return authmod.Auth("test-secret", USER, password_hash, secure=False)


@pytest.fixture
def worker():
    return FakeWorker()


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def server(worker, db, auth):
    """Сервер с включённой авторизацией."""
    srv = _serve(web.make_handler(worker, db, auth))
    yield Client(srv.server_address[1])
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def open_server(worker, db):
    """Сервер без авторизации — режим локального запуска."""
    srv = _serve(web.make_handler(worker, db, None))
    yield Client(srv.server_address[1])
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def proxied_server(worker, db, auth):
    """Сервер, объявленный стоящим за доверенным прокси."""
    srv = _serve(web.make_handler(worker, db, auth, trust_proxy=True))
    yield Client(srv.server_address[1])
    srv.shutdown()
    srv.server_close()
