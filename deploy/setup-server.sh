#!/usr/bin/env bash
# Разовая подготовка сервера под pumpParse. Запускать от root на чистой машине:
#     sudo bash setup-server.sh
# Идемпотентен: повторный запуск ничего не ломает и не трогает БД с паролем.
set -euo pipefail

ROOT=/srv/pumpparse
USER_NAME=pumpparse
REPO="$ROOT/repo.git"
ENVFILE="$ROOT/app.env"
SRC=$(cd "$(dirname "$0")" && pwd)

# Интерпретатор ставит uv, а не пакетный менеджер системы: пакет требует
# 3.11+, а в Ubuntu 22.04 системный python 3.10 и в репозитории лежит только
# 3.11.0~rc1 — release candidate на прод не годится. uv кладёт собственную
# сборку CPython в домашний каталог сервиса и системный python не трогает.
PYVER=3.12
UV_BIN=/usr/local/bin/uv

[ "$(id -u)" -eq 0 ] || { echo "нужен root"; exit 1; }

echo "== пользователь и каталоги =="
id -u "$USER_NAME" >/dev/null 2>&1 || useradd --system --home "$ROOT" --shell /bin/bash "$USER_NAME"
mkdir -p "$ROOT/app" "$ROOT/data"
chown -R "$USER_NAME:$USER_NAME" "$ROOT"

echo "== uv =="
if [ ! -x "$UV_BIN" ]; then
    curl -LsSf https://astral.sh/uv/install.sh \
      | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh
fi
"$UV_BIN" --version

echo "== python $PYVER и окружение =="
# HOME задаём явно: под sudo он остался бы root'овым, и кеш uv с самим
# интерпретатором лёг бы в /root, недоступный сервису.
as_user() { sudo -u "$USER_NAME" env HOME="$ROOT" "$@"; }
as_user "$UV_BIN" python install "$PYVER"
[ -x "$ROOT/venv/bin/python" ] || as_user "$UV_BIN" venv --python "$PYVER" "$ROOT/venv"
# uv venv не кладёт в окружение ни pip, ни setuptools. setuptools нужен,
# потому что выкладка собирает пакет с --no-build-isolation.
as_user "$UV_BIN" pip install --python "$ROOT/venv/bin/python" --quiet setuptools wheel
"$ROOT/venv/bin/python" -V

echo "== bare-репозиторий для пуша =="
if [ ! -d "$REPO" ]; then
    sudo -u "$USER_NAME" git init --bare --initial-branch=main "$REPO"
fi
# checkout из bare-репозитория с --work-tree отказывается работать, пока
# репозиторий помечен bare; рабочего дерева у него по-прежнему нет.
sudo -u "$USER_NAME" git --git-dir="$REPO" config core.bare false
sudo -u "$USER_NAME" git --git-dir="$REPO" config core.worktree "$ROOT/app"
# Обратная сторона снятого core.bare: git считает main «выкаченной веткой»
# и отбивает пуш в неё. Рабочее дерево обновляет хук, поэтому запрет снимаем.
sudo -u "$USER_NAME" git --git-dir="$REPO" config receive.denyCurrentBranch ignore
install -o "$USER_NAME" -g "$USER_NAME" -m 750 "$SRC/post-receive" "$REPO/hooks/post-receive"

echo "== право перезапускать сервис без пароля =="
cat > /etc/sudoers.d/pumpparse <<EOF
$USER_NAME ALL=(root) NOPASSWD: /bin/systemctl restart pumpparse, /bin/systemctl status pumpparse
EOF
chmod 440 /etc/sudoers.d/pumpparse
visudo -cf /etc/sudoers.d/pumpparse >/dev/null

echo "== файл окружения =="
if [ ! -f "$ENVFILE" ]; then
    cat > "$ENVFILE" <<EOF
# Заполните: на рабочей машине \`pumpparse passwd\` напечатает готовые строки.
PUMPPARSE_USER=admin
PUMPPARSE_PASSWORD_HASH=
PUMPPARSE_SECRET=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
EOF
    echo "   создан $ENVFILE — впишите PUMPPARSE_PASSWORD_HASH, иначе вход отключён"
else
    echo "   $ENVFILE уже есть, не трогаю"
fi
chown "$USER_NAME:$USER_NAME" "$ENVFILE"
chmod 600 "$ENVFILE"

echo "== systemd =="
install -m 644 "$SRC/pumpparse.service" /etc/systemd/system/pumpparse.service
systemctl daemon-reload
systemctl enable pumpparse >/dev/null

echo
echo "Готово. Дальше:"
echo "  1) на рабочей машине:  pumpparse passwd"
echo "     вписать вывод в $ENVFILE (кроме PUMPPARSE_SECRET, он уже сгенерирован)"
echo "  2) на рабочей машине:  git remote add production $USER_NAME@<хост>:$REPO"
echo "                         git push production main"
echo "  3) поднять nginx с TLS по образцу deploy/nginx.conf.example"
