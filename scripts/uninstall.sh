#!/bin/sh
# Полное удаление LocalRAG с машины пользователя — БТ «Библиотека индексации» §7:
# останов демона, удаление индексов, конфигов, временных выдач, порта и pid,
# уведомление пользователя.
#
# Файлы поставки не трогает — их снимает пакетный менеджер. Здесь — состояние в
# домашнем каталоге, до которого пакет (он ставится от root) не доберётся.
#
# Запуск (свой бит +x теряется при распаковке архива, поэтому через sh):
#
#     sh uninstall.sh          спросит подтверждение
#     sh uninstall.sh --yes    без вопросов
#
# Переменные (те же, что понимает демон и setup.sh):
#   RAG_LSM_ROOT=<путь>        каталог состояния и port/pid, если не ~/.files-search
#   RAG_LD_DB_PATH=<путь>      индекс LanceDB, если не ~/.files-search/lance
#   RAG_DATA_DIRECTORY=<путь>  каталог конфига, если не ~/.ragsearch
#   MCP_CONFIG=<путь>          один конкретный конфиг агента вместо всех известных
set -eu

DIR=$(cd "$(dirname "$0")" && pwd)
STATE="${RAG_LSM_ROOT:-$HOME/.files-search}"
# RAG_LSM_ROOT переносит не всё: индекс LanceDB и лог демон держит в
# ~/.files-search (src/config.py). Пусто — каталог совпал со STATE.
INDEX_HOME="$HOME/.files-search"
[ "$INDEX_HOME" != "$STATE" ] || INDEX_HOME=""
USER_CONF="${RAG_DATA_DIRECTORY:-$HOME/.ragsearch}"
ASSUME_YES=0

for arg in "$@"; do
    case "$arg" in
        --yes|-y) ASSUME_YES=1 ;;
        *) echo "неизвестный аргумент: $arg" >&2; exit 2 ;;
    esac
done

echo "Будет удалено:"
echo "  $STATE   (индексы, состояние LSM, очереди, port, pid)"
[ -z "$INDEX_HOME" ] || echo "  $INDEX_HOME   (индекс LanceDB, лог)"
[ -z "${RAG_LD_DB_PATH:-}" ] || echo "  $RAG_LD_DB_PATH   (индекс LanceDB)"
echo "  $USER_CONF        (пользовательский конфиг: список папок)"
echo "  временные выдачи ${TMPDIR:-/tmp}/rag_file_*"
echo "  запись localrag из конфига MCP-агента"
echo

if [ "$ASSUME_YES" -ne 1 ]; then
    printf "Продолжить? [y/N] "
    read -r answer
    case "$answer" in
        [yY]|[yY][eE][sS]) ;;
        *) echo "Отменено, ничего не тронуто."; exit 0 ;;
    esac
fi

# Без XDG_RUNTIME_DIR (не из графической сессии) systemctl --user не найдёт
# менеджер пользователя — как в mcp_server.py.
user_systemctl() {
    XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" \
        systemctl --user --no-pager "$@"
}

# Пакетная установка: демоном и регистрацией MCP управляет systemd --user.
# not-found — юнитов нет (поставка архивом), пусто — менеджер недоступен.
SYSTEMD_UNITS=""
if command -v systemctl >/dev/null 2>&1; then
    case "$(user_systemctl show -p LoadState --value lib-local-rag.service 2>/dev/null)" in
        loaded|masked) SYSTEMD_UNITS=yes ;;
    esac
fi

# Порядок — как в debian/prerm: .path (иначе снятие записи MCP ниже снова
# запустит регистрацию), сервис регистрации, маска демона (MCP-мост сам зовёт
# systemctl --user start), сам демон. --runtime: маска живёт до конца сессии и
# следов в домашнем каталоге не оставляет.
stop_units() {
    rc=0
    user_systemctl stop lib-local-rag-mcp-register-wsc.path \
        lib-local-rag-mcp-register-gigacode.path >/dev/null 2>&1 || rc=1
    user_systemctl stop lib-local-rag-mcp-register.service >/dev/null 2>&1 || rc=1
    user_systemctl --quiet mask --runtime lib-local-rag.service >/dev/null 2>&1 || rc=1
    user_systemctl stop lib-local-rag.service >/dev/null 2>&1 || rc=1
    if [ "$rc" -eq 0 ]; then
        echo "  службы systemd --user остановлены, демон замаскирован до конца сессии"
    else
        echo "  systemctl --user справился не со всеми юнитами — демон или запись MCP могут вернуться" >&2
    fi
}

