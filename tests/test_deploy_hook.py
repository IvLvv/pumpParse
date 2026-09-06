"""Хук выкладки прогоняется целиком, с подставными git, pip, sudo и curl.

Чтение исходника ловит опечатки, но не поведение: важно, что при упавшем
сервисе происходит откат, а `git push` возвращает ненулевой код. Настоящие
команды подменены заглушками, которые пишут вызовы в журнал, поэтому
проверять можно и порядок действий.
"""
import os
import shutil
import subprocess

import pytest

from .conftest import ROOT

HOOK = ROOT / "deploy" / "post-receive"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash недоступен")

NEW = "a" * 40
PREV = "b" * 40
MAIN = "refs/heads/main"

# Заглушки пишут в $CALLS свою командную строку. Поведение задаётся снаружи:
# HEALTH_OK — отвечает ли сервис, RESTART_OK — удаётся ли перезапуск.
STUB_GIT = """#!/usr/bin/env bash
echo "git $*" >> "$CALLS"
case "$*" in
  *rev-parse*) echo "${PREV_SHA:-}"; [ -n "${PREV_SHA:-}" ] || exit 1 ;;
  *log*)       echo "abcdef краткое описание" ;;
esac
exit 0
"""

STUB_UV = """#!/usr/bin/env bash
echo "uv $*" >> "$CALLS"
exit "${PIP_RC:-0}"
"""

STUB_SUDO = """#!/usr/bin/env bash
echo "sudo $*" >> "$CALLS"
exit "${RESTART_RC:-0}"
"""

STUB_CURL = """#!/usr/bin/env bash
echo "curl $*" >> "$CALLS"
[ "${HEALTH_OK:-1}" = "1" ] || exit 7
exit 0
"""

STUB_SEQ = """#!/usr/bin/env bash
n=1; while [ "$n" -le "$1" ]; do echo "$n"; n=$((n + 1)); done
"""

STUB_SLEEP = """#!/usr/bin/env bash
exit 0
"""


@pytest.fixture
def hook(tmp_path):
    """Копия хука, нацеленная на временный каталог, с заглушками в PATH."""
    root = tmp_path / "srv"
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "app").mkdir()

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("git", STUB_GIT), ("sudo", STUB_SUDO), ("curl", STUB_CURL),
                       ("seq", STUB_SEQ), ("sleep", STUB_SLEEP)):
        p = bin_dir / name
        p.write_text(body, encoding="utf-8", newline="\n")
        p.chmod(0o755)
    # uv вызывается по абсолютному пути, поэтому подменяется не через PATH,
    # а подстановкой UV_BIN в копии хука.
    uv = tmp_path / "uv"
    uv.write_text(STUB_UV, encoding="utf-8", newline="\n")
    uv.chmod(0o755)
    (root / "venv" / "bin" / "python").write_text("", encoding="utf-8")

    src = HOOK.read_text(encoding="utf-8")
    src = src.replace("ROOT=/srv/pumpparse", f'ROOT="{root.as_posix()}"')
    src = src.replace("UV_BIN=/usr/local/bin/uv", f'UV_BIN="{uv.as_posix()}"')
    src = src.replace("HEALTH_TRIES=15", "HEALTH_TRIES=2")
    script = tmp_path / "post-receive"
    script.write_text(src, encoding="utf-8", newline="\n")

    calls = tmp_path / "calls.log"
    calls.write_text("", encoding="utf-8")

    def run(stdin, **env):
        e = dict(os.environ)
        e["PATH"] = f"{bin_dir.as_posix()}{os.pathsep}{e['PATH']}"
        e["CALLS"] = calls.as_posix()
        e.setdefault("PREV_SHA", PREV)
        e.update({k: str(v) for k, v in env.items()})
        # Строго байты: с text=True Windows подменил бы \n на \r\n, и хук
        # получил бы имя ветки с хвостовым CR — git так никогда не пишет.
        r = subprocess.run([BASH, str(script)], input=stdin.encode(), env=e,
                           capture_output=True, timeout=60)
        r.stdout = r.stdout.decode("utf-8", "replace")
        r.stderr = r.stderr.decode("utf-8", "replace")
        return r, calls.read_text(encoding="utf-8")

    return run


