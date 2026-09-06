"""Хранилище SQLite: монеты, снимки создателей."""
import json
import sqlite3
import time

from .analysis import venue

SCHEMA = """
CREATE TABLE IF NOT EXISTS coins (
    mint TEXT PRIMARY KEY,
    name TEXT, symbol TEXT, creator TEXT, username TEXT,
    created_timestamp INTEGER,
    first_seen INTEGER,
    complete INTEGER,
    mayhem TEXT,
    venue TEXT,
    usd_market_cap REAL,
    ath_market_cap REAL,
    twitter TEXT, telegram TEXT, website TEXT,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_coins_creator ON coins(creator);
CREATE INDEX IF NOT EXISTS idx_coins_created ON coins(created_timestamp DESC);

CREATE TABLE IF NOT EXISTS pool (
    mint TEXT PRIMARY KEY,
    added_at INTEGER,
    seen INTEGER DEFAULT 0,
    why TEXT
);
CREATE INDEX IF NOT EXISTS idx_pool_added ON pool(added_at DESC);
CREATE INDEX IF NOT EXISTS idx_pool_seen ON pool(seen);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS creators (
    address TEXT PRIMARY KEY,
    username TEXT, followers INTEGER,
    total_coins INTEGER, migrated INTEGER,
    migration_rate REAL, best_ath REAL, median_ath REAL,
    stayed_coins INTEGER, first_launch INTEGER, verdict TEXT,
    checked_at INTEGER, raw TEXT
);
"""


