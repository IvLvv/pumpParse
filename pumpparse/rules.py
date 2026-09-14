"""Правила отбора девов по окну последних запусков.

Формат правила:  <метрика><оператор><значение>@<окно>[/<период>]
Примеры:         mig>=3@10   streak>=3@10   rate>=0.3@10   gap<=2@20   streak>=3@10/24h

Окно — сколько последних монет дева брать в расчёт. Если монет меньше,
считается по тому, что есть (правило вроде mig>=3@10 у новичка просто не сработает).

Период сужает окно по времени: `streak>=3@10/24h` — из монет, запущенных
за последние сутки, берутся последние 10, и уже в них ищется серия. Так
«3 миграции подряд» у дева, который последний раз запускался полгода назад,
не выглядят так же, как у дева, сделавшего их сегодня. Без периода —
вся история, как раньше.
"""
import re

OPS = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    "==": lambda a, b: a == b,
}

METRICS = {
    "mig":     "миграций в окне, любая площадка (штук)",
    "ray":     "миграций именно в Raydium (на новых монетах всегда 0 — pump.fun льёт в PumpSwap)",
    "pswap":   "миграций в PumpSwap, собственный AMM pump.fun (штук)",
    "streak":  "самая длинная серия миграций подряд в окне (штук)",
    "rate":    "доля миграций в окне (0..1)",
    "stay":    "доля оставшихся на кривой в окне (0..1)",
    "medath":  "медианный ATH в окне, $",
    "bestath": "лучший ATH в окне, $",
    "pace":    "запусков в сутки внутри окна",
    "gap":     "монет запущено с момента последней миграции",
    "social":  "доля монет с привязанным X/TG/сайтом (0..1)",
    "coins":   "сколько монет реально попало в окно",
}

# Старые имена из сохранённых правил и конфигов пула. dead был долей монет,
# не взлетевших за час; ближайший смысл в новой модели — доля оставшихся.
ALIASES = {"grad": "mig", "dead": "stay"}

# Периоды, которые воркер считает заранее, — только их можно указывать в правиле.
# Ключ — как период пишется в правиле и в снимке дева, значение — секунды.
PERIODS = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000}

RULE_RE = re.compile(
    r"^\s*(\w+)\s*(>=|<=|==|>|<)\s*([-\d.]+)\s*@\s*(\d+)\s*(?:/\s*(\w+))?\s*$")


def window_key(window, period=None):
    """Ключ среза в снимке дева: '10' — вся история, '10/24h' — за сутки."""
    return f"{window}/{period}" if period else str(window)

PRESETS = {
    "runner":  ["mig>=3@10"],                       # 3 миграции из последних 10
    "series":  ["streak>=3@10"],                    # 3 миграции подряд внутри последних 10
    "hot":     ["mig>=2@5"],                        # дев в ударе прямо сейчас
    "quality": ["rate>=0.3@10", "coins>=5@10"],     # стабильно мигрирует, есть история
    "fresh":   ["mig>=1@5", "pace<=5@10"],          # недавняя миграция, не конвейер
    "whale":   ["bestath>=100000@10"],              # хотя бы одна монета была крупной
    "warm":    ["gap<=3@20"],                       # миграция была совсем недавно
}


class Rule:
    def __init__(self, metric, op, value, window, raw, period=None):
        self.metric, self.op, self.value, self.window, self.raw = metric, op, value, window, raw
        self.period = period

    @property
    def key(self):
        return window_key(self.window, self.period)

    def test(self, windows):
        got = windows.get(self.key, {}).get(self.metric)
        if got is None:
            return False
        return OPS[self.op](got, self.value)

    def explain(self, windows):
        got = windows.get(self.key, {}).get(self.metric)
        shown = "n/a" if got is None else (f"{got:g}" if isinstance(got, float) else got)
        return f"{self.raw} (факт: {shown})"


def parse(text):
    m = RULE_RE.match(text)
    if not m:
        raise ValueError(f"не разобрать правило {text!r}, нужен вид mig>=3@10 или mig>=3@10/24h")
    metric, op, value, window = m.group(1), m.group(2), float(m.group(3)), int(m.group(4))
    period = m.group(5)
    metric = ALIASES.get(metric, metric)
    if metric not in METRICS:
        raise ValueError(f"неизвестная метрика {metric!r}; есть: {', '.join(METRICS)}")
    if window < 1:
        raise ValueError("окно должно быть >= 1")
    if period is not None and period not in PERIODS:
        raise ValueError(f"неизвестный период {period!r}; есть: {', '.join(PERIODS)}")
    return Rule(metric, op, value, window, text.strip(), period)


def parse_all(rule_texts, preset_names):
    rules = [parse(t) for t in rule_texts or []]
    for name in preset_names or []:
        if name not in PRESETS:
            raise ValueError(f"неизвестный пресет {name!r}; есть: {', '.join(PRESETS)}")
        rules += [parse(t) for t in PRESETS[name]]
    return rules


def windows_needed(rules):
    return sorted({r.window for r in rules}) or [10]


def periods_needed(rules):
    return sorted({r.period for r in rules if r.period}, key=PERIODS.get)


def keys_needed(rules):
    """Срезы (окно/период), которые должны быть в снимке дева, чтобы применить правила."""
    return {r.key for r in rules}


def match(rules, windows, require_all=True):
    if not rules:
        return True
    results = [r.test(windows) for r in rules]
    return all(results) if require_all else any(results)
