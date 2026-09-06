"""Конфиг пула: нормализация присланного из UI и отбор монет."""
import time

import pytest

from pumpparse import pool as poolmod

NOW = 1_700_000_000.0


def coin(**kw):
    base = {"usd_market_cap": 50_000.0, "created_timestamp": int((NOW - 60) * 1000),
            "twitter": None, "telegram": None, "website": None}
    base.update(kw)
    return base


def dev(**kw):
    base = {"score": 50, "windows": {"10": {"mig": 3, "streak": 2, "coins": 10}}}
    base.update(kw)
    return base


class TestNormalize:
    def test_пустой_конфиг_даёт_умолчания(self):
        assert poolmod.normalize({}) == poolmod.DEFAULT
        assert poolmod.normalize(None) == poolmod.DEFAULT

    def test_посторонние_ключи_отбрасываются(self):
        cfg = poolmod.normalize({"enabled": True, "выдумка": 1, "__class__": "x"})
        assert "выдумка" not in cfg and "__class__" not in cfg

    def test_возвращается_копия(self):
        poolmod.normalize({})["mcap_min"] = 999
        assert poolmod.DEFAULT["mcap_min"] == 0.0

    @pytest.mark.parametrize("value", [1, "1", True, "да"])
    def test_флаги_приводятся_к_bool(self, value):
        assert poolmod.normalize({"enabled": value})["enabled"] is True

    def test_отрицательная_капа_подтягивается_к_нулю(self):
        assert poolmod.normalize({"mcap_min": -100})["mcap_min"] == 0.0

    @pytest.mark.parametrize("value", [None, "", 0])
    def test_пустой_потолок_капы_значит_без_ограничения(self, value):
        assert poolmod.normalize({"mcap_max": value})["mcap_max"] is None

    def test_нулевой_возраст_значит_без_ограничения(self):
        assert poolmod.normalize({"age_max_sec": 0})["age_max_sec"] is None

    def test_пустые_правила_отсеиваются(self):
        assert poolmod.normalize({"rules": ["mig>=3@10", "", "   "]})["rules"] == ["mig>=3@10"]

    def test_битое_правило_падает_сразу(self):
        """Иначе ошибка всплыла бы на каждой монете внутри воркера."""
        with pytest.raises(ValueError):
            poolmod.normalize({"rules": ["мусор"]})

    @pytest.mark.parametrize("cfg", [
        {"mcap_min": "не число"}, {"age_max_sec": "не число"}, {"min_score": "x"},
    ])
    def test_нечисловые_значения_отвергаются(self, cfg):
        with pytest.raises(ValueError):
            poolmod.normalize(cfg)


