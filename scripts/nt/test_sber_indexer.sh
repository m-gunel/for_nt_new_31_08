#!/usr/bin/env bash
set -e

# ══════════════════════════════════════════════════════════════════════════════
#  Тестирование индексатора (ragsearch) — по образцу test_sber_assistant.sh
#
#  Протокол (3 фазы по N минут, пассивный монитор идёт в каждой фазе):
#    ФАЗА 1  ПРОСТОЙ    — индексатор ВЫКЛЮЧЕН, система в покое
#    ФАЗА 2  СТРЕСС     — индексатор запущен, python-стенд давит максимум
#                         (индексация корпуса + дозагрузка + поиск 2 зап/с)
#    ФАЗА 3  ПОЛЬЗОВАТЕЛЬ — реалистичный путь: ~30 поисков/час,
#                         1 индексация, 1 доиндексация
#
#  Монитор (ваш инструмент) запускается в foreground как в тесте СберКота,
#  нагрузка — python-стендом в фоне, его лог пишется в файл фазы.
# ══════════════════════════════════════════════════════════════════════════════

# ─── Проверка прав root ────────────────────────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    echo "Ошибка: скрипт должен быть запущен от root (sudo)." >&2
    exit 1
fi

# Все файлы, создаваемые скриптом и его детьми (python, main), — доступны ВСЕМ.
# umask 000 -> новые файлы 666, каталоги 777. Наследуется дочерними процессами.
umask 000

# ─── Цвета ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# ─── Константы стенда ─────────────────────────────────────────────────────────
# Пути задаются переменными окружения — в репозитории они не могут быть
# захардкожены под конкретную машину. Пример запуска:
#   INDEXER_DIR=~/lib-local-rag WATCHED_DIR=~/index_test_files \
#   MONITOR_DIR=~/datahound sudo -E ./test_sber_indexer.sh
readonly INDEXER_DIR="${INDEXER_DIR:-$PWD}"
readonly INDEXER_BIN="python main.py"
readonly INDEXER_PATTERN="main.py"           # для pgrep -f (python main.py)
readonly INDEXER_URL="${INDEXER_URL:-http://127.0.0.1:8000}"
readonly LOADTEST="${LOADTEST:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/sber_indexer_loadtest.py}"
readonly WATCHED_DIR="${WATCHED_DIR:-$HOME/index_test_files}"
readonly MONITOR_DIR="${MONITOR_DIR:-$HOME/datahound}"  # где лежит cli/main.py
readonly MONITOR_CMD="python cli/main.py run system/system-passive-monitor"
readonly RESULTS_ROOT="./indexer_test_$(date +%Y%m%d_%H%M%S)"
readonly OS_VERSION_FILE="/etc/os-version"
readonly NUM_FILES=800            # общий дефолт
# Корпус для пользовательского пути. Замер 15.07: индексация идёт ~0.46 файл/с.
# 800 файлов -> 680 копируется в bulk -> ~25 мин индексации, остаток часа (~35 мин)
# на поисковую нагрузку. Закрывает требование продакта "500-1000 файлов".
# Для 15-минутных проверок ставь 300 (255 файлов -> ~9 мин).
readonly USER_NUM_FILES=800
readonly STRESS_NUM_FILES=800    # корпус для стресса (тяжёлая индексация)
readonly USER_SEARCHES_PER_HOUR=30
readonly USER_REINDEX_FILES=10
readonly AUTO_START_INDEXER=true    # true = скрипт сам запускает ./main (без sudo, от пользователя)
# ОБХОД дефекта индексатора "email root not found" (src/config.py:432):
# парсер дёргает email-ветку для каждого файла даже при email.enabled=false.
# Укажи существующий каталог как email root (напр. "$HOME/.thunderbird"),
# пусто = слать как раньше.
readonly EMAIL_FOLDER=""
# ОБХОД race-бага индексатора (подтверждён 2026-07-15): поиск ВО ВРЕМЯ индексации
# вызывает release_memory() на занятой OpenVINO-модели -> RuntimeError ->
# эмбеддер умирает НАВСЕГДА (все /search = 500, индексация встаёт).
# ОБХОД БОЛЬШЕ НЕ НУЖЕН. В сборке 2026-08-25 гонка исправлена: в OpenVINOEmbedder
# добавлен threading.Lock вокруг инференса, release_memory() не вызывается
# (пересоздаются только собственные infer-request'ы).
# false = честный пользовательский сценарий: поиск параллельно с индексацией.
readonly SAFE_MODE=false

# ─── Утилиты вывода ───────────────────────────────────────────────────────────
ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
err()  { echo -e "  ${RED}✗${NC} $*" >&2; }
info() { echo -e "  ${CYAN}→${NC} $*"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $*"; }
hdr()  { echo -e "\n${CYAN}${BOLD}$*${NC}"; }
sep()  { echo -e "${DIM}────────────────────────────────────────────────────${NC}"; }

info_row() {
    local label="$1"; local value="$2"; local color="${3:-$NC}"
    echo -e "  ${DIM}│${NC}  ${BOLD}${label}${NC}  ${color}${value}${NC}"
}

read_tty() {
    local prompt="$1"; local __result_var="$2"; local input=""
    if [ -t 0 ]; then
        read -rp "$(echo -e "$prompt")" input
    elif [ -c /dev/tty ]; then
        read -rp "$(echo -e "$prompt")" input < /dev/tty
    else
        input=""
    fi
    printf -v "$__result_var" '%s' "$input"
}

banner() {
    local color="$1"; shift
    echo ""
    echo -e "${color}${BOLD}╔══════════════════════════════════════════════════════════╗${NC}"
    printf "${color}${BOLD}║  %-56s║${NC}\n" "$*"
    echo -e "${color}${BOLD}╚══════════════════════════════════════════════════════════╝${NC}"
}

# ─── Данные о системе ─────────────────────────────────────────────────────────
get_os_target_version() {
    if [ -f "$OS_VERSION_FILE" ]; then
        grep '^ostargetversion=' "$OS_VERSION_FILE" \
            | sed 's/ostargetversion=//' | tr -d '"' | xargs 2>/dev/null \
            || echo "неизвестно"
    else
        echo "неизвестно"
    fi
}

get_current_profile_raw() {
    tuned-adm active 2>/dev/null \
        | grep "Current active profile:" \
        | sed 's/Current active profile: //' | xargs \
        || echo "неизвестно"
}

get_profile_friendly_name() {
    case "$1" in
        "server-powersave") echo "Сбалансированный" ;;
        "balanced")         echo "Производительный" ;;
        "powersave")        echo "Энергосберегающий" ;;
        *)                  echo "Неизвестный" ;;
    esac
}

