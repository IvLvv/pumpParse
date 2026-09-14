"""Профилирование создателя монеты по его истории запусков на pump.fun.

Монета имеет ровно два состояния: **миграция** (бондинг-кривая закрыта,
ликвидность уехала в пул) и **осталась** (всё ещё на кривой). Никаких оценок
«жива/мертва» по капе и возрасту не делается: API не даёт для этого
авторитетного признака, а порог был выдуманным.
"""
import statistics
import time

from .rules import PERIODS, window_key


def is_migrated(c):
    """Настоящая миграция, а не завершённый раунд Mayhem.

    Одного флага `complete` мало: у монет режима Mayhem он тоже true, хотя
    ликвидность никуда не уезжала — на pump.fun они светятся значком
    «Completed» с капой в пару долларов. Отличает их поле `mayhem_state`
    (active/paused/completed); у обычных монет оно пустое. По выборке из 350
    монет с `complete=true` разделение чистое: без mayhem_state минимальный
    ATH — $41k (кривая закрывается около $69k), с ним медианный ATH — $4.5k.
    """
    return bool(c.get("complete")) and not c.get("mayhem_state")


def venue(c):
    """Куда уехала ликвидность: raydium, pumpswap или None (не мигрировала).

    Поля `raydium_pool` и `pump_swap_pool` взаимоисключающие. С весны 2025
    pump.fun мигрирует монеты в собственный AMM PumpSwap, а не в Raydium:
    в свежих срезах доля Raydium — 0%, в срезах 2024 года — 60-70%.
    Так что фильтр `ray` на новых монетах ничего не найдёт по существу рынка,
    а не из-за ошибки — смотреть на него имеет смысл только по старой истории.
    """
    if not is_migrated(c):
        return None
    if c.get("raydium_pool"):
        return "raydium"
    if c.get("pump_swap_pool"):
        return "pumpswap"
    return "unknown"


def _median(xs):
    return statistics.median(xs) if xs else 0.0


def _best_streak(flags):
    """Самая длинная серия подряд идущих миграций."""
    best = run = 0
    for f in flags:
        run = run + 1 if f else 0
        best = max(best, run)
    return best


def window_metrics(coins, n, now=None, period=None):
    """Метрики по последним n монетам дева (свежие первыми).

    period — имя из rules.PERIODS: сначала отсекаются монеты старше периода,
    и только из оставшихся берутся последние n. Так streak>=3@10/24h видит
    серию, сделанную сегодня, а не полгода назад.
    """
    now = now or int(time.time() * 1000)
    if period:
        since = now - PERIODS[period] * 1000
        coins = [c for c in coins if (c.get("created_timestamp") or 0) >= since]
    w = coins[:n]
    if not w:
        return dict.fromkeys(
            ("mig", "ray", "pswap", "rate", "stay", "streak", "medath",
             "bestath", "pace", "gap", "social", "coins"), 0)

    flags = [is_migrated(c) for c in w]
    mig = sum(flags)
    venues = [venue(c) for c in w]
    aths = [c.get("ath_market_cap") or 0.0 for c in w]
    social = [c for c in w if c.get("twitter") or c.get("telegram") or c.get("website")]

    # Темп внутри окна: от самой старой монеты окна до самой свежей.
    ts = [c.get("created_timestamp") or now for c in w]
    span_days = max((max(ts) - min(ts)) / 86_400_000, 1 / 1440)
    pace = len(w) / span_days if len(w) > 1 else 0.0

    # Сколько монет запущено после последней миграции (0 — последняя же и уехала).
    gap = next((i for i, f in enumerate(flags) if f), len(w))

    return {
        "coins": len(w),
        "mig": mig,
        "ray": venues.count("raydium"),
        "pswap": venues.count("pumpswap"),
        "rate": round(mig / len(w), 3),
        "stay": round((len(w) - mig) / len(w), 3),
        "streak": _best_streak(flags),
        "medath": round(_median(aths), 2),
        "bestath": round(max(aths), 2),
        "pace": round(pace, 2),
        "gap": gap,
        "social": round(len(social) / len(w), 3),
    }


def coin_state(c, now=None):
    """mig — монета мигрировала в пул, stay — осталась на кривой."""
    return "mig" if is_migrated(c) else "stay"


