#!/usr/bin/env bash
# Сборка архива для передачи тестировщику — после проверок: без uv.lock состав
# пакетов отрезолвится у принимающей стороны заново, и прогон будет несравним;
# секреты и локальное состояние в архив уходить не должны.
#
# Запускать из корня репозитория, на машине с доступом к зеркалу:
#   ./scripts/make_handover.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
NAME="lib-local-rag-mcp-$(date +%Y%m%d)"
OUT="../${NAME}.zip"
FAIL=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  OK   %s\n' "$*"; }
bad()  { printf '  СТОП %s\n' "$*"; FAIL=1; }
# Файлы с учётными данными в URL (схема://логин:пароль@); печатаются только имена.
# Плейсхолдеры не в счёт: $(OSC_TOKEN), ${...} и <...> в шаблон пароля не входят,
# литерал OSC_TOKEN отсекается отдельно.
cred_files() {
  { grep -rHIoE 'https?://[^/@:[:space:]]+:[A-Za-z0-9._~%!*+=-]{8,}@' "$@" 2>/dev/null || true; } \
    | { grep -v ':OSC_TOKEN@$' || true; } | cut -d: -f1 | sort -u
}

say "=== 1. Состав пакетов зафиксирован ==="
if [[ -f uv.lock ]]; then
  ok "uv.lock на месте"
else
  bad "нет uv.lock — выполните: uv lock"
fi
if [[ -f requirements.lock.txt ]]; then
  ok "requirements.lock.txt на месте"
else
  say "  ПРЕДУПР requirements.lock.txt нет — нужен, только если кто-то ставится через pip:"
  say "          uv export --no-hashes --format requirements-txt --no-dev -o requirements.lock.txt"
fi

say "=== 2. В локе нет живого токена ==="
# Лок, пересозданный с токеном в адресе индекса, может унести его в свои URL.
# Живой токен ищется первым: плейсхолдер OSC_TOKEN рядом его не прячет.
if [[ -f uv.lock ]]; then
  if [[ -n "$(cred_files uv.lock)" ]]; then
    bad "в uv.lock похоже лежит РЕАЛЬНЫЙ токен — пересоздайте лок на рабочей машине"
  elif grep -q "OSC_TOKEN" uv.lock; then
    ok "в индексных URL плейсхолдер OSC_TOKEN"
  else
    ok "подстановок токена не видно"
  fi
fi

say "=== 3. Платформа целевого контура объявлена ==="
if grep -q "required-environments" pyproject.toml; then
  ok "required-environments задан (x86_64 Linux)"
else
  bad "нет required-environments — uv может отрезолвить lancedb без колеса под АРМ"
fi

say "=== 4. Тестовые зависимости не уедут в сборку демона ==="
if grep -qE '^\s*"(pytest|pytest-asyncio|httpx|asgi-lifespan|sentence-transformers)' <(sed -n '/^dependencies = \[/,/^]/p' pyproject.toml); then
  bad "в [project.dependencies] остались тестовые или тяжёлые пакеты"
else
  ok "runtime-зависимости чистые"
fi

say "=== 5. Быстрые тесты ==="
if command -v uv >/dev/null 2>&1; then
  if uv run pytest -m "not integration" -q >/tmp/handover_tests.log 2>&1; then
    ok "$(tail -1 /tmp/handover_tests.log)"
  else
    bad "тесты красные, см. /tmp/handover_tests.log"
  fi
else
  say "  ПРОПУСК uv не найден — прогоните тесты вручную"
fi

say "=== 6. Мост отвечает ==="
if echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | ./mcp_launcher.sh 2>/dev/null | grep -q local_search; then
  ok "tools/list отдаёт инструменты"
else
  bad "мост не ответил на tools/list"
fi

if (( FAIL )); then
  say ""
  say "Архив НЕ собран: сначала закройте пункты со «СТОП»."
  exit 1
fi

say "=== 7. Сборка архива ==="
rm -rf "/tmp/${NAME}" && mkdir -p "/tmp/${NAME}"
# Исключаем секреты, локальное состояние индекса и всё, что пересоздаётся само.
# Dockerfile и pip.conf от git прячет только .git/info/exclude, а rsync его не читает.
# Скрытые каталоги — состояние git, venv, IDE и инструментов: в репозитории их нет.
# CHANGES.diff и ноутбуки с сохранёнными выводами тестировщику не нужны.
rsync -a \
  --exclude '.*/' --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude 'dist/' --exclude 'build/' \
  --exclude 'data/' --exclude 'tests/tmp/' \
  --exclude '.env' --exclude '.env.*' --exclude '.secret' --exclude '.secret.temp' \
  --exclude '.DS_Store' \
  --exclude 'Dockerfile' --exclude 'pip.conf' \
  --exclude 'CHANGES.diff' --exclude 'notebooks/' \
  ./ "/tmp/${NAME}/lib-local-rag/"

# Последний рубеж: учётные данные в URL в любом файле архива, а не только в uv.lock.
leaked=$(cred_files "/tmp/${NAME}/lib-local-rag")
if [[ -n "$leaked" ]]; then
  bad "в архив попали бы учётные данные в URL (значения не печатаются):"
  printf '%s\n' "$leaked" | sed "s#^/tmp/${NAME}/lib-local-rag/#         #"
  rm -rf "/tmp/${NAME}"
  say "Архив НЕ собран."
  exit 1
fi

# Архив распаковывается в lib-local-rag/, без лишнего уровня с датой.
( cd "/tmp/${NAME}" && rm -f "../${NAME}.zip" && zip -qr "../${NAME}.zip" lib-local-rag )
mv "/tmp/${NAME}.zip" "$OUT"
rm -rf "/tmp/${NAME}"

say ""
say "Готово: $(cd .. && pwd)/${NAME}.zip"
say "Внутри: uv.lock, код, тесты, MCP-мост, модель. Без .env, .secret, Dockerfile,"
say "pip.conf, скрытых каталогов и data/; учётных данных в URL нет."
say ""
say "Тестировщику передайте вместе с docs/HANDOVER.md — там порядок запуска."