# ─── Индексатор: процесс и здоровье ──────────────────────────────────────────
check_indexer_process() {
    pgrep -f "$INDEXER_PATTERN" >/dev/null 2>&1
}

# PID именно рабочего процесса (не sudo-обёртки): максимальный RSS
get_indexer_worker_pid() {
    pgrep -f "$INDEXER_PATTERN" 2>/dev/null \
        | xargs -r ps -o pid=,rss= -p 2>/dev/null \
        | sort -k2 -n | tail -1 | awk '{print $1}' || true
}

get_indexer_pids() {
    pgrep -f "$INDEXER_PATTERN" 2>/dev/null | xargs || true
}

check_indexer_health() {
    curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
        "$INDEXER_URL/api/v1/health" 2>/dev/null | grep -q '^200$'
}

show_indexer_top() {
    if check_indexer_process; then
        echo -e "  ${DIM}Процессы индексатора:${NC}"
        pgrep -f "$INDEXER_PATTERN" | xargs -r ps -o pid,ppid,cmd,%cpu,rss --sort=-rss -p \
            | sed 's/^/    /'
    fi
}

# Чистит тестовую папку от нашего корпуса (doc_*). Между прогонами файлы
# копятся и раздувают индекс -> каждый следующий тест индексирует всё дольше.
clean_watched_dir() {
    local n
    n=$(find "$WATCHED_DIR" -maxdepth 1 -type f -name 'doc_*' 2>/dev/null | wc -l)
    if [ "$n" -gt 0 ]; then
        info "Очистка тестовой папки: удаляю $n старых файлов корпуса (doc_*)"
        find "$WATCHED_DIR" -maxdepth 1 -type f -name 'doc_*' -delete 2>/dev/null || true
    else
        info "Тестовая папка чиста (старого корпуса нет)."
    fi
}

# ПОЛНЫЙ СБРОС перед тестом. Зачем: индексатор не переиндексирует уже известные
# файлы ("nothing new"), а стенд генерит корпус с одинаковыми именами. Без сброса
# индекса bulk-фаза не создаст нагрузки — тест измерит пустоту.
full_reset() {
    local uhome; uhome="$(user_home)"
    local store="$uhome/.files-search"
    hdr "  Полный сброс перед тестом"
    info "1/4 останавливаю индексатор..."
    stop_indexer || true
    info "2/4 удаляю хранилище индекса и очередь: $store"
    rm -rf "$store" 2>/dev/null || true
    rm -rf /root/.files-search 2>/dev/null || true   # хвост от прошлых sudo-запусков
    # КОНТРОЛЬ: если каталог остался — индексатор стартует на «грязной» очереди LSM
    # и упадёт по assert_db_state при следующем POST /config (подтверждено 27.08).
    if [ -d "$store" ]; then
        err "НЕ УДАЛОСЬ удалить $store — файлы держит процесс?"
        echo -e "  ${DIM}$(ls -la "$store" 2>/dev/null | head -4)${NC}"
        warn "Останови индексатор вручную и удали каталог, иначе возможен краш"
        warn "по assert SQLite LSM во время теста."
        local c=""
        read_tty "  ${YELLOW}Продолжить всё равно?${NC} [д/н]: " c
        c=$(echo "$c" | tr '[:upper:]' '[:lower:]')
        case "$c" in д|y|yes|да) ;; *) return 1 ;; esac
    else
        ok "индекс и очередь удалены (каталог отсутствует)"
    fi
    info "3/4 чищу тестовую папку..."
    clean_watched_dir
    info "4/4 поднимаю индексатор заново..."
    ensure_indexer_running || return 1
    ok "сброс завершён — индексатор стартовал с пустым индексом"
    return 0
}

# Лёгкий сброс: чистим папку + перенацеливаем индексатор на неё (без сноса индекса).
reset_before_load() {
    clean_watched_dir
    # Сбрасываем индексатор на пустую/эту же папку, чтобы индекс не тянул старое.
    if check_indexer_health; then
        curl -s -X POST "$INDEXER_URL/api/v1/config" \
            -H 'Content-Type: application/json' \
            -d "{\"email\":{\"enabled\":false,\"index_last_n_days\":null,\"offsets\":null,\"path_to_email_folder\":\"$EMAIL_FOLDER\"},\"folders\":[\"$WATCHED_DIR\"],\"sberchat\":{\"enabled\":false}}" \
            -o /dev/null 2>/dev/null || true
    fi
}

# Домашний каталог РЕАЛЬНОГО пользователя (не root), даже когда скрипт под sudo.
user_home() {
    local u="${SUDO_USER:-$(id -un)}"
    local h; h="$(getent passwd "$u" 2>/dev/null | cut -d: -f6)"
    [ -n "$h" ] && echo "$h" || echo "$HOME"
}

# Делает путь доступным ВСЕМ (владелец — реальный пользователь, права a+rwX).
# Нужно, потому что скрипт под sudo создаёт файлы от root.
fix_perms() {
    local path="$1"
    [ -e "$path" ] || return 0
    local u="${SUDO_USER:-root}"
    local g; g="$(id -gn "$u" 2>/dev/null || echo "$u")"
    chown -R "$u":"$g" "$path" 2>/dev/null || true
    chmod -R a+rwX "$path" 2>/dev/null || true
}

