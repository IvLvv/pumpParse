"""Локальный веб-интерфейс: фоновый сканер + просмотр и фильтрация из браузера."""
import json
import os
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from . import auth as authmod
from . import pool as poolmod
from . import rules as rulemod
from .analysis import profile_creator
from .api import PumpAPI
from .db import Store

STATIC = os.path.join(os.path.dirname(__file__), "static")

# Воркер считает эти окна заранее, поэтому в UI доступны только они.
WINDOWS = (5, 10, 20, 50)


class Worker(threading.Thread):
    """Тянет свежие монеты и профилирует девов в фоне, чтобы страница не ждала сеть."""

    daemon = True

    def __init__(self, db_path, interval=20, limit=70, rps=4.0):
        super().__init__()
        self.db_path, self.interval, self.limit = db_path, interval, limit
        self.api = PumpAPI(rps=rps)
        self.enabled = threading.Event()
        self.enabled.set()
        self.state = {"running": True, "last_poll": None, "new_last_poll": 0,
                      "profiled": 0, "errors": 0, "last_error": None, "busy": False,
                      "pooled": 0}

    def run(self):
        store = Store(self.db_path)          # своё соединение: sqlite не любит межпоточное
        while True:
            self.enabled.wait()
            self.state["busy"] = True
            try:
                fresh = 0
                for c in reversed(self.api.coins(limit=self.limit, sort="created_timestamp")):
                    if store.seen(c["mint"]):
                        continue
                    store.upsert_coin(c)
                    fresh += 1
                    dev = profile_creator(self.api, c["creator"], store, windows=WINDOWS)
                    self.state["profiled"] += 1
                    # Конфиг перечитываем на каждой монете, а не раз в цикл: проход по
                    # странице занимает минуты, и правка фильтра из UI иначе применялась
                    # бы только со следующего круга.
                    cfg = poolmod.normalize(store.get_setting("pool_config", {}))
                    if cfg["enabled"]:
                        ok, why = poolmod.match(c, dev, cfg)
                        if ok and store.pool_add(c["mint"], why):
                            self.state["pooled"] += 1
                self.state["new_last_poll"] = fresh
                self.state["last_poll"] = int(time.time())
            except Exception as e:
                self.state["errors"] += 1
                self.state["last_error"] = f"{type(e).__name__}: {e}"
                traceback.print_exc()
            finally:
                self.state["busy"] = False
            time.sleep(self.interval)

    def toggle(self, on):
        self.state["running"] = bool(on)
        self.enabled.set() if on else self.enabled.clear()


def _apply_rules(rows, rule_texts, require_all):
    """Возвращает (прошедшие, правила, сколько отброшено из-за нехватки данных).

    Профиль дева мог быть посчитан раньше, без нужного окна — такие строки
    правилом не проверить, и они отсеиваются. Считаем их отдельно, чтобы
    в интерфейсе это не выглядело как «условие никого не нашло».
    """
    rules = [rulemod.parse(t) for t in rule_texts if t.strip()]
    if not rules:
        return rows, [], 0
    need = {str(r.window) for r in rules}
    out, stale = [], 0
    for r in rows:
        dev = r.get("dev")
        wins = (dev or {}).get("windows") or {}
        if not dev or not need <= set(wins):
            stale += 1
            continue
        if rulemod.match(rules, wins, require_all=require_all):
            r = dict(r)
            r["matched"] = [{"rule": x.raw, "ok": x.test(wins)} for x in rules]
            out.append(r)
    return out, rules, stale


# Открыты без сессии: страница входа, сам вход и проба живости для деплоя.
PUBLIC = ("/login", "/api/login", "/healthz")


# Тело POST крупнее этого не читаем: маршруты принимают маленькие JSON-объекты,
# а Content-Length клиент назначает сам.
MAX_BODY = 64 * 1024


