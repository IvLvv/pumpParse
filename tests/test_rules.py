"""Разбор и применение правил отбора девов."""
import pytest

from pumpparse import rules as rulemod

WINDOWS = {
    "10": {"mig": 3, "ray": 0, "pswap": 3, "streak": 2, "rate": 0.3, "stay": 0.7,
           "medath": 40000.0, "bestath": 120000.0, "pace": 4.0, "gap": 1,
           "social": 0.5, "coins": 10},
    "5": {"mig": 1, "streak": 1, "rate": 0.2, "coins": 5},
}


class TestParse:
    @pytest.mark.parametrize("text,metric,op,value,window", [
        ("mig>=3@10", "mig", ">=", 3.0, 10),
        ("  streak > 2 @ 20  ", "streak", ">", 2.0, 20),
        ("rate>=0.3@10", "rate", ">=", 0.3, 10),
        ("gap<=0@5", "gap", "<=", 0.0, 5),
        ("bestath>=100000@10", "bestath", ">=", 100000.0, 10),
        ("coins==5@5", "coins", "==", 5.0, 5),
        ("medath<-1@10", "medath", "<", -1.0, 10),
    ])
    def test_разбор(self, text, metric, op, value, window):
        r = rulemod.parse(text)
        assert (r.metric, r.op, r.value, r.window) == (metric, op, value, window)

    @pytest.mark.parametrize("old,new", [("grad>=3@10", "mig"), ("dead<=0.5@10", "stay")])
    def test_старые_имена_метрик(self, old, new):
        assert rulemod.parse(old).metric == new

    def test_исходный_текст_сохраняется(self):
        assert rulemod.parse("  mig>=3@10 ").raw == "mig>=3@10"

    @pytest.mark.parametrize("text", [
        "", "мусор", "mig>=3", "mig@10", ">=3@10", "mig>=@10", "mig=>3@10",
        "mig>=3@", "mig>=3@0", "mig>=3@-1", "mig>=abc@10", "mig>=3@1.5",
    ])
    def test_кривое_правило_отвергается(self, text):
        with pytest.raises(ValueError):
            rulemod.parse(text)

    def test_неизвестная_метрика(self):
        with pytest.raises(ValueError, match="неизвестная метрика"):
            rulemod.parse("выдумка>=1@10")

    def test_окно_меньше_единицы(self):
        with pytest.raises(ValueError, match="окно"):
            rulemod.parse("mig>=3@0")


class TestPresets:
    def test_все_пресеты_разбираются(self):
        for name, texts in rulemod.PRESETS.items():
            assert texts, name
            for t in texts:
                rulemod.parse(t)

    def test_пресет_разворачивается(self):
        rules = rulemod.parse_all([], ["quality"])
        assert sorted(r.raw for r in rules) == ["coins>=5@10", "rate>=0.3@10"]

    def test_правила_и_пресеты_складываются(self):
        assert len(rulemod.parse_all(["mig>=1@5"], ["runner"])) == 2

    def test_неизвестный_пресет(self):
        with pytest.raises(ValueError, match="неизвестный пресет"):
            rulemod.parse_all([], ["выдумка"])

    def test_пустой_ввод(self):
        assert rulemod.parse_all(None, None) == []


class TestMatch:
    def test_условие_выполняется(self):
        assert rulemod.parse("mig>=3@10").test(WINDOWS)

    def test_условие_не_выполняется(self):
        assert not rulemod.parse("mig>=4@10").test(WINDOWS)

    def test_отсутствующее_окно_не_проходит(self):
        assert not rulemod.parse("mig>=1@50").test(WINDOWS)

    def test_отсутствующая_метрика_не_проходит(self):
        """Окно посчитано по старым правилам — судить не по чему."""
        assert not rulemod.parse("bestath>=1@5").test(WINDOWS)

    def test_режим_и(self):
        rules = [rulemod.parse("mig>=3@10"), rulemod.parse("gap<=1@10")]
        assert rulemod.match(rules, WINDOWS, require_all=True)
        rules.append(rulemod.parse("mig>=99@10"))
        assert not rulemod.match(rules, WINDOWS, require_all=True)

    def test_режим_или(self):
        rules = [rulemod.parse("mig>=99@10"), rulemod.parse("gap<=1@10")]
        assert rulemod.match(rules, WINDOWS, require_all=False)
        assert not rulemod.match(rules, WINDOWS, require_all=True)

    def test_пустой_набор_пропускает_всех(self):
        assert rulemod.match([], WINDOWS)
        assert rulemod.match([], {})

    def test_объяснение_показывает_факт(self):
        assert "3" in rulemod.parse("mig>=3@10").explain(WINDOWS)
        assert "n/a" in rulemod.parse("mig>=3@50").explain(WINDOWS)


class TestWindowsNeeded:
    def test_собирает_уникальные(self):
        rules = [rulemod.parse("mig>=1@5"), rulemod.parse("gap<=1@20"),
                 rulemod.parse("rate>=0.1@5")]
        assert rulemod.windows_needed(rules) == [5, 20]

    def test_без_правил_окно_по_умолчанию(self):
        assert rulemod.windows_needed([]) == [10]