# Запускает команду от имени реального пользователя (не root), если скрипт под sudo.
run_as_user() {
    if [ -n "$SUDO_USER" ]; then
        sudo -u "$SUDO_USER" "$@"
    else
        "$@"
    fi
}

# Автозапуск индексатора БЕЗ sudo, от пользователя, в фоне.
# 'sleep infinity | ./main' держит stdin открытым -> ввод PIN блокируется,
# а uvicorn-сервер поднимается в фоне (как при ручном запуске, когда PIN не вводят).
# Индекс при этом ложится в ~/.files-search пользователя, а не в /root.
start_indexer_auto() {
    local log="$RESULTS_ROOT/indexer_stdout.log"
    mkdir -p "$RESULTS_ROOT"
    local uhome; uhome="$(user_home)"
    info "Автозапуск индексатора от ${SUDO_USER:-текущего} (HOME=$uhome)"
    # HOME задаём ЯВНО: индексатор ищет email root (~/.thunderbird) и кладёт индекс
    # в ~/.files-search. Если HOME останется /root -> ValueError: email root not found.
    # ВАЖНО: новый индексатор ставится в venv. Без активации получим
    # "ModuleNotFoundError: No module named 'uvicorn'". Активируем, если venv есть.
    local runcmd
    if [ -f "$INDEXER_DIR/.venv/bin/activate" ]; then
        runcmd="cd '$INDEXER_DIR' && source .venv/bin/activate && $INDEXER_BIN"
    else
        runcmd="cd '$INDEXER_DIR' && $INDEXER_BIN"
    fi
    if [ -n "$SUDO_USER" ]; then
        setsid sudo -u "$SUDO_USER" -H env "HOME=$uhome" \
            bash -c "$runcmd" > "$log" 2>&1 &
    else
        setsid env "HOME=$uhome" bash -c "$runcmd" > "$log" 2>&1 &
    fi
    local waited=0
    until check_indexer_health; do
        sleep 3; waited=$((waited + 3))
        if [ $((waited % 15)) -eq 0 ]; then
            info "жду health автозапуска (${waited}с)..."
        fi
        if [ "$waited" -ge 120 ]; then
            warn "Автозапуск не поднял health за 120с. Лог: $log"
            tail -n 15 "$log" 2>/dev/null | sed 's/^/      /'
            return 1
        fi
    done
    ok "Индексатор поднялся автоматически (health 200). PID: $(get_indexer_pids)"
    show_indexer_top
    return 0
}

# Гарантирует, что индексатор запущен: health -> авто -> ручной fallback.
ensure_indexer_running() {
    check_indexer_health && return 0
    if [ "$AUTO_START_INDEXER" = "true" ] && ! check_indexer_process; then
        start_indexer_auto && return 0
        warn "Автозапуск не удался — перехожу на ручной режим."
    fi
    wait_indexer_started
}

stop_indexer() {
    info "Завершаю процессы индексатора..."
    pkill -f "$INDEXER_PATTERN" 2>/dev/null || true
    sleep 2
    if check_indexer_process; then
        info "Принудительное завершение (SIGKILL)..."
        pkill -9 -f "$INDEXER_PATTERN" 2>/dev/null || true
        sleep 1
    fi
    if check_indexer_process; then
        warn "Не удалось завершить все процессы — проверьте вручную."
        return 1
    fi
    ok "Индексатор ${RED}остановлен${NC}."
}

# Индексатор при старте спрашивает PIN — автоматизировать нельзя.
# Поэтому просим запустить его в соседнем терминале и ждём health.
wait_indexer_started() {
    echo ""
    warn "Индексатор спрашивает PIN при старте — запустите его ВРУЧНУЮ в соседнем терминале:"
    echo -e "    ${BOLD}cd $INDEXER_DIR && source .venv/bin/activate && python main.py${NC}"
    echo -e "  ${DIM}(БЕЗ sudo — под sudo venv не подхватится: ModuleNotFoundError uvicorn)${NC}"
    echo -e "  Жду ответа $INDEXER_URL/api/v1/health ..."
    local waited=0
    until check_indexer_health; do
        sleep 3
        waited=$((waited + 3))
        if [ $((waited % 30)) -eq 0 ]; then
            info "всё ещё жду health (${waited}с)..."
        fi
        if [ "$waited" -ge 600 ]; then
            err "Индексатор не поднялся за 10 минут."
            return 1
        fi
    done
    ok "Индексатор отвечает (health 200). PID процессов: $(get_indexer_pids)"
    show_indexer_top
}

# Ждём, пока индексатор доиндексирует всё и перейдёт в фон (is_indexing=false)
wait_indexing_settled() {
    info "Жду, пока индексатор закончит индексацию (is_indexing=false)..."
    local waited=0
    while true; do
        local st
        st=$(curl -s --max-time 3 "$INDEXER_URL/api/v1/status" 2>/dev/null || echo "")
        if echo "$st" | grep -q '"is_indexing": *false'; then
            ok "Индексация завершена, индексатор в фоне."
            echo -e "  ${DIM}status: $st${NC}"
            return 0
        fi
        if [ $((waited % 60)) -eq 0 ] && [ "$waited" -gt 0 ]; then
            info "всё ещё индексирует (${waited}с): $(echo "$st" | head -c 160)"
        fi
        sleep 5
        waited=$((waited + 5))
        if [ "$waited" -ge 3600 ]; then
            warn "Индексация не завершилась за час — фаза фона будет нечистой."
            return 1
        fi
    done
}

