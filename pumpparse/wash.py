"""Накрутка: монеты, которые «крутят» ботами, а не торгуют живые люди.

Что видно в данных pump.fun (срезы сентября 2026, ~30 монет с сделками
и холдерами, см. README «Накрутка»):

* **пыль** — бот-объём. Сотня сделок по $0.02–0.05 от сотни разных кошельков
  за десять секунд; капа при этом рисуется в миллионы (ADAMITY: 3 минуты
  от рождения, $3.6M, 90% сделок меньше доллара). У живых монет доля сделок
  дешевле доллара — 0–25%.
* **клоны** — бандл-кошельки с одинаковым балансом. У NYT Coin 42 из 50
  топ-холдеров держали ровно 911 692.528 токенов: это один владелец, который
  раскидал закуп по кошелькам. У живых монет совпадений 1–2 (случайные
  круглые закупы).
* **самокрутка** — два-семь кошельков гоняют монету туда-сюда: 100 сделок
  от двух адресов (beer), 33 от двух (SXSN). У живых монет на сотню сделок
  приходится 30–100 адресов.
* **бандл** — pump.fun сам помечает холдеров флагами `isBundler` /
  `isSniper` (эндпоинт `coins/top-holders`). Когда бандлеров половина
  топа — закуп с запуска контролирует дев.

Флаг `boost_mode` (IN_PROGRESS / COMPLETED) встречался у всех накрученных
до миллионов монет, но и у пары живых — сам по себе не критерий, отдаётся
как справка.

Проверка стоит два запроса к API, поэтому воркер делает её только монетам,
которые уже прошли остальной фильтр пула.
"""
import collections

# Пороги подобраны по срезу, а не выдуманы: между живыми и накрученными
# монетами по каждому признаку есть зазор в разы (см. модульный docstring).
MIN_TRADES = 20          # меньше — судить рано, объём ещё не набрался
DUST_USD = 1.0           # сделка дешевле — «пыль»
DUST_SHARE = 0.5         # доля пыли, с которой это бот-объём (живые: до 0.25)
SPIN_MIN_TRADES = 30     # самокрутку ищем от 30 сделок
SPIN_RATIO = 10.0        # сделок на кошелёк (живые: 1–7)
CLONE_MIN = 5            # холдеров с одинаковым балансом (живые: 1–2)
BUNDLE_MIN = 5           # бандлеров в топе — минимум штук
BUNDLE_SHARE = 0.5       # и минимум доля от топа холдеров


def assess(coin, trades, holders):
    """Чистая функция: сделки и холдеры → вердикт.

    Возвращает {"suspicious": bool, "flags": [...], ...метрики}. Метрики
    отдаются всегда, чтобы в интерфейсе было видно, на чём основан вердикт.
    """
    usd = [float(t.get("amountUsd") or 0) for t in trades]
    wallets = collections.Counter(t.get("userAddress") for t in trades)
    n = len(trades)
    dust = sum(u < DUST_USD for u in usd)
    dust_share = round(dust / n, 3) if n else 0.0
    ratio = round(n / len(wallets), 1) if wallets else 0.0

    # Дева из подсчёта клонов убираем: у него баланс свой, а не «раздатый».
    amounts = collections.Counter(round(float(h.get("amount") or 0), 2)
                                  for h in holders if not h.get("isDev"))
    clones = max(amounts.values(), default=0)
    bundlers = sum(1 for h in holders if h.get("isBundler"))
    snipers = sum(1 for h in holders if h.get("isSniper"))
    bundle_share = round(bundlers / len(holders), 3) if holders else 0.0

    flags = []
    if n >= MIN_TRADES and dust_share >= DUST_SHARE:
        flags.append("пыль")
    if n >= SPIN_MIN_TRADES and ratio >= SPIN_RATIO:
        flags.append("самокрутка")
    if clones >= CLONE_MIN:
        flags.append("клоны")
    if bundlers >= BUNDLE_MIN and bundle_share >= BUNDLE_SHARE:
        flags.append("бандл")

    return {
        "suspicious": bool(flags),
        "flags": flags,
        "trades": n,
        "wallets": len(wallets),
        "ratio": ratio,
        "dust_share": dust_share,
        "holders": len(holders),
        "clones": clones,
        "bundlers": bundlers,
        "snipers": snipers,
        "boost": (coin or {}).get("boost_mode") or None,
    }


def inspect(api, coin):
    """Два запроса к API и вердикт. Сеть упала — вердикт «не проверено»:
    монету из-за этого не отсеиваем, но и чистой не называем."""
    try:
        trades = api.trades(coin["mint"], limit=100)
        holders = api.top_holders(coin["mint"])
    except Exception as e:  # noqa: BLE001 — любой сбой сети равнозначен
        return {"suspicious": False, "flags": [], "error": f"{type(e).__name__}: {e}",
                "boost": coin.get("boost_mode") or None}
    return assess(coin, trades, holders)


def label(w):
    """Короткая строка для столбца «почему прошла» и подсказок."""
    if not w:
        return "накрутка: не проверялась"
    if w.get("error"):
        return "накрутка: не проверено (" + w["error"] + ")"
    if w["suspicious"]:
        return "накрутка: " + ", ".join(w["flags"])
    if w["trades"] < MIN_TRADES:
        return f"мало данных: {w['trades']} сделок"
    return f"чисто: {w['trades']} сделок от {w['wallets']} кошельков"
