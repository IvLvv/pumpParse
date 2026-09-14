"""Период в правиле: streak>=3@10/24h — серия из последних 10 монет за сутки.

Без периода «3 подряд» у дева, запускавшегося полгода назад, выглядят так же,
как у дева, сделавшего их сегодня. Период режет окно по времени до того, как
из него берутся последние n монет.
"""
import time

import pytest

from pumpparse import analysis
from pumpparse import rules as rulemod
from pumpparse.web import _apply_rules

NOW = 1_700_000_000_000
H = 3_600_000


def coins(*hours_ago, migrated=()):
    """Монеты дева, свежие первыми; migrated — индексы мигрировавших."""
    return [{"created_timestamp": NOW - h * H, "complete": i in migrated}
            for i, h in enumerate(hours_ago)]


class TestParse:
    @pytest.mark.parametrize("text,window,period,key", [
        ("mig>=3@10", 10, None, "10"),
        ("streak>=3@10/24h", 10, "24h", "10/24h"),
        ("  streak > 3 @ 10 / 7d ", 10, "7d", "10/7d"),
        ("rate>=0.5@5/1h", 5, "1h", "5/1h"),
    ])
    def test_разбор(self, text, window, period, key):
        r = rulemod.parse(text)
        assert (r.window, r.period, r.key) == (window, period, key)

    @pytest.mark.parametrize("text", ["mig>=3@10/2w", "mig>=3@10/", "mig>=3@10/24", "mig>=3@10/h"])
    def test_неизвестный_период(self, text):
        with pytest.raises(ValueError):
            rulemod.parse(text)

    def test_все_периоды_из_списка_разбираются(self):
        for p in rulemod.PERIODS:
            assert rulemod.parse(f"mig>=1@10/{p}").period == p

    def test_keys_и_periods_needed(self):
        rs = [rulemod.parse(t) for t in ("mig>=3@10", "streak>=2@10/24h", "mig>=1@5/1h")]
        assert rulemod.keys_needed(rs) == {"10", "10/24h", "5/1h"}
        assert rulemod.periods_needed(rs) == ["1h", "24h"]
        assert rulemod.windows_needed(rs) == [5, 10]

    def test_window_key(self):
        assert rulemod.window_key(10) == "10"
        assert rulemod.window_key(10, "24h") == "10/24h"


class TestWindowMetrics:
    def test_период_отсекает_старые_монеты(self):
        cs = coins(1, 2, 3, 30, 31, 32, migrated={3, 4, 5})
        assert analysis.window_metrics(cs, 10, NOW)["streak"] == 3
        w = analysis.window_metrics(cs, 10, NOW, "24h")
        assert w["coins"] == 3
        assert w["streak"] == 0 and w["mig"] == 0

    def test_окно_режется_после_периода(self):
        """Из монет за сутки берутся последние n, а не наоборот."""
        cs = coins(*range(2, 22), migrated=set(range(0, 20, 2)))   # 20 монет за 20 часов
        w = analysis.window_metrics(cs, 5, NOW, "24h")
        assert w["coins"] == 5
        assert analysis.window_metrics(cs, 5, NOW, "6h")["coins"] == 5
        assert analysis.window_metrics(cs, 5, NOW, "1h")["coins"] == 0

    def test_граница_периода_включительно(self):
        assert analysis.window_metrics(coins(1), 10, NOW, "1h")["coins"] == 1

    def test_пустой_период_даёт_нули(self):
        w = analysis.window_metrics(coins(48, 49), 10, NOW, "24h")
        assert w["coins"] == 0 and w["streak"] == 0

    def test_серия_за_сутки_видна(self):
        cs = coins(1, 2, 3, 100, migrated={0, 1, 2})
        assert analysis.window_metrics(cs, 10, NOW, "24h")["streak"] == 3

    def test_монета_без_времени_вне_периода(self):
        cs = [{"created_timestamp": None, "complete": True}]
        assert analysis.window_metrics(cs, 10, NOW, "24h")["coins"] == 0
        assert analysis.window_metrics(cs, 10, NOW)["coins"] == 1

    def test_window_keys(self):
        assert analysis.window_keys((5, 10), ("24h",)) == ["5", "5/24h", "10", "10/24h"]
        assert analysis.window_keys((10,)) == ["10"]


class FakeAPI:
    """profile_creator берёт «сейчас» из часов, поэтому монеты сдвигаются к реальному времени."""

    def __init__(self, coins):
        shift = int(time.time() * 1000) - NOW
        self._coins = [dict(c, created_timestamp=c["created_timestamp"] + shift) for c in coins]

    def creator_coins(self, address, max_coins=300):
        return list(self._coins)

    def user(self, address):
        return {"username": "dev"}


class TestProfile:
    def test_снимок_содержит_все_срезы(self):
        p = analysis.profile_creator(FakeAPI(coins(1, 2, 30)), "A",
                                     windows=(5, 10), periods=("1h", "24h"))
        assert set(p["windows"]) == {"5", "5/1h", "5/24h", "10", "10/1h", "10/24h"}
        assert p["windows"]["10"]["coins"] == 3
        assert p["windows"]["10/24h"]["coins"] == 2

    def test_кеш_без_периодов_не_годится(self):
        """Снимок, посчитанный до появления периодов, надо пересчитать."""
        class Store:
            def __init__(self):
                self.saved = []

            def cached_creator(self, address, max_age):
                return {"windows": {"5": {}, "10": {}}}

            def save_creator(self, s):
                self.saved.append(s)

        st = Store()
        p = analysis.profile_creator(FakeAPI(coins(1)), "A", store=st,
                                     windows=(5, 10), periods=("24h",))
        assert "10/24h" in p["windows"]
        assert st.saved, "профиль пересчитан и сохранён"

    def test_кеш_с_периодами_используется(self):
        class Store:
            def cached_creator(self, address, max_age):
                return {"windows": {"5": {}, "10": {}, "5/24h": {}, "10/24h": {}}, "cached": True}

        p = analysis.profile_creator(FakeAPI(coins(1)), "A", store=Store(),
                                     windows=(5, 10), periods=("24h",))
        assert p.get("cached")


class TestFeed:
    def test_правило_с_периодом_смотрит_свой_срез(self):
        dev = {"windows": {"10": {"streak": 3}, "10/24h": {"streak": 0}}}
        rows = [{"symbol": "A", "dev": dev}]
        assert _apply_rules(rows, ["streak>=3@10"], True)[0]
        assert _apply_rules(rows, ["streak>=3@10/24h"], True)[0] == []

    def test_нет_среза_с_периодом_значит_stale(self):
        rows = [{"symbol": "A", "dev": {"windows": {"10": {"streak": 3}}}}]
        out, _, stale = _apply_rules(rows, ["streak>=3@10/24h"], True)
        assert out == [] and stale == 1