# ─── ПРЕДПОЛЁТНАЯ ПРОВЕРКА (главный урок: без email root тест бессмыслен) ────
# Индексатор безусловно дёргает email-ветку (src/config.py:432, email_files())
# даже при email.enabled=false. Если в ДОМАШНЕМ каталоге пользователя, под
# которым запущен main, нет email root (~/.thunderbird) -> ValueError:
# "email root not found" -> индексация не стартует вообще.
preflight_email_root() {
    local uhome; uhome="$(user_home)"
    local root="$uhome/.thunderbird"
    hdr "  Предполётная проверка: email root"
    if [ -d "$root" ]; then
        local prof=""
        [ -f "$root/profiles.ini" ] && prof=" (есть profiles.ini — профиль создан)"
        ok "email root найден: $root$prof"
        return 0
    fi
    err "email root НЕ найден: $root"
    echo -e "  ${YELLOW}Без него индексатор упадёт с 'ValueError: email root not found'"
    echo -e "  и НИЧЕГО не проиндексирует — тест уйдёт впустую.${NC}"
    echo -e "  Варианты:"
    echo -e "    1) открыть десктопный Thunderbird под пользователем ${BOLD}${SUDO_USER:-$(id -un)}${NC}"
    echo -e "       (создаст профиль), закрыть — самый надёжный;"
    echo -e "    2) создать каталог: ${BOLD}mkdir -p '$root'${NC} (может не хватить)"
    local c=""
    read_tty "  ${YELLOW}Создать пустой $root сейчас?${NC} [д/н]: " c
    c=$(echo "$c" | tr '[:upper:]' '[:lower:]')
    if [[ "$c" =~ ^(д|y|yes|да)$ ]]; then
        mkdir -p "$root" 2>/dev/null && fix_perms "$root" && ok "создан: $root"
        return 0
    fi
    return 1
}

# Индексатор ОБЯЗАН работать от пользователя, а не от root: под root HOME=/root,
# email root и индекс уедут не туда, и всё сломается.
preflight_indexer_user() {
    local pids; pids=$(get_indexer_pids)
    [ -z "$pids" ] && return 0
    local wp; wp=$(get_indexer_worker_pid)
    [ -z "$wp" ] && return 0
    local owner; owner=$(ps -o user= -p "$wp" 2>/dev/null | xargs)
    if [ "$owner" = "root" ]; then
        warn "Индексатор (PID $wp) работает от ${BOLD}root${NC} -> HOME=/root."
        echo -e "  ${YELLOW}Тогда email root ищется в /root/.thunderbird и индексация упадёт.${NC}"
        echo -e "  Останови его (пункт 1) и дай скрипту поднять от пользователя."
        return 1
    fi
    ok "индексатор работает от пользователя '$owner' — верно"
    return 0
}

# ─── Отображение состояния ────────────────────────────────────────────────────
show_system_info() {
    local os_target raw_profile friendly_profile
    os_target=$(get_os_target_version)
    raw_profile=$(get_current_profile_raw)
    friendly_profile=$(get_profile_friendly_name "$raw_profile")

    echo ""
    echo -e "  ${CYAN}${BOLD}╔══════════════════════════════════════════════╗${NC}"
    echo -e "  ${CYAN}${BOLD}║     Тестирование индексатора (ragsearch)     ║${NC}"
    echo -e "  ${CYAN}${BOLD}╠══════════════════════════════════════════════╣${NC}"
    info_row "Целевой релиз ОС :" "$os_target" "$YELLOW"
    if check_indexer_process; then
        info_row "Индексатор       :" "ЗАПУЩЕН  (PID: $(get_indexer_pids))" "$GREEN"
        if check_indexer_health; then
            info_row "API              :" "health OK ($INDEXER_URL)" "$GREEN"
        else
            info_row "API              :" "процесс есть, health НЕ отвечает" "$YELLOW"
        fi
    else
        info_row "Индексатор       :" "НЕ ЗАПУЩЕН" "$RED"
    fi
    info_row "Режим питания    :" "$friendly_profile ($raw_profile)" "$CYAN"
    info_row "Папка индексации :" "$WATCHED_DIR" "$NC"
    echo -e "  ${CYAN}${BOLD}╚══════════════════════════════════════════════╝${NC}"
    echo ""
}

# ─── Управление индексатором (пункт меню) ─────────────────────────────────────
toggle_indexer() {
    echo ""
    if check_indexer_process; then
        echo -e "  Индексатор сейчас ${GREEN}ЗАПУЩЕН${NC}."
        show_indexer_top
        local choice=""
        read_tty "  ${YELLOW}Выключить индексатор?${NC} [д/н]: " choice
        choice=$(echo "$choice" | tr '[:upper:]' '[:lower:]')
        case "$choice" in
            д|y|yes|да) stop_indexer ;;
            *) echo -e "  Без изменений." ;;
        esac
    else
        echo -e "  Индексатор сейчас ${RED}НЕ ЗАПУЩЕН${NC}."
        local choice=""
        read_tty "  ${YELLOW}Запустить индексатор?${NC} [д/н]: " choice
        choice=$(echo "$choice" | tr '[:upper:]' '[:lower:]')
        case "$choice" in
            д|y|yes|да) ensure_indexer_running ;;
            *) echo -e "  Без изменений." ;;
        esac
    fi
    echo ""
}

# ─── Выбор режима питания (как в исходном скрипте) ────────────────────────────
select_power_profile() {
    local raw_profile friendly_profile
    raw_profile=$(get_current_profile_raw)
    friendly_profile=$(get_profile_friendly_name "$raw_profile")
    echo ""
    hdr "  Выбор режима питания"
    sep
    echo -e "  Текущий: ${BOLD}$friendly_profile${NC} ${DIM}($raw_profile)${NC}"
    echo ""
    echo -e "  ${BOLD}1.${NC} ${BLUE}Энергосберегающий${NC}  ${DIM}(powersave)${NC}"
    echo -e "  ${BOLD}2.${NC} ${CYAN}Сбалансированный${NC}   ${DIM}(server-powersave)${NC}"
    echo -e "  ${BOLD}3.${NC} ${GREEN}Производительный${NC}   ${DIM}(balanced)${NC}"
    echo -e "  ${BOLD}0.${NC} Назад"
    echo ""
    while true; do
        local choice=""
        read_tty "  ${YELLOW}Ваш выбор [0-3]:${NC} " choice
        case "$choice" in
            1) tuned-adm profile powersave && ok "Профиль: powersave"; break ;;
            2) tuned-adm profile server-powersave && ok "Профиль: server-powersave"; break ;;
            3) tuned-adm profile balanced && ok "Профиль: balanced"; break ;;
            0|"") break ;;
            *) warn "Введите цифру от 0 до 3." ;;
        esac
    done
    echo ""
}

