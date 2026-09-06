"""Авторизация веб-интерфейса: пароль из окружения + подписанная cookie-сессия.

Пароль в открытом виде на сервере не лежит: в окружении держим только PBKDF2-хэш,
он же генерируется командой `pumpparse passwd`. Сессия — не запись в БД, а
подписанный HMAC токен в cookie: сервер однопроцессный и перезапускается при
каждой выкладке, а переживать перезапуск сессия должна.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

USER_ENV = "PUMPPARSE_USER"
HASH_ENV = "PUMPPARSE_PASSWORD_HASH"
SECRET_ENV = "PUMPPARSE_SECRET"   # noqa: S105 — имя переменной окружения, не пароль
SECURE_ENV = "PUMPPARSE_SECURE_COOKIE"

COOKIE = "pp_session"
DEFAULT_TTL = 14 * 24 * 3600

# Стоимость подбора пароля. Меняется свободно: итерации записаны в самом хэше,
# старые хэши продолжают проверяться своим числом итераций.
ITERATIONS = 600_000


def hash_password(password, iterations=ITERATIONS):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password, stored):
    try:
        algo, iters, salt, want = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt), int(iters))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(dk.hex(), want)


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class AuthError(Exception):
    """Вход не удался; текст показывается пользователю как есть."""


class Auth:
    """Проверка пароля и выдача/разбор сессионных cookie.

    secret — ключ подписи. Он же инвалидирует все сессии разом: сменили ключ,
    все выданные токены перестали проверяться.
    """

    # Защита от перебора: после стольких неудач с одного адреса вход
    # блокируется на LOCK_SEC. Счётчик обнуляется при успешном входе.
    MAX_FAILS = 5
    LOCK_SEC = 300

    # Потолок на размер таблицы неудач: иначе перебор с подставных адресов
    # (или просто из-за прокси) раздувает её без границы. При переполнении
    # выбрасываем самые старые записи — их блокировка всё равно истекла.
    MAX_TRACKED = 4096

    def __init__(self, secret, user, password_hash, ttl=DEFAULT_TTL, secure=True):
        self.secret = secret.encode() if isinstance(secret, str) else secret
        self.user = user
        self.password_hash = password_hash
        self.ttl = ttl
        self.secure = secure
        self._fails = {}                     # ip -> (число неудач, время последней)

    # --- пароль ---

    def _throttle(self, ip):
        fails, last = self._fails.get(ip, (0, 0.0))
        left = self.LOCK_SEC - (time.time() - last)
        if fails >= self.MAX_FAILS and left > 0:
            raise AuthError(f"слишком много попыток, подождите {int(left)}с")

    def _remember_fail(self, ip):
        if len(self._fails) >= self.MAX_TRACKED and ip not in self._fails:
            for old in sorted(self._fails, key=lambda k: self._fails[k][1])[:self.MAX_TRACKED // 4]:
                del self._fails[old]
        self._fails[ip] = (self._fails.get(ip, (0, 0.0))[0] + 1, time.time())

    def login(self, user, password, ip="-"):
        """Возвращает значение cookie либо бросает AuthError."""
        self._throttle(ip)
        # Поля приходят из разобранного JSON и строками быть не обязаны:
        # {"username": 1} раньше валил .encode() и обработчик отдавал 500.
        if not isinstance(user, str) or not isinstance(password, str):
            self._remember_fail(ip)
            raise AuthError("неверный логин или пароль")
        # Хэш считаем всегда, даже когда логин заведомо не тот: иначе неверный
        # логин отвечает мгновенно, а верный — через PBKDF2, и по времени
        # ответа логин перебирается.
        pw_ok = verify_password(password or "", self.password_hash)
        # Сравниваем байты: compare_digest на строках с не-ASCII бросает
        # TypeError, а логин с кириллицей вводится случайно и легко. Раньше
        # такая попытка давала 500 и не попадала в счётчик неудач, то есть
        # перебор упирался не в лимит, а только в скорость PBKDF2.
        user_ok = hmac.compare_digest((user or "").encode(), self.user.encode())
        if not (pw_ok and user_ok):
            self._remember_fail(ip)
            raise AuthError("неверный логин или пароль")
        self._fails.pop(ip, None)
        return self.make_token(self.user)

    # --- сессия ---

    def make_token(self, user):
        payload = _b64(json.dumps({"u": user, "exp": int(time.time() + self.ttl)}).encode())
        sig = _b64(hmac.new(self.secret, payload.encode(), hashlib.sha256).digest())
        return f"{payload}.{sig}"

    def check_token(self, token):
        """Имя пользователя из валидного токена, иначе None."""
        try:
            payload, sig = (token or "").split(".", 1)
            want = _b64(hmac.new(self.secret, payload.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(sig, want):
                return None
            data = json.loads(_unb64(payload))
        except (ValueError, TypeError, json.JSONDecodeError):
            return None
        if data.get("exp", 0) < time.time() or data.get("u") != self.user:
            return None
        return data["u"]

    def user_from_cookies(self, cookie_header):
        return self.check_token(parse_cookies(cookie_header).get(COOKIE))

    def set_cookie_header(self, token):
        parts = [f"{COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Lax",
                 f"Max-Age={self.ttl}"]
        if self.secure:
            parts.append("Secure")
        return "; ".join(parts)

    def clear_cookie_header(self):
        parts = [f"{COOKIE}=", "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0"]
        if self.secure:
            parts.append("Secure")
        return "; ".join(parts)


def parse_cookies(header):
    out = {}
    for chunk in (header or "").split(";"):
        name, _, value = chunk.partition("=")
        if name.strip():
            out[name.strip()] = value.strip()
    return out


def secret_from(store):
    """Ключ подписи: из окружения, иначе разовая генерация с сохранением в БД.

    В окружении он предпочтительнее — тогда сессии переживают пересоздание БД.
    """
    env = os.environ.get(SECRET_ENV)
    if env:
        return env
    saved = store.get_setting("auth_secret")
    if not saved:
        saved = secrets.token_hex(32)
        store.set_setting("auth_secret", saved)
    return saved


def from_env(store, ttl=DEFAULT_TTL, secure=True):
    """Собирает Auth из переменных окружения. None — авторизация выключена."""
    pw_hash = os.environ.get(HASH_ENV)
    if not pw_hash:
        return None
    return Auth(secret_from(store), os.environ.get(USER_ENV, "admin"),
                pw_hash, ttl=ttl, secure=secure)
