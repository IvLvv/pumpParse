"""Файлы выкладки. Проверяем то, что ломается молча и всплывает уже на проде:
синтаксис скриптов, переводы строк, секреты в git, ключевые директивы юнита.
"""
import re
import shutil
import subprocess

import pytest

from pumpparse import auth as authmod

from .conftest import ROOT

DEPLOY = ROOT / "deploy"
HOOK = DEPLOY / "post-receive"
SETUP = DEPLOY / "setup-server.sh"
UNIT = DEPLOY / "pumpparse.service"

BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash недоступен")


def read(path):
    return path.read_text(encoding="utf-8")


class TestShellScripts:
    @pytest.mark.parametrize("path", [HOOK, SETUP], ids=lambda p: p.name)
    @needs_bash
    def test_синтаксис(self, path):
        r = subprocess.run([BASH, "-n", str(path)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    @pytest.mark.parametrize("path", [HOOK, SETUP], ids=lambda p: p.name)
    def test_перевод_строки_unix(self, path):
        """CRLF ломает shebang: на сервере получается «bad interpreter»."""
        assert b"\r\n" not in path.read_bytes()

    @pytest.mark.parametrize("path", [HOOK, SETUP], ids=lambda p: p.name)
    def test_shebang_и_строгий_режим(self, path):
        src = read(path)
        assert src.startswith("#!/usr/bin/env bash")
        assert "set -euo pipefail" in src

    def test_хук_выкладывает_только_main(self):
        src = read(HOOK)
        assert "BRANCH=main" in src
        assert 'refs/heads/$BRANCH' in src

    def test_хук_проверяет_живость_и_откатывается(self):
        src = read(HOOK)
        assert "/healthz" in src
        assert "release \"$prev\"" in src
        # Провалившаяся выкладка обязана заканчиваться ненулевым кодом,
        # иначе git push отчитается об успехе на лежащем сервисе.
        assert "exit 1" in src

    def test_хук_ставит_пакет_без_зависимостей(self):
        assert "--no-deps" in read(HOOK)

    def test_хук_и_setup_согласованы_по_uv(self):
        """Разъедься пути к uv — выкладка упадёт уже на сервере."""
        assert "UV_BIN=/usr/local/bin/uv" in read(HOOK)
        assert "UV_BIN=/usr/local/bin/uv" in read(SETUP)

    @pytest.mark.parametrize("path", [HOOK, SETUP], ids=lambda p: p.name)
    def test_пути_в_кавычках(self, path):
        """Незакавыченный $WORK развалится на пробеле в пути."""
        src = read(path)
        for var in ("$WORK", "$VENV", "$ROOT"):
            bare = re.findall(rf"(?<![\"'\w]){re.escape(var)}(?![\w\"])", src)
            assert not bare, f"{var} без кавычек: {bare}"

    def test_setup_ставит_свой_python(self):
        """Системный python 3.10 старее требуемого, интерпретатор ставит uv."""
        src = read(SETUP)
        assert "PYVER=" in src
        assert "uv" in src and "venv --python" in src
        assert "setuptools" in src, "без setuptools сборка с --no-build-isolation не пройдёт"

    def test_setup_закрывает_файл_окружения(self):
        src = read(SETUP)
        assert "chmod 600" in src
        assert "visudo -cf" in src, "sudoers без проверки может закрыть вход на сервер"

    def test_setup_не_перезаписывает_существующий_env(self):
        """Повторный запуск не должен затирать пароль и ключ сессий."""
        src = read(SETUP)
        assert 'if [ ! -f "$ENVFILE" ]' in src

    def test_setup_снимает_флаг_bare(self):
        """Без этого checkout с --work-tree откажется работать."""
        assert "core.bare false" in read(SETUP)

    def test_setup_разрешает_пуш_в_текущую_ветку(self):
        """Обратная сторона снятого core.bare: иначе push в main отбивается."""
        assert "receive.denyCurrentBranch ignore" in read(SETUP)


class TestSystemdUnit:
    def test_перевод_строки_unix(self):
        assert b"\r\n" not in UNIT.read_bytes()

    @pytest.mark.parametrize("directive", [
        "EnvironmentFile=", "ExecStart=", "Restart=always",
        "NoNewPrivileges=yes", "ProtectSystem=strict", "ReadWritePaths=",
        "WantedBy=multi-user.target", "PYTHONUNBUFFERED=1",
    ])
    def test_ключевые_директивы_на_месте(self, directive):
        assert directive in read(UNIT)

    def test_слушает_только_петлю(self):
        """Наружу отдаёт nginx; открытый порт обошёл бы TLS."""
        assert "--host 127.0.0.1" in read(UNIT)

    def test_секреты_не_в_юните(self):
        src = read(UNIT)
        assert authmod.HASH_ENV + "=" not in src
        assert authmod.SECRET_ENV + "=" not in src

    def test_прокси_флаги_не_зашиты_в_юнит(self):
        """Их значение зависит от машины: есть ли перед сервисом nginx с TLS."""
        src = read(UNIT)
        assert "Environment=PUMPPARSE_TRUST_PROXY" not in src
        assert "Environment=PUMPPARSE_SECURE_COOKIE" not in src
        assert "PUMPPARSE_TRUST_PROXY" in src, "но упомянуть в комментарии стоит"

    def test_прокси_флаги_выключены_в_заготовке_окружения(self):
        """Включить их без nginx опаснее, чем забыть включить вместе с ним."""
        src = read(SETUP)
        assert "PUMPPARSE_TRUST_PROXY=0" in src
        assert "PUMPPARSE_SECURE_COOKIE=0" in src

    def test_nginx_затирает_клиентский_xff(self):
        """Только это делает доверие заголовку осмысленным."""
        nginx = read(DEPLOY / "nginx.conf.example")
        assert "proxy_set_header X-Forwarded-For $remote_addr" in nginx

    def test_у_каждой_секции_есть_заголовок(self):
        src = read(UNIT)
        for section in ("[Unit]", "[Service]", "[Install]"):
            assert section in src


class TestRepoHygiene:
    def test_секреты_игнорируются(self):
        ignore = read(ROOT / ".gitignore")
        assert "*.env" in ignore
        assert "pump.db" in ignore

    def test_скрипты_помечены_как_lf(self):
        attrs = read(ROOT / ".gitattributes")
        assert "*.sh" in attrs and "eol=lf" in attrs
        assert "deploy/post-receive" in attrs

    def test_рантайм_без_зависимостей(self):
        """`pip install --no-deps .` обязан ставить рабочий сервис."""
        import tomllib
        with open(ROOT / "pyproject.toml", "rb") as f:
            project = tomllib.load(f)["project"]
        assert project["dependencies"] == []
        assert project["requires-python"] == ">=3.11"

    def test_в_requirements_нет_пакетов(self):
        lines = [ln.strip() for ln in read(ROOT / "requirements.txt").splitlines()]
        assert not [ln for ln in lines if ln and not ln.startswith("#")]

    def test_инструменты_разработки_закреплены(self):
        for line in read(ROOT / "requirements-dev.txt").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                assert "==" in line, f"{line} без точной версии"