def checkouts(log):
    """SHA всех выкаток по порядку — по ним видно, был ли откат."""
    return [line.split()[-1] for line in log.splitlines() if "checkout -f" in line]


class TestУспешнаяВыкладка:
    def test_код_возврата_ноль(self, hook):
        r, _ = hook(f"{PREV} {NEW} {MAIN}\n")
        assert r.returncode == 0, r.stderr

    def test_разворачивает_присланный_коммит(self, hook):
        _, log = hook(f"{PREV} {NEW} {MAIN}\n")
        assert checkouts(log) == [NEW]

    def test_ставит_пакет_и_перезапускает(self, hook):
        _, log = hook(f"{PREV} {NEW} {MAIN}\n")
        assert "uv pip install" in log
        assert "--no-deps" in log
        assert "systemctl restart pumpparse" in log

    def test_проверяет_живость(self, hook):
        _, log = hook(f"{PREV} {NEW} {MAIN}\n")
        assert "/healthz" in log

    def test_сообщает_об_успехе(self, hook):
        r, _ = hook(f"{PREV} {NEW} {MAIN}\n")
        assert "сервис отвечает" in r.stdout


class TestЧужиеВетки:
    def test_ветка_кроме_main_пропускается(self, hook):
        r, log = hook(f"{PREV} {NEW} refs/heads/feature\n")
        assert r.returncode == 0
        assert checkouts(log) == []
        assert "пропускаю" in r.stdout

    def test_тег_не_выкладывается(self, hook):
        _, log = hook(f"{PREV} {NEW} refs/tags/v1\n")
        assert checkouts(log) == []

    def test_из_нескольких_ссылок_берётся_только_main(self, hook):
        _, log = hook(f"{PREV} {NEW} refs/heads/dev\n{PREV} {NEW} {MAIN}\n")
        assert checkouts(log) == [NEW]


class TestОткат:
    def test_упавший_сервис_откатывается(self, hook):
        r, log = hook(f"{PREV} {NEW} {MAIN}\n", HEALTH_OK=0)
        assert checkouts(log) == [NEW, PREV], "отката на прошлый коммит не было"
        assert r.returncode == 1, "неудачная выкладка обязана возвращать ненулевой код"

    def test_упавший_рестарт_тоже_ведёт_к_откату(self, hook):
        """set -e раньше обрывал хук на systemctl, минуя откат."""
        r, log = hook(f"{PREV} {NEW} {MAIN}\n", RESTART_RC=1, HEALTH_OK=0)
        assert checkouts(log) == [NEW, PREV]
        assert r.returncode == 1

    def test_упавший_pip_тоже_ведёт_к_откату(self, hook):
        r, log = hook(f"{PREV} {NEW} {MAIN}\n", PIP_RC=1, HEALTH_OK=0)
        assert checkouts(log) == [NEW, PREV]
        assert r.returncode == 1

    def test_первая_выкладка_откатывать_некуда(self, hook):
        r, log = hook(f"{PREV} {NEW} {MAIN}\n", HEALTH_OK=0, PREV_SHA="")
        assert checkouts(log) == [NEW]
        assert r.returncode == 1
        assert "вручную" in r.stderr

    def test_повторная_выкладка_того_же_коммита(self, hook):
        """prev == new: откат вернул бы ровно то, что уже не работает."""
        r, log = hook(f"{PREV} {NEW} {MAIN}\n", HEALTH_OK=0, PREV_SHA=NEW)
        assert checkouts(log) == [NEW]
        assert r.returncode == 1

    def test_prev_берётся_из_рабочей_копии_а_не_из_ссылки(self, hook):
        """После force-push старый SHA из stdin врёт, HEAD рабочей копии — нет."""
        неверный = "c" * 40
        _, log = hook(f"{неверный} {NEW} {MAIN}\n", HEALTH_OK=0, PREV_SHA=PREV)
        assert checkouts(log) == [NEW, PREV]
