"""Пул: настраиваемый отбор девов по их новым монетам, капе и свежести.

В пуле живут девы. Дев попадает туда, когда его новая монета проходит фильтр;
дальше каждая его следующая монета поднимает его наверх списка с пометкой
«новая монета» — фильтр по капе, возрасту и правилам к ней уже не применяется,
дев уже отобран. Единственное, что проверяется всегда, — накрутка: монету,
которую крутят ботами, в пул не пускаем, даже от знакомого дева.

Фильтр живёт на сервере, а не в браузере: монеты отбираются в момент появления
фоновым воркером, поэтому пул наполняется и при закрытой странице.
"""
import time

from . import rules as rulemod
from . import wash as washmod

DEFAULT = {
    "enabled": True,
    "rules": [],            # условия по деву в формате mig>=3@10
    "any": False,           # True — достаточно одного условия
    "mcap_min": 0.0,        # капа в долларах, нижняя граница
    "mcap_max": None,       # верхняя граница, None — без ограничения
    "age_max_sec": 900,     # брать монеты не старше этого возраста
    "require_social": False,   # только с привязанным X/TG/сайтом
    "min_score": 0,
    "wash_filter": True,       # отсеивать монеты с признаками накрутки (см. wash.py)
}


def normalize(cfg):
    """Приводит присланный из UI конфиг к безопасному виду."""
    c = dict(DEFAULT)
    c.update({k: v for k, v in (cfg or {}).items() if k in DEFAULT})
    c["enabled"] = bool(c["enabled"])
    c["any"] = bool(c["any"])
    c["require_social"] = bool(c["require_social"])
    c["wash_filter"] = bool(c["wash_filter"])
    c["rules"] = [str(r).strip() for r in (c["rules"] or []) if str(r).strip()]
    for r in c["rules"]:
        rulemod.parse(r)                     # падаем сразу, а не на каждой монете
    c["mcap_min"] = max(0.0, float(c["mcap_min"] or 0))
    c["mcap_max"] = None if c["mcap_max"] in (None, "", 0) else float(c["mcap_max"])
    c["age_max_sec"] = max(0, int(c["age_max_sec"] or 0)) or None
    c["min_score"] = int(c["min_score"] or 0)
    return c


def match(coin, dev, cfg, now=None):
    """Проверяет монету против конфига. Возвращает (подходит, причины)."""
    now = now or time.time()
    why = []

    mc = coin.get("usd_market_cap") or 0
    if mc < cfg["mcap_min"]:
        return False, why
    if cfg["mcap_max"] is not None and mc > cfg["mcap_max"]:
        return False, why
    why.append(f"капа ${mc:,.0f}".replace(",", " "))

    if cfg["age_max_sec"]:
        age = now - (coin.get("created_timestamp") or 0) / 1000
        if age > cfg["age_max_sec"]:
            return False, why
        why.append(f"возраст {int(age)}с")

    if cfg["require_social"] and not (coin.get("twitter") or coin.get("telegram")
                                      or coin.get("website")):
        return False, why

    if not dev:
        return False, why
    if (dev.get("score") or 0) < cfg["min_score"]:
        return False, why

    if cfg["rules"]:
        parsed = [rulemod.parse(r) for r in cfg["rules"]]
        wins = dev.get("windows") or {}
        if not rulemod.keys_needed(parsed) <= set(wins):
            return False, why          # срез не посчитан — судить не по чему
        if not rulemod.match(parsed, wins, require_all=not cfg["any"]):
            return False, why
        why += [r.raw for r in parsed if r.test(wins)]

    return True, why


def consider(coin, dev, cfg, store, api, now=None):
    """Полная проверка новой монеты воркером. Возвращает (добавлена, причины).

    Порядок: сначала дешёвые проверки по уже имеющимся данным (match), и только
    прошедшим — проверка накрутки, потому что она стоит два запроса к API.
    Дев, который уже в пуле, проходит match автоматически: его следующая
    монета должна поднять его наверх, а не пройти отбор заново.
    """
    if store.pool_has_dev(coin.get("creator")):
        ok, why = True, ["дев уже в пуле"]
    else:
        ok, why = match(coin, dev, cfg, now)
    if not ok:
        return False, why

    if cfg["wash_filter"]:
        w = washmod.inspect(api, coin)
        store.set_coin_wash(coin["mint"], w)
        why.append(washmod.label(w))
        if w["suspicious"]:
            return False, why

    return store.pool_add(coin["mint"], why), why