# ─── Монитор ──────────────────────────────────────────────────────────────────
run_monitor() {
    local duration="$1"
    # ВАЖНО: datahound живёт в своём venv. Под sudo окружение сбрасывается ->
    # "ModuleNotFoundError: No module named 'click'". Активируем venv, если он есть.
    if [ -f "$MONITOR_DIR/.venv/bin/activate" ]; then
        ( cd "$MONITOR_DIR" && source .venv/bin/activate \
          && yes | $MONITOR_CMD --var duration="$duration" --all )
    else
        ( cd "$MONITOR_DIR" && yes | $MONITOR_CMD --var duration="$duration" --all )
    fi
}

# Проверка, что монитор вообще запустится (иначе фаза уйдёт впустую).
check_monitor_ready() {
    if [ ! -d "$MONITOR_DIR" ]; then
        err "Не найден каталог монитора: $MONITOR_DIR"; return 1
    fi
    local out
    if [ -f "$MONITOR_DIR/.venv/bin/activate" ]; then
        out=$( cd "$MONITOR_DIR" && source .venv/bin/activate && python -c "import click" 2>&1 )
    else
        out=$( cd "$MONITOR_DIR" && python -c "import click" 2>&1 )
    fi
    if [ -n "$out" ]; then
        err "Монитор не запустится: $out"
        echo -e "  ${YELLOW}Активируй venv datahound и поставь зависимости:${NC}"
        echo -e "    cd $MONITOR_DIR && source .venv/bin/activate && pip install -r requirements.txt"
        return 1
    fi
    ok "монитор готов (зависимости на месте)"
    return 0
}

# ─── Нагрузка (python-стенд в фоне) ───────────────────────────────────────────
LOAD_PID=""

start_load() {
    local scenario="$1"     # stress | user
    local minutes="$2"
    local out_dir="$3"

    if [ ! -f "$LOADTEST" ]; then
        err "Стенд не найден по пути LOADTEST=$LOADTEST"
        err "Поправьте константу LOADTEST в шапке скрипта. Фаза без нагрузки бессмысленна."
        return 1
    fi
    local py; py="$(command -v python3 || command -v python || true)"
    if [ -z "$py" ]; then
        err "Не найден python3/python в PATH."; return 1
    fi

    local worker_pid; worker_pid=$(get_indexer_worker_pid)
    local pid_arg=""
    [ -n "$worker_pid" ] && pid_arg="--sample-pid $worker_pid"
    # ВНИМАНИЕ: только ДОБАВЛЯЕМ к extra (+=), иначе затрём --safe-mode/--email-folder
    local extra="" nfiles="$NUM_FILES"
    if [ "$scenario" = "user" ]; then
        extra="--searches-per-hour $USER_SEARCHES_PER_HOUR --reindex-files $USER_REINDEX_FILES"
        nfiles="$USER_NUM_FILES"
    else
        nfiles="$STRESS_NUM_FILES"
    fi
    [ -n "$EMAIL_FOLDER" ] && extra="$extra --email-folder $EMAIL_FOLDER"
    [ "$SAFE_MODE" = "true" ] && extra="$extra --safe-mode"
    info "флаги стенда: $extra"

    local console="$out_dir.console.log"
    info "Запускаю нагрузку в фоне: сценарий=$scenario, ${minutes} мин, worker PID=${worker_pid:-нет}"
    # shellcheck disable=SC2086
    nohup "$py" "$LOADTEST" \
        --scenario "$scenario" --minutes "$minutes" \
        --base-url "$INDEXER_URL" --watched-dir "$WATCHED_DIR" \
        --num-files "$nfiles" --sample-psi $pid_arg $extra \
        --out "$out_dir" \
        > "$console" 2>&1 &
    LOAD_PID=$!

    # Проверка живости: за 15с питон должен подняться и записать "ПРОГОН НАЧАТ".
    local ok_started="no"
    for _ in $(seq 1 15); do
        if ! kill -0 "$LOAD_PID" 2>/dev/null; then break; fi
        if grep -q "ПРОГОН НАЧАТ" "$console" 2>/dev/null \
           || grep -q "ПРОГОН НАЧАТ" "$out_dir/timeline.log" 2>/dev/null; then
            ok_started="yes"; break
        fi
        sleep 1
    done

    if ! kill -0 "$LOAD_PID" 2>/dev/null && [ "$ok_started" = "no" ]; then
        err "Генератор нагрузки УПАЛ на старте. Последние строки лога ($console):"
        tail -n 25 "$console" 2>/dev/null | sed 's/^/      /'
        LOAD_PID=""
        return 1
    fi
    if [ "$ok_started" = "no" ]; then
        warn "Питон запущен, но за 15с не подтвердил старт. Лог: $console"
        tail -n 10 "$console" 2>/dev/null | sed 's/^/      /'
    fi
    ok "Нагрузка идёт (PID $LOAD_PID). Живой лог: tail -f $out_dir/timeline.log"
    return 0
}

stop_load() {
    if [ -n "$LOAD_PID" ] && kill -0 "$LOAD_PID" 2>/dev/null; then
        info "Останавливаю генератор нагрузки (PID $LOAD_PID)..."
        kill -INT "$LOAD_PID" 2>/dev/null || true
        # даём стенду дописать summary.json
        for _ in $(seq 1 15); do
            kill -0 "$LOAD_PID" 2>/dev/null || break
            sleep 1
        done
        kill -9 "$LOAD_PID" 2>/dev/null || true
    fi
    LOAD_PID=""
}

cleanup() { stop_load; }
trap cleanup EXIT INT TERM

