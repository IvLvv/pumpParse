"""Отбор ленты по окнам.

Окно правила и окно, которое видит пользователь в таблице, — разные вещи,
и именно на их расхождении строка выглядит не проходящей собственное условие.
Здесь проверяется серверная половина: по какому окну идёт отбор и что
происходит с девами, у которых нужного окна не посчитано.
"""
import re
from pathlib import Path

import pytest

from pumpparse.web import _apply_rules

INDEX_HTML = Path(__file__).resolve().parent.parent / "pumpparse" / "static" / "index.html"


def dev(**windows):
    """Профиль дева с указанными окнами: dev(**{"10": {...}})."""
    return {"windows": {k: dict(v) for k, v in windows.items()}}


def row(symbol, dev_profile):
    return {"symbol": symbol, "dev": dev_profile}


W10 = {"mig": 1, "coins": 10, "streak": 1, "gap": 4}
W20 = {"mig": 5, "coins": 19, "streak": 2, "gap": 4}


class TestВыборОкна:
    def test_правило_смотрит_своё_окно_а_не_десятку(self):
        """Ровно случай из UI: 1/10 в десятке, 5/19 в двадцатке."""
        rows = [row("TUCKER", dev(**{"10": W10, "20": W20}))]
        out, rules, stale = _apply_rules(rows, ["mig>=3@20"], True)
        assert [r["symbol"] for r in out] == ["TUCKER"]
        assert stale == 0
        assert [r.raw for r in rules] == ["mig>=3@20"]

    def test_то_же_условие_на_десятке_не_проходит(self):
        rows = [row("TUCKER", dev(**{"10": W10, "20": W20}))]
        out, _, _ = _apply_rules(rows, ["mig>=3@10"], True)
        assert out == []

    def test_разные_окна_в_одном_наборе(self):
        rows = [row("A", dev(**{"10": W10, "20": W20}))]
        out, _, _ = _apply_rules(rows, ["mig>=1@10", "mig>=3@20"], True)
        assert len(out) == 1
        out, _, _ = _apply_rules(rows, ["mig>=9@10", "mig>=3@20"], True)
        assert out == []

    def test_режим_любого_условия(self):
        rows = [row("A", dev(**{"10": W10, "20": W20}))]
        out, _, _ = _apply_rules(rows, ["mig>=9@10", "mig>=3@20"], False)
        assert len(out) == 1

    def test_без_правил_возвращается_всё(self):
        rows = [row("A", dev(**{"10": W10})), row("B", None)]
        out, rules, stale = _apply_rules(rows, [], True)
        assert out == rows
        assert rules == []
        assert stale == 0

    def test_пустые_строки_правил_игнорируются(self):
        rows = [row("A", dev(**{"10": W10}))]
        out, rules, _ = _apply_rules(rows, ["", "   "], True)
        assert out == rows
        assert rules == []


class TestНедостающиеОкна:
    def test_дев_без_нужного_окна_отсеивается_в_stale(self):
        """Профиль посчитан до появления окна — судить не по чему."""
        rows = [row("A", dev(**{"10": W10}))]
        out, _, stale = _apply_rules(rows, ["mig>=1@20"], True)
        assert out == []
        assert stale == 1

    def test_дев_без_профиля_тоже_stale(self):
        rows = [row("A", None)]
        out, _, stale = _apply_rules(rows, ["mig>=1@10"], True)
        assert out == []
        assert stale == 1

    def test_stale_не_смешивается_с_непрошедшими(self):
        """Иначе «условие никого не нашло» и «данных ещё нет» неразличимы."""
        rows = [
            row("нет-окна", dev(**{"10": W10})),
            row("не-прошёл", dev(**{"10": W10, "20": {"mig": 0, "coins": 20}})),
            row("прошёл", dev(**{"10": W10, "20": W20})),
        ]
        out, _, stale = _apply_rules(rows, ["mig>=3@20"], True)
        assert [r["symbol"] for r in out] == ["прошёл"]
        assert stale == 1

    def test_нужны_все_окна_из_набора(self):
        rows = [row("A", dev(**{"20": W20}))]
        out, _, stale = _apply_rules(rows, ["mig>=3@20", "mig>=1@10"], True)
        assert out == []
        assert stale == 1


class TestРазметкаСовпадений:
    def test_каждое_правило_отмечено_фактом(self):
        rows = [row("A", dev(**{"10": W10, "20": W20}))]
        out, _, _ = _apply_rules(rows, ["mig>=3@20", "gap<=9@20"], True)
        assert [m["rule"] for m in out[0]["matched"]] == ["mig>=3@20", "gap<=9@20"]
        assert all(m["ok"] for m in out[0]["matched"])

    def test_исходные_строки_не_портятся(self):
        """Разметка кладётся в копию: строки приходят прямо из БД."""
        original = row("A", dev(**{"20": W20}))
        rows = [original]
        _apply_rules(rows, ["mig>=3@20"], True)
        assert "matched" not in original

    def test_в_режиме_или_видно_какое_условие_сработало(self):
        rows = [row("A", dev(**{"10": W10, "20": W20}))]
        out, _, _ = _apply_rules(rows, ["mig>=9@20", "mig>=3@20"], False)
        assert [m["ok"] for m in out[0]["matched"]] == [False, True]


class TestСтолбецСреза:
    """Регрессия на интерфейс: столбец был жёстко прибит к окну 10.

    JS здесь не исполняется, поэтому проверяется само место, где ошибка и
    жила: окно приходит аргументом, а не берётся из ключа '10'.
    """

    @pytest.fixture
    def src(self):
        return INDEX_HTML.read_text(encoding="utf-8")

    def test_окно_столбца_передаётся_аргументом(self, src):
        assert "function winCell(dev, win)" in src
        assert not re.search(r"wins\['10'\]", src), "окно снова захардкожено"

    def test_окно_берётся_из_правил(self, src):
        assert "function rulesWindows(" in src
        assert "setWinHeader($('#th-win'), rulesWindows(d.rules))" in src

    def test_у_пула_своё_окно(self, src):
        """Пул отбирается своим набором правил, не тем, что на вкладке ленты."""
        assert "rulesWindows((d.config || {}).rules)" in src

    def test_заголовки_столбцов_подписываются_из_кода(self, src):
        assert 'id="th-win"' in src
        assert 'id="pth-win"' in src

    def test_столбец_не_подставляет_чужое_окно(self, src):
        """Показать десятку вместо запрошенной двадцатки — то же враньё."""
        assert "нет окна" in src
