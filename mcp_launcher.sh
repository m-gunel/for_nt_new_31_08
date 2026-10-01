#!/usr/bin/env bash
# Launcher MCP-моста LocalRAG (stdio).
#
# Мост написан на чистом stdlib, поэтому venv ему не обязателен: если рядом
# есть .venv репозитория — берём его (там доступны парсеры офисных форматов
# для local_read_file), иначе работаем на системном python3. Второй случай —
# это поставка: мост уезжает в дистрибутив отдельными файлами, и venv рядом с
# ним не распаковывается.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER="$SCRIPT_DIR/mcp_server.py"

if [[ ! -f "$SERVER" ]]; then
  echo "Ошибка: не найден $SERVER" >&2
  exit 1
fi

if [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
  PYTHON="$SCRIPT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  echo "Ошибка: не найден интерпретатор python3" >&2
  exit 1
fi

exec "$PYTHON" "$SERVER" "$@"
