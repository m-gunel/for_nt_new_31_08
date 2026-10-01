#!/bin/sh
# Сборка .deb без sudo.
#
# Для `make deb` нужны debhelper и dpkg-dev. На рабочем ноутбуке их может не
# быть, а поставить через `sudo apt install` может быть некому: у разработчика
# нет прав администратора. Этот скрипт обходится без них:
#
#   1. если debhelper и dpkg-dev уже есть в системе — просто `make deb`;
#   2. иначе скачивает НЕДОСТАЮЩИЕ пакеты обычным пользователем и распаковывает
#      их в домашний каталог (`dpkg -x`) — в систему ничего не устанавливается.
#      Сначала пробует `apt-get download`; на SberOS у пользователя он не
#      работает (файлы с адресами репозиториев закрыты для чтения), и тогда
#      пакеты берутся напрямую из корпоративного зеркала Debian в SberOSC —
#      того же, что в Dockerfile, — с токеном из .secret
#      (scripts/fetch_build_tools.py: подпись Release и контрольные суммы
#      проверяются так же, как это делает apt);
#   3. запускает ту же `make deb` с этими инструментами.
#
# Сборка идёт тем же путём, что и в CI: debhelper выравнивает права
# (dh_fixperms), а scripts/check_deb.sh в конце `make deb` проверяет готовый
# пакет. Ручной сборки в обход debhelper здесь нет — именно она дала 1.0.6 с
# юнитами 0600 («Unit ... could not be found»).
#
# Перед запуском должен быть готов dist/ (`make build`). Запуск из корня
# проекта:
#
#     sh scripts/build_deb_nosudo.sh
#
# Готовый пакет — уровнем выше каталога проекта: ../lib-local-rag_<версия>_amd64.deb.
# Скачанные инструменты кешируются в ~/.cache/lib-local-rag-debtools (другой
# каталог — переменная LQRAG_DEBTOOLS); повторный запуск их не качает.
set -eu

cd "$(dirname "$0")/.."

have_dh=""; have_dpkgdev=""
command -v dh >/dev/null 2>&1 && have_dh=yes
command -v dpkg-buildpackage >/dev/null 2>&1 && have_dpkgdev=yes

if [ -n "$have_dh" ] && [ -n "$have_dpkgdev" ]; then
    echo "build_deb_nosudo: debhelper и dpkg-dev уже есть в системе — обычная сборка (make deb)."
    exec make deb
fi

TOOLS="${LQRAG_DEBTOOLS:-$HOME/.cache/lib-local-rag-debtools}"
ROOT="$TOOLS/root"

# Качаем только недостающее. dpkg-dev, если он есть в системе, не подменяем:
# его Perl-модули (Dpkg::*) должны совпадать с установленным dpkg.
# dh-autoreconf — потому что dh вызывает dh_autoreconf в стандартной
# последовательности (у нас он ничего не делает, но должен существовать).
PKGS=""
[ -n "$have_dpkgdev" ] || PKGS="dpkg-dev libdpkg-perl"
[ -n "$have_dh" ] || PKGS="$PKGS debhelper libdebhelper-perl dh-autoreconf"

tools_ready() {
    { [ -n "$have_dh" ] || [ -x "$ROOT/usr/bin/dh" ]; } &&
        { [ -n "$have_dpkgdev" ] || [ -x "$ROOT/usr/bin/dpkg-buildpackage" ]; }
}

if ! tools_ready; then
    echo "build_deb_nosudo: в системе нет:$PKGS — скачиваю в $TOOLS"
    echo "                  (без sudo, в систему ничего не ставится)"
    rm -rf "$TOOLS/dl" "$ROOT"
    mkdir -p "$TOOLS/dl" "$ROOT"
    # shellcheck disable=SC2086  # PKGS — список имён пакетов, дробление нужно
    if command -v apt-get >/dev/null 2>&1 &&
        (cd "$TOOLS/dl" && apt-get download $PKGS >/dev/null 2>&1); then
        echo "build_deb_nosudo: скачано через apt-get download"
    else
        rm -f "$TOOLS"/dl/*.deb
        echo "build_deb_nosudo: apt-get download недоступен пользователю — беру из зеркала Debian в SberOSC"
        # Токен — из окружения или из .secret (та же строка OSC_TOKEN=..., что
        # нужна make build). Файл не «исполняем» через `.`: токен — данные.
        if [ -z "${OSC_TOKEN:-}" ] && [ -f .secret ]; then
            OSC_TOKEN=$(sed -n 's/^[[:space:]]*OSC_TOKEN[[:space:]]*=[[:space:]]*//p' .secret | head -n 1)
        fi
        export OSC_TOKEN="${OSC_TOKEN:-}"
        # shellcheck disable=SC2086
        if ! python3 scripts/fetch_build_tools.py --dest "$TOOLS/dl" $PKGS; then
            echo "build_deb_nosudo: не удалось скачать:$PKGS" >&2
            echo "  Проверьте файл .secret (OSC_TOKEN=...) и доступ к корпоративной сети," >&2
            echo "  либо соберите пакет в CI (debian/README.md, §2, вариант 1)." >&2
            exit 1
        fi
    fi
    for f in "$TOOLS"/dl/*.deb; do
        dpkg -x "$f" "$ROOT"
    done
fi

# debhelper и dpkg-dev написаны на Perl и берут модули из полного perl
# (File::Copy и др.). В настольной системе он есть всегда; в урезанной —
# только perl-base, и сборка упадёт с «Can't locate File/Copy.pm».
if ! perl -MFile::Copy -MFile::Temp -e 1 >/dev/null 2>&1; then
    echo "build_deb_nosudo: в системе неполный Perl (нет модуля File::Copy)." >&2
    echo "  Нужен пакет perl — попросите администратора или соберите пакет в CI." >&2
    exit 1
fi

PATH="$ROOT/usr/bin:$PATH"
PERL5LIB="$ROOT/usr/share/perl5${PERL5LIB:+:$PERL5LIB}"
# Шаблоны сниппетов (enable пользовательских юнитов и т.п.) debhelper ищет
# в /usr/share/debhelper/autoscripts, если не указано иное.
DH_AUTOSCRIPTDIR="$ROOT/usr/share/debhelper/autoscripts"
export PATH PERL5LIB DH_AUTOSCRIPTDIR

echo "build_deb_nosudo: debhelper из $(command -v dh), dpkg-buildpackage из $(command -v dpkg-buildpackage)"

# Сборка — через debian/rules напрямую (scripts/deb_rules_build.sh), а не
# dpkg-buildpackage. Тот читает базу /var/lib/dpkg/status: для проверки
# Build-Depends (debhelper распакован в домашний каталог, база о нём всё равно
# не знает) и для служебного .buildinfo после сборки. На SberOS эта база
# закрыта от пользователя, и dpkg-buildpackage падает, хотя .deb уже собран.
# Гейты make deb и check_deb.sh отрабатывают как обычно.
exec make deb DEB_BUILD_CMD="sh scripts/deb_rules_build.sh"
