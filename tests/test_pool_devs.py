"""Пул девов: consider() воркера и хранилище-журнал, из которого собирается список.

Дев заводится в пул первой монетой, прошедшей фильтр; каждая следующая
поднимает его наверх. Накрутка проверяется всегда — и у знакомого дева.
"""
import time

import pytest

from pumpparse import pool as poolmod
from pumpparse.db import Store

NOW = 1_700_000_000.0


def coin(**kw):
    base = {"mint": "M1", "creator": "DEV", "usd_market_cap": 50_000.0,
            "created_timestamp": int((NOW - 60) * 1000),
            "twitter": None, "telegram": None, "website": None}
    base.update(kw)
    return base


def dev(**kw):
    base = {"score": 50, "windows": {"10": {"mig": 3, "streak": 2, "coins": 10}}}
    base.update(kw)
    return base


class TestНормализацияНакрутки:
    def test_фильтр_накрутки_включён_по_умолчанию(self):
        assert poolmod.normalize({})["wash_filter"] is True

    @pytest.mark.parametrize("value,expected", [(0, False), ("", False), (1, True), ("да", True)])
    def test_флаг_приводится_к_bool(self, value, expected):
        assert poolmod.normalize({"wash_filter": value})["wash_filter"] is expected


class TestПравилаСПериодом:
    def test_правило_с_периодом_смотрит_свой_срез(self):
        d = dev(windows={"10": {"streak": 3}, "10/24h": {"streak": 1}})
        assert poolmod.match(coin(), d, poolmod.normalize({"rules": ["streak>=3@10"]}), now=NOW)[0]
        assert not poolmod.match(coin(), d, poolmod.normalize({"rules": ["streak>=3@10/24h"]}),
                                 now=NOW)[0]

    def test_непосчитанный_период_не_проходит(self):
        cfg = poolmod.normalize({"rules": ["streak>=1@10/24h"]})
        assert poolmod.match(coin(), dev(), cfg, now=NOW)[0] is False


class FakeStore:
    """Минимум, который нужен consider(): журнал попаданий и вердикты по накрутке."""

    def __init__(self, devs_in_pool=()):
        self.creators = set(devs_in_pool)
        self.added, self.wash = [], {}

    def pool_has_dev(self, address):
        return address in self.creators

    def pool_add(self, mint, why):
        if mint in self.added:
            return False
        self.added.append(mint)
        return True

    def set_coin_wash(self, mint, w):
        self.wash[mint] = w


class FakeAPI:
    """Сделки и холдеры: по умолчанию — живая монета."""

    def __init__(self, dust=False, fail=False):
        self.dust, self.fail, self.calls = dust, fail, 0

    def trades(self, mint, limit=100):
        self.calls += 1
        if self.fail:
            raise RuntimeError("сеть")
        usd = "0.03" if self.dust else "12"
        return [{"userAddress": f"w{i}", "amountUsd": usd} for i in range(100)]

    def top_holders(self, mint):
        return [{"amount": 100.0 + i, "isDev": False, "isBundler": False, "isSniper": False}
                for i in range(30)]


class TestConsider:
    def test_первая_монета_проходит_фильтр_и_заводит_дева(self):
        st, api = FakeStore(), FakeAPI()
        added, why = poolmod.consider(coin(), dev(), poolmod.normalize({}), st, api, now=NOW)
        assert added and st.added == ["M1"]
        assert any(w.startswith("чисто") for w in why)

    def test_не_прошедшая_фильтр_монета_не_проверяется_на_накрутку(self):
        """Проверка стоит два запроса — только для кандидатов."""
        st, api = FakeStore(), FakeAPI()
        cfg = poolmod.normalize({"min_score": 99})
        added, _ = poolmod.consider(coin(), dev(), cfg, st, api, now=NOW)
        assert not added and api.calls == 0 and st.wash == {}

    def test_дев_в_пуле_минует_фильтр(self):
        """Знакомый дев: его новая монета поднимает его наверх, даже если
        по капе или правилам сейчас не прошла бы."""
        st, api = FakeStore(devs_in_pool={"DEV"}), FakeAPI()
        cfg = poolmod.normalize({"min_score": 99, "mcap_min": 10**9})
        added, why = poolmod.consider(coin(mint="M2"), dev(), cfg, st, api, now=NOW)
        assert added and st.added == ["M2"]
        assert why[0] == "дев уже в пуле"

    def test_дев_в_пуле_без_профиля_тоже_поднимается(self):
        st, api = FakeStore(devs_in_pool={"DEV"}), FakeAPI()
        added, _ = poolmod.consider(coin(mint="M2"), None, poolmod.normalize({}), st, api, now=NOW)
        assert added

    def test_накрутка_отсеивает_даже_знакомого_дева(self):
        st, api = FakeStore(devs_in_pool={"DEV"}), FakeAPI(dust=True)
        added, why = poolmod.consider(coin(mint="M2"), dev(), poolmod.normalize({}), st, api,
                                      now=NOW)
        assert not added and st.added == []
        assert any("накрутка: пыль" in w for w in why)
        assert st.wash["M2"]["suspicious"], "вердикт записан на монету, хотя в пул она не попала"

    def test_накрутка_отсеивает_нового_дева(self):
        st, api = FakeStore(), FakeAPI(dust=True)
        added, _ = poolmod.consider(coin(), dev(), poolmod.normalize({}), st, api, now=NOW)
        assert not added

    def test_выключенный_фильтр_накрутки_не_ходит_в_api(self):
        st, api = FakeStore(), FakeAPI(dust=True)
        cfg = poolmod.normalize({"wash_filter": False})
        added, why = poolmod.consider(coin(), dev(), cfg, st, api, now=NOW)
        assert added and api.calls == 0
        assert not any("накрутка" in w or "чисто" in w for w in why)

    def test_сбой_проверки_не_отсеивает(self):
        """Сеть упала — «не проверено», монету не теряем."""
        st, api = FakeStore(), FakeAPI(fail=True)
        added, why = poolmod.consider(coin(), dev(), poolmod.normalize({}), st, api, now=NOW)
        assert added
        assert any("не проверено" in w for w in why)

    def test_повторная_монета_не_добавляется_дважды(self):
        st, api = FakeStore(), FakeAPI()
        cfg = poolmod.normalize({})
        assert poolmod.consider(coin(), dev(), cfg, st, api, now=NOW)[0]
        assert not poolmod.consider(coin(), dev(), cfg, st, api, now=NOW)[0]