class Store:
    def __init__(self, path="pump.db"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self._rename_old_columns()
        self.db.executescript(SCHEMA)
        self._add_missing_columns()
        self._drop_stale_cache()
        self.db.commit()

    def _cols(self, table):
        return {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}

    def _rename_old_columns(self):
        """Схема до перехода на термины «миграция/осталась»."""
        if "graduated" not in self._cols("creators"):
            return
        for old, new in (("graduated", "migrated"),
                         ("graduation_rate", "migration_rate"),
                         ("dead_coins", "stayed_coins")):
            self.db.execute(f"ALTER TABLE creators RENAME COLUMN {old} TO {new}")

    def _add_missing_columns(self):
        """CREATE TABLE IF NOT EXISTS не достраивает колонки существующей таблице."""
        for col in ("mayhem", "venue"):
            if self._cols("coins") and col not in self._cols("coins"):
                self.db.execute(f"ALTER TABLE coins ADD COLUMN {col} TEXT")

    # Растёт, когда прошлые снимки девов посчитаны по устаревшим правилам
    # и их нельзя показывать: кеш сбрасывается, воркер переберёт девов заново.
    CACHE_REV = 3

    def _drop_stale_cache(self):
        rev = self.db.execute(
            "SELECT value FROM settings WHERE key='cache_rev'").fetchone()
        if rev and json.loads(rev["value"]) == self.CACHE_REV:
            return
        self.db.execute("DELETE FROM creators")
        self.db.execute("INSERT INTO settings (key,value) VALUES ('cache_rev',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (json.dumps(self.CACHE_REV),))

    def seen(self, mint):
        return self.db.execute("SELECT 1 FROM coins WHERE mint=?", (mint,)).fetchone() is not None

    def upsert_coin(self, c):
        self.db.execute(
            """INSERT INTO coins (mint,name,symbol,creator,username,created_timestamp,
                   first_seen,complete,mayhem,venue,usd_market_cap,ath_market_cap,
                   twitter,telegram,website,raw)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(mint) DO UPDATE SET
                   complete=excluded.complete, mayhem=excluded.mayhem,
                   venue=excluded.venue,
                   usd_market_cap=excluded.usd_market_cap,
                   ath_market_cap=excluded.ath_market_cap,
                   raw=excluded.raw""",
            (c.get("mint"), c.get("name"), c.get("symbol"), c.get("creator"),
             c.get("username"), c.get("created_timestamp"), int(time.time() * 1000),
             1 if c.get("complete") else 0, c.get("mayhem_state"), venue(c),
             c.get("usd_market_cap"), c.get("ath_market_cap"),
             c.get("twitter"), c.get("telegram"), c.get("website"), json.dumps(c)))
        self.db.commit()

    def save_creator(self, s):
        self.db.execute(
            """INSERT INTO creators (address,username,followers,total_coins,migrated,
                   migration_rate,best_ath,median_ath,stayed_coins,first_launch,verdict,checked_at,raw)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(address) DO UPDATE SET
                   username=excluded.username, followers=excluded.followers,
                   total_coins=excluded.total_coins, migrated=excluded.migrated,
                   migration_rate=excluded.migration_rate, best_ath=excluded.best_ath,
                   median_ath=excluded.median_ath, stayed_coins=excluded.stayed_coins,
                   verdict=excluded.verdict, checked_at=excluded.checked_at, raw=excluded.raw""",
            (s["address"], s["username"], s["followers"], s["total_coins"], s["migrated"],
             s["migration_rate"], s["best_ath"], s["median_ath"], s["stayed_coins"],
             s["first_launch"], s["verdict"], int(time.time()), json.dumps(s)))
        self.db.commit()

    def feed(self, limit=200):
        """Монеты с приклеенным профилем создателя, свежие первыми."""
        rows = self.db.execute(
            """SELECT c.mint, c.name, c.symbol, c.creator, c.username,
                      c.created_timestamp, c.first_seen, c.complete, c.mayhem, c.venue,
                      c.usd_market_cap, c.ath_market_cap,
                      c.twitter, c.telegram, c.website,
                      cr.raw AS dev_raw
                 FROM coins c LEFT JOIN creators cr ON cr.address = c.creator
                ORDER BY c.created_timestamp DESC LIMIT ?""", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["dev"] = json.loads(d.pop("dev_raw")) if d["dev_raw"] else None
            out.append(d)
        return out

    def counts(self):
        c = self.db.execute("SELECT COUNT(*) FROM coins").fetchone()[0]
        d = self.db.execute("SELECT COUNT(*) FROM creators").fetchone()[0]
        g = self.db.execute("SELECT COUNT(*) FROM creators WHERE migrated > 0").fetchone()[0]
        return {"coins": c, "creators": d, "creators_with_mig": g}

    # --- пул ---

    def pool_add(self, mint, why):
        cur = self.db.execute(
            "INSERT OR IGNORE INTO pool (mint, added_at, seen, why) VALUES (?,?,0,?)",
            (mint, int(time.time() * 1000), json.dumps(why, ensure_ascii=False)))
        self.db.commit()
        return cur.rowcount > 0

    def pool_rows(self, limit=300):
        rows = self.db.execute(
            """SELECT p.mint, p.added_at, p.seen, p.why,
                      c.name, c.symbol, c.creator, c.created_timestamp,
                      c.usd_market_cap, c.ath_market_cap,
                      c.twitter, c.telegram, c.website,
                      cr.raw AS dev_raw
                 FROM pool p
                 JOIN coins c ON c.mint = p.mint
                 LEFT JOIN creators cr ON cr.address = c.creator
                ORDER BY p.added_at DESC LIMIT ?""", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["dev"] = json.loads(d.pop("dev_raw")) if d["dev_raw"] else None
            d["why"] = json.loads(d["why"]) if d["why"] else []
            out.append(d)
        return out

    def pool_unread(self):
        return self.db.execute("SELECT COUNT(*) FROM pool WHERE seen=0").fetchone()[0]

    def pool_mark_seen(self):
        n = self.db.execute("UPDATE pool SET seen=1 WHERE seen=0").rowcount
        self.db.commit()
        return n

    def pool_clear(self):
        n = self.db.execute("DELETE FROM pool").rowcount
        self.db.commit()
        return n

    def pool_total(self):
        return self.db.execute("SELECT COUNT(*) FROM pool").fetchone()[0]

    # --- настройки ---

    def get_setting(self, key, default=None):
        r = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set_setting(self, key, value):
        self.db.execute(
            "INSERT INTO settings (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)))
        self.db.commit()

    def cached_creator(self, address, max_age=3600):
        r = self.db.execute(
            "SELECT raw,checked_at FROM creators WHERE address=?", (address,)).fetchone()
        if r and time.time() - r["checked_at"] < max_age:
            return json.loads(r["raw"])
        return None
