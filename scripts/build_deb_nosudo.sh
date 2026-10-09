#!/bin/sh
# Сборка .deb без sudo: на рабочем ноутбуке может не быть debhelper и dpkg-dev,
# а прав администратора, чтобы их поставить, у разработчика нет.
#
# Недостающие пакеты качаются обычным пользователем и распаковываются в домашний
# каталог (`dpkg -x`), в систему ничего не ставится. Сначала `apt-get download`;
# на SberOS он пользователю недоступен (списки репозиториев закрыты на чтение), и
# тогда пакеты берутся из зеркала Debian в SberOSC с токеном из .secret
# (scripts/fetch_build_tools.py проверяет подпись Release и суммы, как apt).
# Дальше — та же `make deb`: dh_fixperms и scripts/check_deb.sh отрабатывают как в CI.
#
# Нужен готовый dist/ (`make build`). Запуск из корня проекта:
#
#     sh scripts/build_deb_nosudo.sh
#
# Пакет — ../lib-local-rag_<версия>_amd64.deb. Инструменты кешируются в
# ~/.cache/lib-local-rag-debtools (или LQRAG_DEBTOOLS) и повторно не качаются.
set -eu

cd "$(dirname "$0")/.."

have_dh=""; have_dpkgdev=""
command -v dh >/dev/null 2>&1 && have_dh=yes
command -v dpkg-buildpackage >/dev/null 2>&1 && have_dpkgdev=yes

if [ -n "$have_dh" ] && [ -n "$have_dpkgdev" ]; then
    # dpkg-buildpackage читает базу dpkg, а на SberOS она закрыта, кто бы ни
    # ставил debhelper. Проверка — настоящим чтением: отказ там «Operation not
    # permitted», не по правам файла, и `[ -r ]` его может не увидеть.
    if cat "${DPKG_ADMINDIR:-/var/lib/dpkg}/status" >/dev/null 2>&1; then
        echo "build_deb_nosudo: debhelper и dpkg-dev уже есть в системе — обычная сборка (make deb)."
        exec make deb
    fi
    echo "build_deb_nosudo: debhelper и dpkg-dev уже есть в системе, база dpkg закрыта — сборка через debian/rules."
    exec make deb DEB_BUILD_CMD="sh scripts/deb_rules_build.sh"
fi

TOOLS="${LQRAG_DEBTOOLS:-$HOME/.cache/lib-local-rag-debtools}"
ROOT="$TOOLS/root"

# Системный dpkg-dev не подменяем: его модули Dpkg::* должны совпадать с dpkg.
# dh-autoreconf нужен лишь потому, что dh зовёт dh_autoreconf в стандартной цепочке.
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
        # Токен — из окружения или .secret; файл читаем, а не исполняем через `.`.
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

# debhelper и dpkg-dev требуют полного perl (File::Copy и др.); в урезанной системе
# есть только perl-base, и сборка упадёт с «Can't locate File/Copy.pm».
if ! perl -MFile::Copy -MFile::Temp -e 1 >/dev/null 2>&1; then
    echo "build_deb_nosudo: в системе неполный Perl (нет модуля File::Copy)." >&2
    echo "  Нужен пакет perl — попросите администратора или соберите пакет в CI." >&2
    exit 1
fi

PATH="$ROOT/usr/bin:$PATH"
PERL5LIB="$ROOT/usr/share/perl5${PERL5LIB:+:$PERL5LIB}"
# Иначе debhelper ищет шаблоны сниппетов в системном /usr/share/debhelper/autoscripts.
DH_AUTOSCRIPTDIR="$ROOT/usr/share/debhelper/autoscripts"
export PATH PERL5LIB DH_AUTOSCRIPTDIR

echo "build_deb_nosudo: debhelper из $(command -v dh), dpkg-buildpackage из $(command -v dpkg-buildpackage)"

# Через debian/rules напрямую (scripts/deb_rules_build.sh): dpkg-buildpackage
# падает на закрытой на SberOS базе /var/lib/dpkg/status.
exec make deb DEB_BUILD_CMD="sh scripts/deb_rules_build.sh"