# По pid-файлу, а не по имени процесса: похожий на main.py процесс может быть чужим.
stop_daemon() {
    pidfile="$STATE/pid"
    if [ ! -f "$pidfile" ]; then
        echo "  pid-файл не найден — считаем, что демон не запущен"
        return 0
    fi
    pid=$(cat "$pidfile" 2>/dev/null || echo "")
    case "$pid" in
        ''|*[!0-9]*) echo "  в pid-файле не число, пропускаем остановку"; return 0 ;;
    esac
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "  процесс $pid уже не жив"
        return 0
    fi
    kill -TERM "$pid" 2>/dev/null || true
    i=0
    while [ "$i" -lt 15 ]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "  демон $pid остановлен"
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    # graceful shutdown у uvicorn — 5 секунд; если за 15 не уложился, ждать
    # дальше смысла нет, а индексы всё равно сейчас будут снесены.
    kill -KILL "$pid" 2>/dev/null || true
    echo "  демон $pid не ответил на TERM за 15 c — отправлен KILL"
}

echo "Останавливаю демон:"
[ -z "$SYSTEMD_UNITS" ] || stop_units
stop_daemon

# register_mcp.py лежит в mcp/ в распакованной поставке и рядом — в репозитории.
echo
echo "Снимаю регистрацию MCP-моста:"
register=""
for candidate in "$DIR/mcp/register_mcp.py" "$DIR/register_mcp.py"; do
    if [ -f "$candidate" ]; then
        register="$candidate"
        break
    fi
done
if [ -z "$register" ]; then
    echo "  register_mcp.py не найден — уберите запись localrag из конфига агента вручную" >&2
elif ! command -v python3 >/dev/null 2>&1; then
    echo "  python3 не найден — уберите запись localrag из конфига агента вручную" >&2
elif [ -n "${MCP_CONFIG:-}" ]; then
    # Тот же MCP_CONFIG, что у setup.sh: иначе запись в нестандартном конфиге
    # агента останется — след, который запрещает §7.
    python3 "$register" --config "$MCP_CONFIG" --remove \
        || echo "  не удалось — уберите запись localrag вручную" >&2
else
    python3 "$register" --remove \
        || echo "  не удалось — уберите запись localrag вручную" >&2
fi

echo
echo "Удаляю данные:"
for target in "$STATE" "$INDEX_HOME" "${RAG_LD_DB_PATH:-}" "$USER_CONF"; do
    [ -n "$target" ] || continue
    if [ -e "$target" ]; then
        rm -rf "$target"
        echo "  удалено: $target"
    else
        echo "  уже отсутствует: $target"
    fi
done

# Временные выдачи прежних версий (POST /get_file_path) — только свои: каталог общий.
find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'rag_file_*' -user "$(id -u)" \
    -exec rm -rf {} + 2>/dev/null || true
echo "  удалены временные выдачи ${TMPDIR:-/tmp}/rag_file_*"

# Явное уведомление пользователя — требование БТ §7.
echo
echo "================================================================"
echo " Индексы LocalRAG очищены. Данные с устройства удалены полностью."
echo " Файлы самой поставки не тронуты — их снимает пакетный менеджер."
if [ -n "$SYSTEMD_UNITS" ]; then
    # Юниты включены глобально: при следующем входе пакет поднимет их снова.
    echo " Пакет lib-local-rag остаётся установленным: при следующем входе"
    echo " демон и регистрация MCP поднимутся снова, и индексация начнётся"
    echo " заново. Удалить LocalRAG полностью может администратор:"
    echo "     sudo apt purge lib-local-rag"
fi
echo "================================================================"