def make_handler(worker, db_path, auth=None, trust_proxy=False):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass                              # не засорять консоль сканера

        def _send(self, code, body, ctype="application/json; charset=utf-8",
                  headers=()):
            """Всегда возвращает True: вызывающий по этому судит, что ответ ушёл."""
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode()
            elif isinstance(body, str):
                body = body.encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
            except DROPPED:
                self.close_connection = True
            return True

        def _client_ip(self):
            """Адрес, по которому считаются неудачные попытки входа.

            X-Forwarded-For берём, только когда сервис явно объявлен стоящим
            за своим прокси: иначе заголовок подставляет сам клиент и меняет
            его на каждой попытке, обнуляя троттлинг.
            """
            fwd = self.headers.get("X-Forwarded-For") if trust_proxy else None
            return fwd.split(",")[0].strip() if fwd else self.client_address[0]

        def _blocked(self, path):
            """True — ответ уже отправлен, обработку нужно немедленно прекратить.

            Раньше метод возвращал None и на стороне вызова сравнивался с None;
            _send тоже отдавал None, поэтому 401 уходил, а обработчик продолжал
            работать и дописывал в тот же сокет второй, уже полный ответ.
            """
            if auth is None or path in PUBLIC:
                return False
            if auth.user_from_cookies(self.headers.get("Cookie")):
                return False
            if path.startswith("/api/"):
                return self._send(401, {"error": "нужен вход"})
            nxt = quote(self.path, safe="/?=&")
            return self._send(302, b"", "text/html; charset=utf-8",
                              headers=[("Location", f"/login?next={nxt}")])

        def _login(self, data):
            try:
                token = auth.login(data.get("username"), data.get("password"),
                                   self._client_ip())
            except authmod.AuthError as e:
                return self._send(401, {"error": str(e)})
            return self._send(200, {"ok": True, "user": auth.user},
                              headers=[("Set-Cookie", auth.set_cookie_header(token))])

        def _static(self, name):
            with open(os.path.join(STATIC, name), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, {"error": "неверный Content-Length"})
            if not 0 <= n <= MAX_BODY:
                self.close_connection = True
                return self._send(413, {"error": f"тело больше {MAX_BODY} байт"})
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError as e:
                return self._send(400, {"error": f"битый JSON: {e}"})
            if not isinstance(data, dict):
                return self._send(400, {"error": "ожидался объект JSON"})
            if auth is not None:
                if path == "/api/login":
                    return self._login(data)
                if path == "/api/logout":
                    return self._send(200, {"ok": True},
                                      headers=[("Set-Cookie", auth.clear_cookie_header())])
            if self._blocked(path):
                return None
            try:
                return self._routes_post(path, data)
            except ValueError as e:
                return self._send(400, {"error": str(e)})

        def _routes_post(self, path, data):
            if path == "/api/pool/config":
                store = Store(db_path)
                cfg = poolmod.normalize(data)
                store.set_setting("pool_config", cfg)
                return self._send(200, cfg)

            if path == "/api/pool/seen":
                return self._send(200, {"marked": Store(db_path).pool_mark_seen()})

            if path == "/api/pool/clear":
                return self._send(200, {"deleted": Store(db_path).pool_clear()})

            if path == "/api/worker":
                worker.toggle(data.get("running", True))
                if "interval" in data:
                    worker.interval = max(5, int(data["interval"]))
                return self._send(200, worker.state)
            return self._send(404, {"error": "нет такого маршрута"})

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            def one(k, d=None):
                return (q.get(k) or [d])[0]

            try:
                if u.path == "/healthz":
                    return self._send(200, {"ok": True})

                if u.path == "/login":
                    if auth is None:
                        return self._send(302, b"", headers=[("Location", "/")])
                    return self._static("login.html")

                if self._blocked(u.path):
                    return None

                if u.path in ("/", "/index.html"):
                    return self._static("index.html")

                if u.path == "/api/status":
                    st = dict(worker.state)
                    st["auth"] = auth.user if auth else None
                    st["interval"] = worker.interval
                    store = Store(db_path)
                    st["counts"] = store.counts()
                    st["pool_unread"] = store.pool_unread()
                    st["pool_total"] = store.pool_total()
                    st["windows"] = list(WINDOWS)
                    st["presets"] = rulemod.PRESETS
                    st["metrics"] = rulemod.METRICS
                    return self._send(200, st)

                if u.path == "/api/feed":
                    store = Store(db_path)
                    rows = store.feed(int(one("limit", 300)))
                    texts = [t for t in (one("rules", "") or "").split(";") if t.strip()]
                    for name in (one("presets", "") or "").split(","):
                        if name.strip():
                            texts += rulemod.PRESETS.get(name.strip(), [])
                    rows, rules, stale = _apply_rules(
                        rows, texts, one("any", "0") != "1")
                    return self._send(200, {"rows": rows, "total": len(rows),
                                            "stale": stale,
                                            "rules": [r.raw for r in rules]})

                if u.path == "/api/pool":
                    store = Store(db_path)
                    return self._send(200, {
                        "rows": store.pool_rows(int(one("limit", 300))),
                        "unread": store.pool_unread(),
                        "total": store.pool_total(),
                        "config": poolmod.normalize(store.get_setting("pool_config", {})),
                    })

                if u.path == "/api/dev":
                    addr = one("address")
                    if not addr:
                        return self._send(400, {"error": "нужен address"})
                    p = profile_creator(worker.api, addr, Store(db_path),
                                        cache_age=0, windows=WINDOWS,
                                        with_coins=True)
                    return self._send(200, p)

                self._send(404, {"error": "нет такого маршрута"})
            except ValueError as e:
                self._send(400, {"error": str(e)})
            except Exception as e:
                traceback.print_exc()
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

    return Handler


