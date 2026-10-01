#!/bin/sh
# Полное удаление LocalRAG с машины пользователя — БТ «Библиотека индексации» §7.
#
# БТ требует четырёх вещей, и все четыре здесь: демон немедленно остановлен;
# индексные файлы и директория удалены полностью; следов не остаётся (конфиги,
# временные каталоги выдач, файлы порта и pid); пользователь получает
# уведомление, что индексы очищены.
#
# Скрипт НЕ удаляет сами файлы поставки — их снимает пакетный менеджер или
# пользователь. Он убирает пользовательское состояние, до которого пакету не
# добраться: оно лежит в домашних каталогах, а пакет ставится от root.
#
# Запуск (свой бит +x теряется при распаковке архива, поэтому через sh):
#
#     sh uninstall.sh          спросит подтверждение
#     sh uninstall.sh --yes    без вопросов, для postrm
#
# Переменные (те же, что понимает демон и setup.sh):
#   RAG_LSM_ROOT=<путь>        каталог индексов, если не ~/.files-search
#   RAG_DATA_DIRECTORY=<путь>  каталог конфига, если не ~/.ragsearch
#   MCP_CONFIG=<путь>          один конкретный конфиг агента вместо всех известных
set -eu

DIR=$(cd "$(dirname "$0")" && pwd)
# Те же переменные, которыми переопределяет каталоги сам демон (env_prefix
# RAG_ в src/config.py). Иначе при нестандартной установке скрипт снёс бы
# каталоги по умолчанию, а настоящие оставил.
STATE="${RAG_LSM_ROOT:-$HOME/.files-search}"
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
echo "  $USER_CONF        (пользовательский конфиг: список папок, настройки почты)"
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

# --- 1. Остановить демон -----------------------------------------------------
# По pid-файлу, а не поиском по имени процесса: на чужой машине «что-то похожее
# на main.py» может оказаться не нашим.
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
stop_daemon

# --- 2. Снять регистрацию у агента ------------------------------------------
# Ищем скрипт в обеих раскладках: mcp/ в распакованной поставке, рядом — в
# репозитории.
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
    # Тот же MCP_CONFIG, что и у setup.sh: если конфиг агента лежит не по
    # умолчанию, без него снятие ушло бы в чужой файл, а настоящая запись
    # осталась бы — то есть след, который §7 запрещает.
    python3 "$register" --config "$MCP_CONFIG" --remove \
        || echo "  не удалось — уберите запись localrag вручную" >&2
else
    python3 "$register" --remove \
        || echo "  не удалось — уберите запись localrag вручную" >&2
fi

# --- 3. Снести состояние -----------------------------------------------------
echo
echo "Удаляю данные:"
for target in "$STATE" "$USER_CONF"; do
    if [ -e "$target" ]; then
        rm -rf "$target"
        echo "  удалено: $target"
    else
        echo "  уже отсутствует: $target"
    fi
done

# Временные выдачи POST /get_file_path. Только свои: каталог общий, и чужие
# трогать нельзя.
find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'rag_file_*' -user "$(id -u)" \
    -exec rm -rf {} + 2>/dev/null || true
echo "  удалены временные выдачи ${TMPDIR:-/tmp}/rag_file_*"

# --- 4. Уведомить ------------------------------------------------------------
# Отдельным явным сообщением: БТ §7 требует, чтобы пользователь узнал об этом.
echo
echo "================================================================"
echo " Индексы LocalRAG очищены. Данные с устройства удалены полностью."
echo " Файлы самой поставки не тронуты — их снимает пакетный менеджер."
echo "================================================================"