class TestMatch:
    def test_подходящая_монета(self):
        ok, why = poolmod.match(coin(), dev(), poolmod.normalize({}), now=NOW)
        assert ok and why

    def test_ниже_нижней_капы(self):
        cfg = poolmod.normalize({"mcap_min": 100_000})
        assert poolmod.match(coin(), dev(), cfg, now=NOW)[0] is False

    def test_выше_верхней_капы(self):
        cfg = poolmod.normalize({"mcap_max": 10_000})
        assert poolmod.match(coin(), dev(), cfg, now=NOW)[0] is False

    def test_слишком_старая(self):
        cfg = poolmod.normalize({"age_max_sec": 30})
        assert poolmod.match(coin(), dev(), cfg, now=NOW)[0] is False

    def test_без_ограничения_возраста_проходит_старая(self):
        cfg = poolmod.normalize({"age_max_sec": 0})
        old = coin(created_timestamp=int((NOW - 90000) * 1000))
        assert poolmod.match(old, dev(), cfg, now=NOW)[0] is True

    def test_требование_соцсетей(self):
        cfg = poolmod.normalize({"require_social": True})
        assert poolmod.match(coin(), dev(), cfg, now=NOW)[0] is False
        assert poolmod.match(coin(twitter="https://x.com/a"), dev(), cfg, now=NOW)[0] is True

    def test_без_профиля_дева_не_проходит(self):
        """Профиль ещё не посчитан — судить не по чему, монету не берём."""
        assert poolmod.match(coin(), None, poolmod.normalize({}), now=NOW)[0] is False

    def test_низкий_score(self):
        cfg = poolmod.normalize({"min_score": 80})
        assert poolmod.match(coin(), dev(), cfg, now=NOW)[0] is False

    def test_правило_по_деву(self):
        cfg = poolmod.normalize({"rules": ["mig>=3@10"]})
        assert poolmod.match(coin(), dev(), cfg, now=NOW)[0] is True
        cfg = poolmod.normalize({"rules": ["mig>=5@10"]})
        assert poolmod.match(coin(), dev(), cfg, now=NOW)[0] is False

    def test_непосчитанное_окно_не_проходит(self):
        cfg = poolmod.normalize({"rules": ["mig>=1@50"]})
        assert poolmod.match(coin(), dev(), cfg, now=NOW)[0] is False

    def test_режим_любого_условия(self):
        cfg = poolmod.normalize({"rules": ["mig>=99@10", "streak>=2@10"], "any": True})
        assert poolmod.match(coin(), dev(), cfg, now=NOW)[0] is True
        cfg = poolmod.normalize({"rules": ["mig>=99@10", "streak>=2@10"], "any": False})
        assert poolmod.match(coin(), dev(), cfg, now=NOW)[0] is False

    def test_причины_перечисляют_сработавшее(self):
        cfg = poolmod.normalize({"rules": ["mig>=3@10"]})
        ok, why = poolmod.match(coin(), dev(), cfg, now=NOW)
        assert ok
        assert "mig>=3@10" in why
        assert any("капа" in w for w in why)

    def test_монета_без_капы_считается_нулевой(self):
        cfg = poolmod.normalize({"mcap_min": 1})
        assert poolmod.match(coin(usd_market_cap=None), dev(), cfg, now=NOW)[0] is False

    def test_now_по_умолчанию_текущее_время(self):
        свежая = coin(created_timestamp=int(time.time() * 1000))
        cfg = poolmod.normalize({"age_max_sec": 900})
        assert poolmod.match(свежая, dev(), cfg)[0] is True


class TestПорядокПроверок:
    """Порядок важен: в «почему прошла» видно лишь пройденное до отказа."""

    def test_отказ_по_капе_не_доходит_до_возраста(self):
        cfg = poolmod.normalize({"mcap_min": 100_000})
        ok, why = poolmod.match(coin(), dev(), cfg, now=NOW)
        assert ok is False
        assert why == [], "капа проверяется первой, причин накопиться не должно"

    def test_отказ_по_возрасту_оставляет_причину_по_капе(self):
        cfg = poolmod.normalize({"age_max_sec": 10})
        ok, why = poolmod.match(coin(), dev(), cfg, now=NOW)
        assert ok is False
        assert any("капа" in w for w in why)
        assert not any("возраст" in w for w in why)

    def test_отказ_по_score_оставляет_капу_и_возраст(self):
        cfg = poolmod.normalize({"min_score": 99})
        ok, why = poolmod.match(coin(), dev(), cfg, now=NOW)
        assert ok is False
        assert any("капа" in w for w in why)
        assert any("возраст" in w for w in why)

    def test_профиль_дева_проверяется_после_соцсетей(self):
        """Без профиля судить не по чему, но капу с возрастом уже записали."""
        cfg = poolmod.normalize({})
        ok, why = poolmod.match(coin(), None, cfg, now=NOW)
        assert ok is False
        assert any("капа" in w for w in why)


class TestГраницыИзИнтерфейса:
    """Значения, которые интерфейс шлёт как «без ограничения»."""

    def test_ноль_в_потолке_капы_снимает_границу(self):
        cfg = poolmod.normalize({"mcap_max": 0})
        дорогая = coin(usd_market_cap=10_000_000.0)
        assert poolmod.match(дорогая, dev(), cfg, now=NOW)[0] is True

    def test_ноль_в_свежести_снимает_границу(self):
        cfg = poolmod.normalize({"age_max_sec": 0})
        старая = coin(created_timestamp=int((NOW - 30 * 86400) * 1000))
        assert poolmod.match(старая, dev(), cfg, now=NOW)[0] is True

    def test_монета_без_капы_считается_нулевой_но_проходит_при_нуле(self):
        cfg = poolmod.normalize({"mcap_min": 0})
        assert poolmod.match(coin(usd_market_cap=None), dev(), cfg, now=NOW)[0] is True

    @pytest.mark.parametrize("value", [60, 300, 900, 3600, 86400])
    def test_все_варианты_свежести_из_интерфейса_разбираются(self, value):
        assert poolmod.normalize({"age_max_sec": value})["age_max_sec"] == value
