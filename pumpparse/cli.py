"""CLI: сканер новых токенов pump.fun с профилированием создателя."""
import argparse
import contextlib
import getpass
import secrets
import sys
import time

from . import auth as authmod
from . import rules as rulemod
from .analysis import profile_creator
from .api import PumpAPI
from .db import Store


def _age(ms):
    if not ms:
        return "?"
    s = max(0, int(time.time() - ms / 1000))
    return f"{s}s" if s < 60 else f"{s // 60}m" if s < 3600 else f"{s // 3600}h"


def _win(p, n):
    w = (p.get("windows") or {}).get(str(n))
    if not w:
        return ""
    return (f"[окно {n}: {w['mig']}/{w['coins']} миграций "
            f"(ray {w['ray']}/swap {w['pswap']}), подряд {w['streak']}, gap {w['gap']}] ")


def _line(c, p, win=None):
    flags = []
    if c.get("twitter"):
        flags.append("X")
    if c.get("telegram"):
        flags.append("TG")
    if c.get("website"):
        flags.append("WEB")
    return (
        f"[{_age(c.get('created_timestamp'))}] {c.get('symbol') or '?':<10} "
        f"{(c.get('name') or '')[:28]:<28} mc=${c.get('usd_market_cap') or 0:>9,.0f}  "
        f"{'|'.join(flags) or '-':<10} "
        f"dev={p['username'] or p['address'][:6]} "
        f"coins={p['total_coins']} mig={p['migrated']} "
        f"({p['migration_rate']*100:.0f}%) score={p['score']} :: {p['verdict']}\n"
        f"    https://pump.fun/coin/{c['mint']}"
    )


def cmd_scan(args):
    api, store = PumpAPI(rps=args.rps), Store(args.db)
    try:
        rules = rulemod.parse_all(args.rule, args.preset)
    except ValueError as e:
        sys.exit(str(e))
    wins = rulemod.windows_needed(rules)
    if rules:
        mode = "все" if not args.any else "любое"
        print("правила (" + mode + "): " + ", ".join(r.raw for r in rules))
    print(f"сканер запущен, интервал {args.interval}s, БД {args.db}, "
          f"сеть Solana mainnet. Ctrl+C для выхода.")
    while True:
        try:
            for c in reversed(api.coins(limit=args.limit, sort="created_timestamp", order="DESC")):
                if store.seen(c["mint"]):
                    continue
                store.upsert_coin(c)
                p = profile_creator(api, c["creator"], store, windows=tuple(wins))
                if p["score"] < args.min_score or p["migrated"] < args.min_migrated:
                    continue
                if not rulemod.match(rules, p["windows"], require_all=not args.any):
                    continue
                print(_line(c, p, win=wins[0]), flush=True)
        except Exception as e:
            print(f"! ошибка цикла: {e}", file=sys.stderr, flush=True)
        if api.skipped_foreign:
            print(f"  (пропущено записей чужих сетей: {api.skipped_foreign})",
                  file=sys.stderr, flush=True)
        if args.once:
            return
        time.sleep(args.interval)


def cmd_dev(args):
    api, store = PumpAPI(rps=args.rps), Store(args.db)
    addr = args.address
    if len(addr) > 40 and addr.endswith("pump"):     # передали минт вместо кошелька
        coin = api.coin(addr)
        if not coin:
            sys.exit("монета не найдена или не в сети Solana")
        addr = coin["creator"]
        print(f"создатель монеты {coin['symbol']}: {addr}\n")
    try:
        rules = rulemod.parse_all(args.rule, args.preset)
    except ValueError as e:
        sys.exit(str(e))
    wins = rulemod.windows_needed(rules) if rules else [5, 10, 20]
    p = profile_creator(api, addr, store, cache_age=0, windows=tuple(wins))
    w = max(len(k) for k in p)
    for k, v in p.items():
        if k in ("migrated_mints", "windows"):
            continue
        print(f"{k:<{w}} : {v}")
    for n, m in sorted(p["windows"].items(), key=lambda kv: int(kv[0])):
        print(f"окно {n:<13} : " + "  ".join(f"{k}={v}" for k, v in m.items()))
    for m in p["migrated_mints"]:
        print(f"миграция           : https://pump.fun/coin/{m}")


    if rules:
        ok = rulemod.match(rules, p["windows"], require_all=not args.any)
        print(f"\nправила ({'все' if not args.any else 'любое'}): "
              f"{'ПРОХОДИТ' if ok else 'не проходит'}")
        for r in rules:
            print(f"  [{'+' if r.test(p['windows']) else '-'}] {r.explain(p['windows'])}")