# Браузер закрывает простаивающие keep-alive соединения, а страница опрашивает
# сервер каждые 5-15 секунд — такие разрывы штатны и не должны сыпать трейсбеками,
# иначе за ними не видно настоящих ошибок.
DROPPED = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], DROPPED):
            return
        super().handle_error(request, client_address)


def lan_ips():
    """IPv4-адреса машины, кроме loopback и link-local — чтобы подсказать адрес в сети."""
    out = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith(("127.", "169.254.")) and ip not in out:
                out.append(ip)
    except OSError:
        pass
    return out


def serve(db_path="pump.db", port=8000, interval=20, limit=70, rps=4.0,
          host="127.0.0.1", secure_cookie=None, trust_proxy=None):
    worker = Worker(db_path, interval=interval, limit=limit, rps=rps)
    worker.start()
    # Флаг Secure на cookie ломает вход по http://, поэтому по умолчанию его
    # ставим только там, где снаружи ожидается TLS — то есть не на localhost.
    # За обратным прокси адрес привязки об этом не говорит ничего: сервис
    # слушает петлю, а наружу отдаётся https, поэтому там флаг задают явно.
    if secure_cookie is None:
        env = os.environ.get(authmod.SECURE_ENV)
        if env in ("0", "1"):
            secure_cookie = env == "1"
        else:
            secure_cookie = host not in ("127.0.0.1", "localhost")
    if trust_proxy is None:
        trust_proxy = os.environ.get("PUMPPARSE_TRUST_PROXY", "") == "1"
    auth = authmod.from_env(Store(db_path), secure=secure_cookie)
    srv = QuietServer((host, port),
                      make_handler(worker, db_path, auth, trust_proxy=trust_proxy))
    print(f"интерфейс: http://127.0.0.1:{port}  (БД {db_path}, опрос раз в {interval}s)")
    if host not in ("127.0.0.1", "localhost"):
        for ip in lan_ips():
            print(f"           http://{ip}:{port}  — с других устройств сети")
    if auth:
        print(f"авторизация: включена, пользователь {auth.user}")
    else:
        print(f"ВНИМАНИЕ: интерфейс без пароля — задайте {authmod.HASH_ENV} "
              f"(`pumpparse passwd`), иначе доступ открыт всем, кто видит порт.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлено")
