"""Накрутка: вердикт по сделкам и холдерам.

Наборы данных повторяют реальные срезы pump.fun (сентябрь 2026), по которым
подбирались пороги в wash.py — см. там же, откуда какие числа.
"""
import pytest

from pumpparse import wash


def trade(user, usd):
    return {"userAddress": user, "amountUsd": str(usd), "type": "buy"}


def holder(amount, **flags):
    h = {"address": "h" + str(amount), "amount": amount,
         "isDev": False, "isSniper": False, "isBundler": False}
    h.update(flags)
    return h


def organic():
    """Живая монета: сотня сделок от 65 кошельков по $5–100, холдеры разные."""
    trades = [trade(f"w{i % 65}", 5 + i) for i in range(100)]
    holders = [holder(1000.0 + i * 37.5) for i in range(50)]
    return trades, holders


class TestЖивыеМонеты:
    def test_живая_монета_чиста(self):
        w = wash.assess({}, *organic())
        assert w["suspicious"] is False
        assert w["flags"] == []
        assert w["trades"] == 100 and w["wallets"] == 65

    def test_мало_сделок_не_повод(self):
        """У монеты трёх минут от роду 11 сделок от 3 кошельков — это норма, а не самокрутка."""
        trades = [trade(f"w{i % 3}", 0.5) for i in range(11)]
        w = wash.assess({}, trades, [holder(1.0), holder(2.0)])
        assert w["suspicious"] is False

    def test_пара_совпавших_балансов_не_клоны(self):
        """RAIN, CINO: 3–4 одинаковых круглых закупа у живой монеты."""
        trades, holders = organic()
        holders[:4] = [holder(5000.0) for _ in range(4)]
        assert wash.assess({}, trades, holders)["suspicious"] is False

    def test_пустые_данные_чисты(self):
        w = wash.assess({}, [], [])
        assert w["suspicious"] is False
        assert w["trades"] == 0 and w["holders"] == 0 and w["ratio"] == 0.0


class TestПризнаки:
    def test_пыль_бот_объём(self):
        """ADAMITY: 100 сделок по $0.04 от 100 кошельков за 10 секунд."""
        trades = [trade(f"w{i}", 0.04) for i in range(100)]
        w = wash.assess({"boost_mode": "IN_PROGRESS"}, trades, [holder(i) for i in range(50)])
        assert w["suspicious"] and w["flags"] == ["пыль"]
        assert w["dust_share"] >= 0.9
        assert w["boost"] == "IN_PROGRESS"

    def test_самокрутка_двумя_кошельками(self):
        """beer: 100 сделок от двух адресов."""
        trades = [trade(f"w{i % 2}", 5.0) for i in range(100)]
        w = wash.assess({}, trades, [holder(1.0), holder(2.0), holder(3.0)])
        assert w["flags"] == ["самокрутка"]
        assert w["ratio"] == 50.0

    def test_семь_кошельков_на_сотню_сделок_тоже_самокрутка(self):
        """Pink Bull: 100 сделок от 7 адресов — 14 на кошелёк."""
        trades = [trade(f"w{i % 7}", 2.0) for i in range(100)]
        assert "самокрутка" in wash.assess({}, trades, [])["flags"]

    def test_клоны_холдеры_с_одинаковым_балансом(self):
        """NYT Coin: 42 из 50 холдеров с ровно 911 692.528 токенов."""
        trades, _ = organic()
        holders = [holder(911692.528432) for _ in range(42)] + [holder(i) for i in range(8)]
        w = wash.assess({}, trades, holders)
        assert "клоны" in w["flags"]
        assert w["clones"] == 42

    def test_баланс_дева_в_клоны_не_считается(self):
        trades, holders = organic()
        # Пять записей одного и того же баланса, но четыре из них — сам дев.
        holders[:5] = [holder(777.0, isDev=True) for _ in range(4)] + [holder(777.0)]
        assert "клоны" not in wash.assess({}, trades, holders)["flags"]

    def test_бандл_половина_топа(self):
        """SHI в первые секунды: 15 из 26 холдеров помечены бандлерами."""
        trades, _ = organic()
        holders = [holder(i, isBundler=True, isSniper=True) for i in range(15)]
        holders += [holder(100 + i) for i in range(11)]
        w = wash.assess({}, trades, holders)
        assert "бандл" in w["flags"]
        assert w["bundlers"] == 15 and w["snipers"] == 15

    def test_бандлеров_меньше_половины_не_флаг(self):
        """BUNNY: 12 из 50 — снайперы бывают и у живых запусков."""
        trades, _ = organic()
        holders = [holder(i, isBundler=True) for i in range(12)]
        holders += [holder(100 + i) for i in range(38)]
        assert "бандл" not in wash.assess({}, trades, holders)["flags"]

    def test_несколько_признаков_сразу(self):
        """NYT Coin: и пыль, и клоны."""
        trades = [trade(f"w{i}", 0.02) for i in range(100)]
        holders = [holder(911692.53) for _ in range(42)] + [holder(i) for i in range(8)]
        assert set(wash.assess({}, trades, holders)["flags"]) == {"пыль", "клоны"}

    def test_битые_суммы_не_роняют(self):
        trades = [{"userAddress": "a", "amountUsd": None}, {"userAddress": None}]
        assert wash.assess({}, trades, [{"amount": None}])["suspicious"] is False


class FakeAPI:
    def __init__(self, trades=(), holders=(), fail=False):
        self._trades, self._holders, self.fail = list(trades), list(holders), fail
        self.calls = []

    def trades(self, mint, limit=100):
        self.calls.append(("trades", mint, limit))
        if self.fail:
            raise RuntimeError("pump.fun API недоступен")
        return self._trades

    def top_holders(self, mint):
        self.calls.append(("holders", mint))
        return self._holders


class TestInspect:
    def test_запрашивает_сделки_и_холдеров(self):
        api = FakeAPI(*organic())
        w = wash.inspect(api, {"mint": "M"})
        assert w["suspicious"] is False
        assert [c[0] for c in api.calls] == ["trades", "holders"]
        assert api.calls[0][2] <= 100, "сервер отдаёт не больше 100 сделок за страницу"

    def test_сбой_сети_не_вердикт(self):
        """Не проверено ≠ накрутка: монету не отсеиваем, но и чистой не зовём."""
        w = wash.inspect(FakeAPI(fail=True), {"mint": "M", "boost_mode": "NONE"})
        assert w["suspicious"] is False
        assert "error" in w
        assert "не проверено" in wash.label(w)


class TestLabel:
    def test_подписи(self):
        assert wash.label(None) == "накрутка: не проверялась"
        w = {"suspicious": True, "flags": ["пыль", "клоны"]}
        assert wash.label(w) == "накрутка: пыль, клоны"
        clean = {"suspicious": False, "flags": [], "trades": 70, "wallets": 30}
        assert wash.label(clean) == "чисто: 70 сделок от 30 кошельков"
        few = {"suspicious": False, "flags": [], "trades": 7, "wallets": 3}
        assert wash.label(few) == "мало данных: 7 сделок"


@pytest.mark.parametrize("share", [0.0, 0.25])
def test_доля_пыли_живых_монет_ниже_порога(share):
    n = 100
    trades = [trade(f"w{i}", 0.5 if i < n * share else 10) for i in range(n)]
    assert "пыль" not in wash.assess({}, trades, [])["flags"]
