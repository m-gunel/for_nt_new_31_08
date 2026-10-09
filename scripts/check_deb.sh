#!/bin/sh
# Проверка ГОТОВОГО пакета lib-local-rag перед тем, как отдавать его людям.
#
#     sh scripts/check_deb.sh ../lib-local-rag_1.0.7_amd64.deb
#
# Ни сборка, ни dpkg -i не ловят пакет, собранный мимо dh_fixperms при umask 077:
# юниты 0600 root включит, а systemd --user их не прочитает
# («Unit lib-local-rag.service could not be found»).
#
# Проверки идут до конца, код возврата ненулевой, если провалилась хоть одна.
# Нужен dpkg-deb из dpkg (у busybox нет --fsys-tarfile), остальное — POSIX sh и
# awk/grep/sed/tar. Запускать через `sh`: бит +x теряется при переносе архивом.
# Ожидаемая версия — из src/version.py рядом; отдельно от репозитория — LQRAG_SRC=<корень>.
set -eu

if [ $# -ne 1 ]; then
    echo "использование: sh scripts/check_deb.sh <путь к .deb>" >&2
    exit 2
fi
deb=$1
if [ ! -f "$deb" ]; then
    echo "check_deb: файла нет: $deb" >&2
    exit 2
fi
if ! command -v dpkg-deb >/dev/null 2>&1; then
    echo "check_deb: нужен dpkg-deb (пакет dpkg)" >&2
    exit 2
fi

src=${LQRAG_SRC:-$(cd "$(dirname "$0")/.." && pwd)}

fail=0
bad() {
    echo "check_deb: ОШИБКА: $*" >&2
    fail=$((fail + 1))
}

tmp=$(mktemp -d "${TMPDIR:-/tmp}/check_deb.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
trap 'exit 1' HUP INT TERM

UNIT_DIR=./usr/lib/systemd/user
UNITS="lib-local-rag.service lib-local-rag-mcp-register.service lib-local-rag-mcp-register-wsc.path lib-local-rag-mcp-register-gigacode.path"
# Их запускают systemd --user, агент и пользовательский юнит, то есть не root.
EXECS="./opt/lib-local-rag/main ./usr/lib/lib-local-rag/mcp/mcp_launcher.sh ./usr/lib/lib-local-rag/mcp/register_mcp.py"

# Права и владельцы в data.tar. Строка dpkg-deb -c (вывод tar -tv):
#     права владелец/группа размер дата время путь [-> цель]
if ! dpkg-deb -c "$deb" >"$tmp/list"; then
    echo "check_deb: dpkg-deb не смог прочитать пакет: $deb" >&2
    exit 1
fi

awk -v execs="$EXECS" -v unitdir="$UNIT_DIR/" '
BEGIN { n = split(execs, e, " "); for (i = 1; i <= n; i++) want[e[i]] = 1 }
{
    mode = $1; owner = $2
    path = $6
    for (i = 7; i <= NF; i++) path = path " " $i
    sub(/ -> .*$/, "", path)
    sub(/ link to .*$/, "", path)
    t = substr(mode, 1, 1)

    if (owner != "root/root")
        print "владелец " owner " вместо root/root: " path

    # Каталог без r-x для остальных закрывает и файлы с верными правами.
    if (t == "d") {
        if (substr(mode, 8, 1) != "r" || substr(mode, 10, 1) !~ /[xt]/)
            print "каталог закрыт для пользователей (" mode "): " path
    } else if (t == "-" || t == "h") {
        if (substr(mode, 8, 1) != "r")
            print "файл нечитаем для пользователей (" mode "): " path
    }

    # Юниты ровно 0644, как их кладёт dh_installsystemduser; иное — сборка мимо debhelper.
    if (t != "d" && index(path, unitdir) == 1 && mode != "-rw-r--r--")
        print "юнит с правами " mode " вместо -rw-r--r--: " path

    # Системных юнитов в пакете быть не должно: демон от root пишет индекс
    # в /root/.files-search, а мост от имени пользователя туда не заглянет.
    if (t != "d" && path ~ /^\.\/(usr\/)?lib\/systemd\/system\//)
        print "системный юнит в пакете: " path

    if (path in want) {
        seen[path] = 1
        if (substr(mode, 4, 1) != "x" || substr(mode, 7, 1) != "x" || substr(mode, 10, 1) != "x")
            print "не исполняем для всех (" mode "): " path
    }
}
END {
    for (p in want)
        if (!(p in seen))
            print "нет в пакете: " p
}' "$tmp/list" >"$tmp/perm_errors"

if [ -s "$tmp/perm_errors" ]; then
    bad "права в пакете неверные, у пользователя это даст «Unit ... could not be found». Первые строки:"
    # Сначала юниты: их нечитаемость и есть «could not be found».
    { grep -F 'юнит' "$tmp/perm_errors" || true; grep -vF 'юнит' "$tmp/perm_errors" || true; } | head -n 20 >&2
    total=$(wc -l <"$tmp/perm_errors")
    echo "  (всего замечаний: $total). Собирайте только через make deb: dh_fixperms приводит права к 0644/0755." >&2
fi

if ! dpkg-deb -e "$deb" "$tmp/ctl"; then
    echo "check_deb: dpkg-deb не смог извлечь control-архив: $deb" >&2
    exit 1
fi
control=$tmp/ctl/control

if [ ! -f "$control" ]; then
    bad "в пакете нет control"
else
    # Второй абзац (после пустой строки) dpkg не примет: так бывает при ручной склейке.
    paras=$(awk 'NF == 0 { gap = 1; next } { if (!started || gap) n++; started = 1; gap = 0 } END { print n + 0 }' "$control")
    [ "$paras" = "1" ] || bad "в control абзацев: $paras, должен быть ровно один"

    grep -qx 'Package: lib-local-rag' "$control" || bad "в control нет строки «Package: lib-local-rag»"

    arch=$(sed -n 's/^Architecture:[[:space:]]*//p' "$control")
    [ -n "$arch" ] || bad "в control нет поля Architecture"

    got=$(sed -n 's/^Version:[[:space:]]*//p' "$control")
    if [ ! -f "$src/src/version.py" ]; then
        bad "не найден $src/src/version.py, сверить версию не с чем (корень репозитория задаёт LQRAG_SRC)"
    else
        want=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$src/src/version.py")
        if [ -z "$got" ]; then
            bad "в control нет поля Version"
        elif [ "$got" != "$want" ]; then
            bad "версия пакета $got, а в src/version.py $want"
        fi
    fi
fi

# Все четыре юнита и в каждом ConditionUser=!@system: юниты включены глобально, и
# без условия демон поднимется у root и greeter, а root проиндексирует весь /home.
members=
for u in $UNITS; do
    if awk -v p="$UNIT_DIR/$u" '{ path = $6; for (i = 7; i <= NF; i++) path = path " " $i } path == p { f = 1 } END { exit !f }' "$tmp/list"; then
        members="$members $UNIT_DIR/$u"
    else
        bad "в пакете нет юнита $u"
    fi
done
if [ -n "$members" ]; then
    mkdir "$tmp/data"
    # Имена юнитов без пробелов, поэтому $members без кавычек, по словам.
    # shellcheck disable=SC2086
    if dpkg-deb --fsys-tarfile "$deb" | tar -xf - -C "$tmp/data" $members; then
        for m in $members; do
            grep -Eq '^[[:space:]]*ConditionUser=!@system[[:space:]]*$' "$tmp/data/$m" \
                || bad "в $m нет строки ConditionUser=!@system"
        done
    else
        bad "не удалось извлечь юниты из data.tar"
    fi
fi

# Нераскрытый #DEBHELPER# — maintainer-скрипт попал в пакет мимо dh_installdeb.
for f in preinst postinst prerm postrm; do
    if [ ! -f "$tmp/ctl/$f" ]; then
        bad "в пакете нет $f"
    elif grep -qF '#DEBHELPER#' "$tmp/ctl/$f"; then
        bad "в $f остался нераскрытый маркер #DEBHELPER#"
    fi
done

postinst=$tmp/ctl/postinst
if [ -f "$postinst" ]; then
    # Нет вставки dh_installsystemduser — пакет собран в обход debhelper и dh_fixperms.
    grep -qF 'Automatically added by dh_installsystemduser' "$postinst" \
        || bad "в postinst нет вставки dh_installsystemduser: пакет собран в обход debhelper"
    # enable — для каждого юнита: сниппет на юнит появляется только при секции
    # [Install] (и без -X/--name), а общая вставка есть и без него. Строка сниппета:
    # deb-systemd-helper --user enable 'ЮНИТ' >/dev/null || true (юнитов может быть несколько).
    for u in $UNITS; do
        grep -F 'deb-systemd-helper --user enable ' "$postinst" | grep -qF "'$u'" \
            || bad "в postinst нет enable от debhelper для $u: проверьте [Install] в юните"
    done
    # Индексация стартует сама; подсказка про /activate уводит поиск причины не туда.
    if grep -qF 'api/v1/activate' "$postinst"; then
        bad "в postinst устаревшая подсказка про /api/v1/activate"
    fi
fi

# Юнит, которого нет в purge-вставке debhelper в postrm, останется включённым после удаления.
postrm=$tmp/ctl/postrm
if [ -f "$postrm" ]; then
    for u in $UNITS; do
        grep -F 'deb-systemd-helper --user purge ' "$postrm" | grep -qF "'$u'" \
            || bad "в postrm нет purge от debhelper для $u"
    done
fi

# preinst откладывает distro.yml ручных сборок до 1.0.7 (там он не conffile): иначе
# dpkg без терминала (SberEMP) падает на вопросе о конфиге и пакет не настроится.
preinst=$tmp/ctl/preinst
if [ -f "$preinst" ] && ! grep -qF 'distro.yml.localrag-old' "$preinst"; then
    bad "в preinst нет переноса distro.yml в distro.yml.localrag-old: обновление с 1.0.6 упадёт на вопросе о conffile"
fi

# Без статуса conffile правка distro.yml молча затрётся при обновлении пакета.
if [ ! -f "$tmp/ctl/conffiles" ] || ! grep -Eq '(^|[[:space:]])/etc/lib-local-rag/distro\.yml$' "$tmp/ctl/conffiles"; then
    bad "в conffiles нет /etc/lib-local-rag/distro.yml"
fi

# Ни shlibs, ни triggers: сотни .so бандла PyInstaller — не системные библиотеки,
# и ldconfig на каждую установку не нужен (пустой override_dh_makeshlibs в debian/rules).
for f in shlibs triggers; do
    if [ -e "$tmp/ctl/$f" ]; then
        bad "в control-архиве есть $f: проверьте override_dh_makeshlibs в debian/rules"
    fi
done

if [ "$fail" -ne 0 ]; then
    echo "check_deb: провалено проверок: $fail. Пакет НЕ выпускать: $deb" >&2
    exit 1
fi
echo "check_deb: OK $deb"