# ─── Тестирование: 3 фазы ─────────────────────────────────────────────────────
run_testing() {
    echo ""
    hdr "  Подготовка к тестированию индексатора"
    sep
    show_system_info

    # Проверка зависимостей
    if [ ! -f "$LOADTEST" ]; then
        err "Не найден стенд: $LOADTEST"; return
    fi
    if [ ! -d "$MONITOR_DIR" ]; then
        err "Не найден каталог монитора: $MONITOR_DIR"; return
    fi

    if ! check_monitor_ready; then
        err "Тест отменён — сначала почини запуск монитора."
        return
    fi
    if ! preflight_email_root; then
        err "Предполётная проверка не пройдена — тест отменён."
        return
    fi
    preflight_indexer_user || true

    # ПОЛНЫЙ СБРОС ВЫПОЛНЯЕТСЯ ВСЕГДА (без вопроса). Две причины:
    #  1) на «известных» файлах индексатор скажет "nothing new" -> нагрузки не будет;
    #  2) накопленная очередь LSM роняет индексатор по assert_db_state при
    #     POST /config (подтверждено 27.08: 3-й прогон подряд без сброса = краш).
    # Отключить можно только явно: SKIP_RESET=1 ./test_sber_indexer.sh
    echo ""
    if [ "${SKIP_RESET:-0}" = "1" ]; then
        warn "SKIP_RESET=1 — сброс пропущен. Возможны 'nothing new' и краш LSM."
    else
        echo -e "  ${BOLD}Полный сброс индекса и очереди${NC} (выполняется автоматически)"
        full_reset || { err "Сброс не удался — тест отменён."; return; }
    fi

    # Выбор длительности фазы
    echo -e "  ${BOLD}Длительность КАЖДОЙ фазы:${NC}"
    echo -e "  ${BOLD}1.${NC} ${DIM}15 минут   (900 сек)${NC}  — быстрая проверка"
    echo -e "  ${BOLD}2.${NC} ${DIM}30 минут   (1800 сек)${NC}"
    echo -e "  ${BOLD}3.${NC} ${BOLD}1 час      (3600 сек)${NC} — стандарт"
    echo -e "  ${BOLD}4.${NC} Ввести вручную (в секундах)"
    echo ""
    local duration=3600
    while true; do
        local dur_choice=""
        read_tty "  ${YELLOW}Ваш выбор [1-4]:${NC} " dur_choice
        case "$dur_choice" in
            1) duration=900;  break ;;
            2) duration=1800; break ;;
            3) duration=3600; break ;;
            4)
                local custom=""
                read_tty "  Введите секунды: " custom
                if [[ "$custom" =~ ^[0-9]+$ ]] && [ "$custom" -gt 0 ]; then
                    duration="$custom"; break
                else
                    warn "Введите целое положительное число."
                fi
                ;;
            *) warn "Введите цифру от 1 до 4." ;;
        esac
    done
    local minutes=$(( duration / 60 ))
    mkdir -p "$RESULTS_ROOT"; fix_perms "$RESULTS_ROOT"

    echo ""
    echo -e "  ${BOLD}План:${NC} 3 фазы по ${BOLD}${minutes} мин${NC}, результаты в ${BOLD}$RESULTS_ROOT${NC}"
    # ── ВЫБОР ПОРЯДКА ФАЗ ────────────────────────────────────────────────
    # Обратный порядок обходит дефект индексатора: assert SQLite LSM возникает
    # при POST /config на очереди, прожившей ~час. Если нагрузка идёт ПЕРВОЙ,
    # config приходит на свежую очередь и падения нет (подтверждено смоуком).
    echo ""
    echo -e "  ${BOLD}Порядок фаз:${NC}"
    echo -e "    ${BOLD}1.${NC} Обычный:  простой -> фон -> пользователь"
    echo -e "       ${DIM}фон меряется на чистом индексе (ничего не проиндексировано)${NC}"
    echo -e "    ${BOLD}2.${NC} Обратный: пользователь -> фон -> простой  ${GREEN}(рекомендуется)${NC}"
    echo -e "       ${DIM}обходит краш LSM; фон меряется с прогретым индексом —${NC}"
    echo -e "       ${DIM}это ближе к реальности: у пользователя индекс всегда наполнен${NC}"
    local ord=""
    read_tty "  ${YELLOW}Ваш выбор [1-2]:${NC} " ord
    case "$ord" in
        2|"") PHASE_ORDER="reversed"; info "порядок: ОБРАТНЫЙ (пользователь первым)" ;;
        *)    PHASE_ORDER="normal";   info "порядок: обычный" ;;
    esac
    echo "ПОРЯДОК ФАЗ: $PHASE_ORDER ($(date '+%F %T'))" >> "$RESULTS_ROOT/phases.log"
    echo ""
    if [ "${PHASE_ORDER:-normal}" = "reversed" ]; then
        echo -e "    ФАЗА 1  ПОЛЬЗОВАТЕЛЬ ~${USER_SEARCHES_PER_HOUR} поисков/час, 1 индексация, 1 доиндексация"
        echo -e "    ФАЗА 2  ФОН          индексатор ВКЛЮЧЕН, запросов нет (индекс прогрет)"
        echo -e "    ФАЗА 3  ПРОСТОЙ      индексатор ВЫКЛЮЧЕН — ноль системы"
        echo -e "    ${DIM}фон индексатора = ФАЗА 2 − ФАЗА 3${NC}"
    else
        echo -e "    ФАЗА 1  ПРОСТОЙ      индексатор ВЫКЛЮЧЕН — ноль системы"
        echo -e "    ФАЗА 2  ФОН          индексатор ВКЛЮЧЕН, запросов нет — фон = Ф2−Ф1"
        echo -e "    ФАЗА 3  ПОЛЬЗОВАТЕЛЬ ~${USER_SEARCHES_PER_HOUR} поисков/час, 1 индексация, 1 доиндексация"
    fi
    echo -e "    ФАЗА 4  СТРЕСС       (опция) максимум: индексация + дозагрузка + поиск 2 зап/с"
    echo ""
    local do_stress=""
    read_tty "  ${YELLOW}Включить фазу 4 (СТРЕСС)?${NC} [д/н]: " do_stress
    do_stress=$(echo "$do_stress" | tr '[:upper:]' '[:lower:]')
    case "$do_stress" in д|y|yes|да) do_stress="yes" ;; *) do_stress="no"; info "Стресс-фаза выключена — будет 3 фазы." ;; esac
    echo ""
    local confirm=""
    read_tty "  ${YELLOW}Начать?${NC} [д/н]: " confirm
    confirm=$(echo "$confirm" | tr '[:upper:]' '[:lower:]')
    case "$confirm" in д|y|yes|да) ;; *) warn "Отменено."; return ;; esac

    # ── ФАЗЫ КАК ФУНКЦИИ (порядок задаётся PHASE_ORDER) ─────────────────────
    phase_idle() {
    # ── ФАЗА 1: ПРОСТОЙ (индексатор ВЫКЛЮЧЕН) ──────────────────────────────
    banner "$CYAN" "ФАЗА ПРОСТОЙ: индексатор ВЫКЛЮЧЕН (ноль системы)"
    if check_indexer_process; then
        warn "Индексатор запущен — для чистого нуля системы его нужно выключить."
        local choice=""
        read_tty "  ${YELLOW}Выключить сейчас?${NC} [д/н]: " choice
        choice=$(echo "$choice" | tr '[:upper:]' '[:lower:]')
        case "$choice" in
            д|y|yes|да) stop_indexer || err "Не смог остановить — фаза будет нечистой" ;;
            *) warn "Продолжаю с запущенным индексатором (ноль будет нечистым!)" ;;
        esac
    fi
    echo -e "  ${DIM}Не нагружайте систему во время этой фазы.${NC}"
    info "Монитор: ${minutes} мин..."
    echo "ПРОСТОЙ (индексатор выкл): старт $(date '+%F %T')" >> "$RESULTS_ROOT/phases.log"
    run_monitor "$duration"
    echo "ПРОСТОЙ (индексатор выкл): конец $(date '+%F %T')" >> "$RESULTS_ROOT/phases.log"
    ok "Фаза ПРОСТОЙ завершена."

    }

    phase_background() {
    # ── ФАЗА 2: ФОН (индексатор ВКЛЮЧЕН, запросов нет) ─────────────────────
    banner "$CYAN" "ФАЗА ФОН: индексатор ВКЛЮЧЕН, запросов нет"
    echo -e "  Фоновая нагрузка индексатора = (эта фаза) − (фаза 1)."
    ensure_indexer_running || return
    # индексация должна ПОЛНОСТЬЮ закончиться, иначе фон будет нечистым
    wait_indexing_settled || true
    echo -e "  ${DIM}Никаких запросов к индексатору в этой фазе не идёт.${NC}"
    info "Монитор: ${minutes} мин..."
    echo "ФОН (индексатор вкл, без запросов): старт $(date '+%F %T')" >> "$RESULTS_ROOT/phases.log"
    run_monitor "$duration"
    echo "ФОН (индексатор вкл, без запросов): конец $(date '+%F %T')" >> "$RESULTS_ROOT/phases.log"
    ok "Фаза ФОН завершена."

    }

    phase_user() {
    # ── ФАЗА 3: ПОЛЬЗОВАТЕЛЬСКИЙ ПУТЬ ───────────────────────────────────────
    banner "$GREEN" "ФАЗА ПОЛЬЗОВАТЕЛЬСКИЙ ПУТЬ"
    ensure_indexer_running || return
    reset_before_load
    if ! start_load "user" "$minutes" "$RESULTS_ROOT/phase3_user"; then
        local c=""
        read_tty "  ${YELLOW}Нагрузка не стартовала. Гнать монитор без неё?${NC} [д/н]: " c
        c=$(echo "$c" | tr '[:upper:]' '[:lower:]')
        case "$c" in д|y|yes|да) ;; *) warn "Фаза 3 прервана."; return ;; esac
    fi
    echo "ПОЛЬЗОВАТЕЛЬ: старт $(date '+%F %T')" >> "$RESULTS_ROOT/phases.log"
    info "Монитор: ${minutes} мин (пользовательский путь в фоне)..."
    run_monitor "$duration"
    echo "ПОЛЬЗОВАТЕЛЬ: конец $(date '+%F %T')" >> "$RESULTS_ROOT/phases.log"
    stop_load
    fix_perms "$RESULTS_ROOT/phase3_user"; fix_perms "$WATCHED_DIR"
    ok "Фаза ПОЛЬЗОВАТЕЛЬ завершена. Таймлайн нагрузки: $RESULTS_ROOT/phase3_user/timeline.log"

    }

    phase_stress() {
    # ── ФАЗА 4 (опция): СТРЕСС ──────────────────────────────────────────────
    if [ "$do_stress" = "yes" ]; then
        banner "$YELLOW" "ФАЗА СТРЕСС: максимальная нагрузка"
        ensure_indexer_running || return
        reset_before_load
        local wipe=""
        read_tty "  ${YELLOW}Сбросить индекс перед стрессом (rm -rf /root/.files-search)?${NC} [д/н]: " wipe
        wipe=$(echo "$wipe" | tr '[:upper:]' '[:lower:]')
        if [[ "$wipe" =~ ^(д|y|yes|да)$ ]]; then
            warn "Для сброса хранилища индексатор должен быть перезапущен:"
            echo -e "    1) остановите main (Ctrl+C в его терминале)"
            echo -e "    2) хранилище удалю сам после подтверждения"
            echo -e "    3) запустите main заново (PIN)"
            local go=""
            read_tty "  Индексатор остановлен, удалить хранилище? [д/н]: " go
            go=$(echo "$go" | tr '[:upper:]' '[:lower:]')
            if [[ "$go" =~ ^(д|y|yes|да)$ ]]; then
                rm -rf /root/.files-search && ok "Хранилище индекса удалено."
                wait_indexer_started || return
            fi
        fi
        if ! start_load "stress" "$minutes" "$RESULTS_ROOT/phase4_stress"; then
            local c=""
            read_tty "  ${YELLOW}Нагрузка не стартовала. Гнать монитор без неё?${NC} [д/н]: " c
            c=$(echo "$c" | tr '[:upper:]' '[:lower:]')
            case "$c" in д|y|yes|да) ;; *) warn "Фаза 4 прервана."; return ;; esac
        fi
        echo "СТРЕСС: старт $(date '+%F %T')" >> "$RESULTS_ROOT/phases.log"
        info "Монитор: ${minutes} мин (стресс в фоне)..."
        run_monitor "$duration"
        echo "СТРЕСС: конец $(date '+%F %T')" >> "$RESULTS_ROOT/phases.log"
        stop_load
        fix_perms "$RESULTS_ROOT/phase4_stress"; fix_perms "$WATCHED_DIR"
        ok "Фаза СТРЕСС завершена. Таймлайн: $RESULTS_ROOT/phase4_stress/timeline.log"
    else
        info "Фаза 4 (стресс) пропущена по выбору."
    fi

    }

    # ── ЗАПУСК ФАЗ В ВЫБРАННОМ ПОРЯДКЕ ──────────────────────────────────────
    if [ "${PHASE_ORDER:-normal}" = "reversed" ]; then
        # Пользователь ПЕРВЫМ: POST /config приходит на свежую очередь -> нет краша
        # assert SQLite LSM. Фон меряется с прогретым индексом (ближе к реальности).
        phase_user
        phase_background
        phase_idle
        phase_stress
    else
        phase_idle
        phase_background
        phase_user
        phase_stress
    fi

    # ── Итог ────────────────────────────────────────────────────────────────
    # corpus_staging дублирует корпус и весит десятки МБ — предложим удалить, логи оставим
    local staging_n
    staging_n=$(find "$RESULTS_ROOT" -type d -name corpus_staging 2>/dev/null | wc -l)
    if [ "$staging_n" -gt 0 ]; then
        local delst=""
        read_tty "  ${YELLOW}Удалить сгенерированные корпуса (corpus_staging), оставив логи?${NC} [д/н]: " delst
        delst=$(echo "$delst" | tr '[:upper:]' '[:lower:]')
        if [[ "$delst" =~ ^(д|y|yes|да)$ ]]; then
            find "$RESULTS_ROOT" -type d -name corpus_staging -exec rm -rf {} + 2>/dev/null || true
            ok "corpus_staging удалён, логи и summary.json сохранены."
        fi
    fi
    clean_watched_dir
    fix_perms "$RESULTS_ROOT"; fix_perms "$WATCHED_DIR"
    banner "$GREEN" "✓ ТЕСТИРОВАНИЕ ЗАВЕРШЕНО"
    echo -e "  ${GREEN}Фаза 1 (простой, индексатор выкл) — выполнена${NC}"
    echo -e "  ${GREEN}Фаза 2 (фон индексатора)          — выполнена${NC}"
    echo -e "  ${GREEN}Фаза 3 (пользовательский путь)    — выполнена${NC}"
    if [ "$do_stress" = "yes" ]; then
        echo -e "  ${GREEN}Фаза 4 (стресс)                   — выполнена${NC}"
    else
        echo -e "  ${DIM}Фаза 4 (стресс)                   — пропущена${NC}"
    fi
    echo ""
    echo -e "  Всё в ${BOLD}$RESULTS_ROOT${NC}:"
    echo -e "    phases.log                     — время начала/конца фаз (стыковка с монитором)"
    echo -e "    phase3_user/timeline.log       — каждое действие пользовательского пути"
    echo -e "    phase3_user/summary.json       — латентности p50/p95/p99"
    if [ "$do_stress" = "yes" ]; then
        echo -e "    phase4_stress/timeline.log     — каждое действие стресс-нагрузки"
        echo -e "    phase4_stress/summary.json     — сводка"
    fi
    echo ""
}

