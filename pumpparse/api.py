"""Тонкий клиент публичного API pump.fun (без ключей, без авторизации)."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

FRONTEND = "https://frontend-api-v3.pump.fun"
SWAP = "https://swap-api.pump.fun"

# Сервер режет страницу на 70 записей независимо от запрошенного limit.
PAGE_MAX = 70

# Solana mainnet-beta (genesis hash). Работаем только с этой сетью.
# Серверного фильтра по сети нет — параметры chain/chainId молча игнорируются,
# как и ?user=, поэтому отсекаем на клиенте.
SOLANA_CHAIN_ID = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"


def is_solana(coin):
    return (coin or {}).get("chain_id") == SOLANA_CHAIN_ID

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Origin": "https://pump.fun",
    "Referer": "https://pump.fun/",
}


class PumpAPI:
    def __init__(self, rps=4.0, timeout=20, retries=4):
        self.min_interval = 1.0 / rps
        self.timeout = timeout
        self.retries = retries
        self._last = 0.0
        self.skipped_foreign = 0   # отброшено записей чужих сетей

    def _get(self, url, params=None):
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        for attempt in range(self.retries):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                # Схема всегда https: url собирается из констант FRONTEND/SWAP,
                # снаружи приходят только значения параметров.
                req = urllib.request.Request(url, headers=HEADERS)  # noqa: S310
                with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code not in (408, 429, 500, 502, 503, 504, 530):
                    raise
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                pass
            time.sleep(2 ** attempt)
        raise RuntimeError("pump.fun API недоступен: " + url)

    # --- монеты ---

    def coins(self, offset=0, limit=PAGE_MAX, sort="created_timestamp",
              order="DESC", include_nsfw=True, complete=None, creator=None):
        p = {"offset": offset, "limit": min(limit, PAGE_MAX), "sort": sort,
             "order": order, "includeNsfw": str(include_nsfw).lower()}
        if complete is not None:
            p["complete"] = str(complete).lower()
        if creator:
            p["creator"] = creator
        batch = self._get(f"{FRONTEND}/coins", p) or []
        kept = [c for c in batch if is_solana(c)]
        self.skipped_foreign += len(batch) - len(kept)
        return kept

    def coin(self, mint):
        c = self._get(f"{FRONTEND}/coins/{mint}")
        return c if is_solana(c) else None

    def creator_coins(self, creator, max_coins=500):
        """Все монеты одного кошелька-создателя. Фильтр серверный и строгий."""
        out, offset = [], 0
        while len(out) < max_coins:
            before = self.skipped_foreign
            batch = self.coins(offset=offset, creator=creator, sort="created_timestamp")
            raw_size = len(batch) + (self.skipped_foreign - before)
            if not raw_size:
                break
            out.extend(batch)
            if raw_size < PAGE_MAX:
                break
            offset += PAGE_MAX
        return out[:max_coins]

    # --- прочее ---

    def user(self, address):
        """Профиль кошелька. Вспомогательные данные (ник, подписчики):
        если эндпоинт временно лежит, отдаём пустой профиль, а не роняем анализ."""
        try:
            return self._get(f"{FRONTEND}/users/{address}")
        except (RuntimeError, urllib.error.HTTPError):
            return None

    def sol_price(self):
        return (self._get(f"{FRONTEND}/sol-price") or {}).get("solPrice")

    # Больше 100 сделок за страницу сервер не отдаёт: 400 Bad Request.
    TRADES_PAGE = 100

    def trades(self, mint, limit=100):
        """Сделки по монете, v2 (v1 отдаёт 410). Курсорная пагинация."""
        out, cursor = [], None
        while len(out) < limit:
            p = {"limit": min(self.TRADES_PAGE, limit - len(out))}
            if cursor:
                p["cursor"] = cursor
            d = self._get(f"{SWAP}/v2/coins/{mint}/trades", p)
            if not d or not d.get("trades"):
                break
            out.extend(d["trades"])
            pg = d.get("pagination") or {}
            if not pg.get("hasMore"):
                break
            cursor = pg.get("nextCursor")
        return out

    def top_holders(self, mint):
        """Топ холдеров (до 50) с флагами pump.fun: isDev, isSniper, isBundler."""
        d = self._get(f"{FRONTEND}/coins/top-holders/{mint}") or {}
        return d.get("topHolders") or []

    def candles(self, mint, interval="1m", limit=100, currency="USD"):
        return self._get(f"{SWAP}/v1/coins/{mint}/candles",
                         {"interval": interval, "limit": limit, "currency": currency}) or []