def cmd_coin(args):
    api = PumpAPI(rps=args.rps)
    c = api.coin(args.mint)
    if not c:
        sys.exit("монета не найдена или не в сети Solana")
    for k in ("mint", "name", "symbol", "creator", "username", "created_timestamp",
              "complete", "usd_market_cap", "ath_market_cap", "reply_count",
              "twitter", "telegram", "website", "pool_address"):
        print(f"{k:<18} : {c.get(k)}")
    trades = api.trades(args.mint, limit=args.trades)
    buys = sum(1 for t in trades if t["type"] == "buy")
    print(f"\nсделок загружено   : {len(trades)} (buy {buys} / sell {len(trades)-buys})")
    print(f"уникальных кошельков: {len({t['userAddress'] for t in trades})}")


def _add_rule_args(p):
    p.add_argument("--rule", action="append", metavar="ВЫРАЖЕНИЕ",
                   help="условие вида mig>=3@10 или streak>=3@10 (можно повторять)")
    p.add_argument("--preset", action="append", choices=sorted(rulemod.PRESETS),
                   help="готовый набор условий (можно повторять)")
    p.add_argument("--any", action="store_true",
                   help="достаточно одного условия вместо всех")


def cmd_web(args):
    from .web import serve
    host = "0.0.0.0" if args.lan else args.host  # noqa: S104 — ровно этого и просит --lan
    serve(db_path=args.db, port=args.port, interval=args.interval,
          limit=args.limit, rps=args.rps, host=host,
          secure_cookie=None if not args.insecure_cookie else False)


def cmd_passwd(args):
    """Печатает строки окружения для авторизации. Пароль в БД не пишется."""
    pw = getpass.getpass("новый пароль: ")
    if len(pw) < 8:
        sys.exit("пароль короче 8 символов")
    if pw != getpass.getpass("ещё раз: "):
        sys.exit("пароли не совпали")
    print("\n# в файл окружения сервиса (например /etc/pumpparse.env), "
          "кавычки обязательны:")
    print(f'{authmod.USER_ENV}={args.user}')
    print(f"{authmod.HASH_ENV}='{authmod.hash_password(pw)}'")
    print(f"{authmod.SECRET_ENV}={secrets.token_hex(32)}"
          "   # смена ключа разлогинивает всех")


def main():
    # Windows-консоль по умолчанию не в UTF-8 — иначе кириллица превращается в мусор.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        prog="pumpparse", description="Сканер pump.fun",
        epilog="метрики окна: " + "; ".join(f"{k} — {v}" for k, v in rulemod.METRICS.items())
               + ". Пресеты: " + "; ".join(f"{k} = {' и '.join(v)}"
                                           for k, v in rulemod.PRESETS.items()),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="pump.db")
    ap.add_argument("--rps", type=float, default=4.0, help="запросов в секунду")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="следить за новыми токенами")
    s.add_argument("--interval", type=float, default=15)
    s.add_argument("--limit", type=int, default=70)
    s.add_argument("--min-score", type=int, default=0, help="скрыть создателей ниже рейтинга")
    s.add_argument("--min-migrated", "--min-graduated", type=int, default=0,
                   dest="min_migrated", help="только с N миграциями")
    s.add_argument("--once", action="store_true")
    _add_rule_args(s)
    s.set_defaults(func=cmd_scan)

    d = sub.add_parser("dev", help="профиль создателя (адрес кошелька или минт)")
    d.add_argument("address")
    _add_rule_args(d)
    d.set_defaults(func=cmd_dev)

    c = sub.add_parser("coin", help="карточка монеты + сводка по сделкам")
    c.add_argument("mint")
    c.add_argument("--trades", type=int, default=200)
    c.set_defaults(func=cmd_coin)

    wb = sub.add_parser("web", help="веб-интерфейс")
    wb.add_argument("--port", type=int, default=8000)
    wb.add_argument("--interval", type=float, default=20)
    wb.add_argument("--limit", type=int, default=70)
    wb.add_argument("--host", default="127.0.0.1",
                    help="адрес привязки; по умолчанию только этот компьютер")
    wb.add_argument("--lan", action="store_true",
                    help="открыть для локальной сети (0.0.0.0)")
    wb.add_argument("--insecure-cookie", action="store_true",
                    help="не ставить флаг Secure на сессию: вход по http:// без TLS")
    wb.set_defaults(func=cmd_web)

    pw = sub.add_parser("passwd", help="сгенерировать хэш пароля для авторизации")
    pw.add_argument("--user", default="admin")
    pw.set_defaults(func=cmd_passwd)

    args = ap.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nостановлено")


if __name__ == "__main__":
    main()