# --- хранилище ---

def dbcoin(mint, creator, **kw):
    c = {"mint": mint, "creator": creator, "symbol": mint, "name": mint,
         "created_timestamp": int(time.time() * 1000), "usd_market_cap": 1000.0}
    c.update(kw)
    return c


def profile(address, **kw):
    s = {"address": address, "username": None, "followers": 0, "total_coins": 1,
         "migrated": 0, "migration_rate": 0, "best_ath": 0, "median_ath": 0,
         "stayed_coins": 1, "first_launch": 0, "verdict": "x", "score": 5}
    s.update(kw)
    return s


@pytest.fixture
def st(tmp_path):
    return Store(str(tmp_path / "t.db"))


def add(st, mint, creator):
    st.upsert_coin(dbcoin(mint, creator))
    st.pool_add(mint, ["ok"])
    time.sleep(0.002)          # added_at в миллисекундах — порядок должен быть различим


class TestPoolDevs:
    def test_пусто(self, st):
        assert st.pool_devs() == []
        assert st.pool_total() == 0 and st.pool_unread() == 0 and st.pool_coins_total() == 0

    def test_монеты_группируются_по_деву(self, st):
        for m, d in (("A1", "A"), ("B1", "B"), ("A2", "A")):
            add(st, m, d)
        devs = st.pool_devs()
        assert [d["address"] for d in devs] == ["A", "B"], "дев с последней монетой — наверху"
        a = devs[0]
        assert [c["mint"] for c in a["coins"]] == ["A2", "A1"]
        assert a["latest"]["mint"] == "A2"
        assert a["unread"] == 2 and a["bumped_at"] > a["added_at"]
        assert st.pool_total() == 2 and st.pool_coins_total() == 3

    def test_новая_монета_поднимает_дева_наверх(self, st):
        add(st, "A1", "A")
        add(st, "B1", "B")
        assert [d["address"] for d in st.pool_devs()] == ["B", "A"]
        add(st, "A2", "A")
        assert [d["address"] for d in st.pool_devs()] == ["A", "B"]

    def test_непрочитанные_считаются_по_девам(self, st):
        add(st, "A1", "A")
        add(st, "A2", "A")
        add(st, "B1", "B")
        assert st.pool_unread() == 2, "два дева «горят», хотя монет три"
        st.pool_mark_seen()
        assert st.pool_unread() == 0
        assert all(d["unread"] == 0 for d in st.pool_devs())

    def test_pool_has_dev(self, st):
        st.upsert_coin(dbcoin("A1", "A"))
        assert not st.pool_has_dev("A")
        st.pool_add("A1", [])
        assert st.pool_has_dev("A") and not st.pool_has_dev("B")

    def test_профиль_дева_приклеивается(self, st):
        add(st, "A1", "A")
        assert st.pool_devs()[0]["dev"] is None
        st.save_creator(profile("A", username="alice"))
        assert st.pool_devs()[0]["dev"]["username"] == "alice"

    def test_вердикт_накрутки_на_монете(self, st):
        add(st, "A1", "A")
        assert st.pool_devs()[0]["latest"]["wash"] is None
        st.set_coin_wash("A1", {"suspicious": True, "flags": ["пыль"]})
        assert st.pool_devs()[0]["latest"]["wash"]["flags"] == ["пыль"]
        assert st.feed()[0]["wash"]["suspicious"] is True

    def test_лимит_по_девам_а_не_по_монетам(self, st):
        for i in range(5):
            add(st, f"M{i}", f"D{i % 2}")
        assert len(st.pool_devs(limit=1)) == 1

    def test_очистка(self, st):
        add(st, "A1", "A")
        assert st.pool_clear() == 1
        assert st.pool_devs() == [] and not st.pool_has_dev("A")


class TestМиграцияСхемы:
    def test_старая_таблица_coins_получает_колонку_wash(self, tmp_path):
        import sqlite3
        p = str(tmp_path / "old.db")
        db = sqlite3.connect(p)
        db.execute("""CREATE TABLE coins (mint TEXT PRIMARY KEY, creator TEXT,
                      created_timestamp INTEGER, mayhem TEXT, venue TEXT)""")
        db.commit()
        db.close()
        assert "wash" in Store(p)._cols("coins")

    def test_смена_ревизии_кеша_сбрасывает_снимки(self, tmp_path):
        p = str(tmp_path / "t.db")
        st = Store(p)
        st.save_creator(profile("A"))
        st.set_setting("cache_rev", Store.CACHE_REV - 1)
        st.db.close()
        assert Store(p).cached_creator("A") is None