def compact(c, now=None):
    """Урезанная карточка монеты для истории дева."""
    return {
        "mint": c.get("mint"),
        "name": c.get("name"),
        "symbol": c.get("symbol"),
        "created_timestamp": c.get("created_timestamp"),
        "usd_market_cap": c.get("usd_market_cap"),
        "ath_market_cap": c.get("ath_market_cap"),
        "reply_count": c.get("reply_count"),
        "has_social": bool(c.get("twitter") or c.get("telegram") or c.get("website")),
        "state": coin_state(c, now),
        "mayhem": bool(c.get("mayhem_state")),
        "venue": venue(c),
    }


def window_keys(windows, periods=()):
    """Все срезы окно×период, которые считаются в снимке: '10', '10/24h', ..."""
    return [window_key(n, p) for n in windows for p in (None, *periods)]


def profile_creator(api, address, store=None, cache_age=3600, max_coins=300,
                    windows=(5, 10, 20), periods=(), with_coins=False):
    """Сводка по кошельку: сколько запусков и сколько из них мигрировало.

    'Мигрировала' = complete без mayhem_state: бондинг-кривая закрыта и
    ликвидность ушла в пул (см. is_migrated).
    """
    keys = window_keys(windows, periods)
    if store:
        cached = store.cached_creator(address, cache_age)
        # Кеш годится, только если в нём посчитаны все нужные срезы.
        if cached and all(k in (cached.get("windows") or {}) for k in keys):
            return cached

    coins = api.creator_coins(address, max_coins=max_coins)
    # Свежие первыми — окна режутся от последних запусков.
    coins.sort(key=lambda c: c.get("created_timestamp") or 0, reverse=True)
    # История упёрлась в лимит: доля миграций и темп запусков занижены.
    truncated = len(coins) >= max_coins
    user = api.user(address) or {}
    now = int(time.time() * 1000)

    migrated = [c for c in coins if is_migrated(c)]
    aths = [c.get("ath_market_cap") or 0.0 for c in coins]

    total = len(coins)
    rate = len(migrated) / total if total else 0.0
    first_launch = min((c.get("created_timestamp") or 0 for c in coins), default=0)

    # Темп запусков: спамер льёт десятки монет в сутки.
    span_days = max((now - first_launch) / 86_400_000, 1 / 24) if first_launch else 1
    per_day = total / span_days

    s = {
        "address": address,
        "username": user.get("username"),
        "followers": user.get("followers") or 0,
        "x_username": user.get("x_username"),
        "total_coins": total,
        "migrated": len(migrated),
        "migrated_raydium": sum(1 for c in migrated if venue(c) == "raydium"),
        "migrated_pumpswap": sum(1 for c in migrated if venue(c) == "pumpswap"),
        "migration_rate": round(rate, 3),
        "best_ath": round(max(aths) if aths else 0.0, 2),
        "median_ath": round(_median(aths), 2),
        "stayed_coins": total - len(migrated),
        "stayed_rate": round((total - len(migrated)) / total, 3) if total else 0.0,
        "launches_per_day": round(per_day, 2),
        "first_launch": first_launch,
        "truncated": truncated,
        "windows": {window_key(n, p): window_metrics(coins, n, now, p)
                    for n in windows for p in (None, *periods)},
        "migrated_mints": [c["mint"] for c in migrated],
    }
    s["verdict"] = verdict(s)
    s["score"] = score(s)

    if store:
        store.save_creator(s)      # в кеш кладём только сводку, без списка монет
    if with_coins:
        s["coins"] = [compact(c, now) for c in coins]
    return s


def verdict(s):
    if s["migrated"] >= 3 and s["migration_rate"] >= 0.15:
        return "серийный успешный — несколько миграций"
    if s["migrated"] >= 1:
        return "есть подтверждённая миграция"
    if s["total_coins"] <= 1:
        return "новичок — истории нет"
    if s["launches_per_day"] >= 5 and s["total_coins"] >= 10:
        return "конвейер — десятки запусков в сутки, ни одной миграции"
    if s["total_coins"] >= 5:
        return "без миграций, история слабая"
    return "без миграций, истории мало"


def score(s):
    """Грубый рейтинг 0..100: чем выше, тем интереснее создатель."""
    v = 0
    v += min(s["migrated"], 5) * 12          # миграции весят больше всего
    v += min(s["migration_rate"] * 100, 25)
    v += min(s["best_ath"] / 10_000, 15)
    v += min(s["followers"] / 100, 10)
    v -= min(max(s["launches_per_day"] - 3, 0) * 2, 15)
    return max(0, min(100, round(v)))