# ─── Главное меню ─────────────────────────────────────────────────────────────
main_menu() {
    while true; do
        show_system_info
        echo -e "  ${BOLD}╔═══════════════════════════════════╗${NC}"
        echo -e "  ${BOLD}║         Главное меню              ║${NC}"
        echo -e "  ${BOLD}╠═══════════════════════════════════╣${NC}"
        echo -e "  ${BOLD}║${NC}  ${BOLD}1.${NC} Включить / Выключить          ${BOLD}║${NC}"
        echo -e "  ${BOLD}║${NC}     ${DIM}индексатор (main)${NC}             ${BOLD}║${NC}"
        echo -e "  ${BOLD}║${NC}  ${BOLD}2.${NC} Выбрать режим питания         ${BOLD}║${NC}"
        echo -e "  ${BOLD}║${NC}  ${BOLD}3.${NC} ${CYAN}Начать тестирование (4 фазы)${NC}  ${BOLD}║${NC}"
        echo -e "  ${BOLD}║${NC}  ${BOLD}4.${NC} Очистить тестовую папку       ${BOLD}║${NC}"
        echo -e "  ${BOLD}║${NC}  ${BOLD}5.${NC} Полный сброс (индекс+папка)   ${BOLD}║${NC}"
        echo -e "  ${BOLD}║${NC}  ${BOLD}0.${NC} Выход                         ${BOLD}║${NC}"
        echo -e "  ${BOLD}╚═══════════════════════════════════╝${NC}"
        echo ""
        local choice=""
        read_tty "  ${YELLOW}Ваш выбор [0-5]:${NC} " choice
        case "$choice" in
            1) toggle_indexer ;;
            2) select_power_profile ;;
            3) run_testing ;;
            4) echo ""; clean_watched_dir; fix_perms "$WATCHED_DIR"; echo "" ;;
            5) echo ""; full_reset; echo "" ;;
            0|"")
                echo -e "\n  ${DIM}Выход из скрипта.${NC}\n"
                exit 0
                ;;
            *) warn "Введите цифру от 0 до 5."; echo "" ;;
        esac
    done
}

# ─── Точка входа ──────────────────────────────────────────────────────────────
main_menu
