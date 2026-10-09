#!/bin/sh
# Сборка .deb через debian/rules напрямую — то же, что `dpkg-buildpackage -b`,
# без его служебных шагов. dpkg-checkbuilddeps и dpkg-genbuildinfo (.buildinfo)
# читают /var/lib/dpkg/status, а на SberOS база закрыта от пользователя
# («Operation not permitted»), и dpkg-buildpackage падает, хотя .deb уже собран.
#
# Зовёт скрипт `make deb` через DEB_BUILD_CMD (подставляет build_deb_nosudo.sh),
# так что гейты до сборки и check_deb.sh после неё отрабатывают как обычно.
set -eu

cd "$(dirname "$0")/.."

# Окружение, которое dpkg-buildpackage выставляет для debian/rules.
# Без DEB_RULES_REQUIRES_ROOT debhelper не велит dpkg-deb ставить владельцем root,
# и в .deb уедет uid сборщика. Значение — Rules-Requires-Root из debian/control.
rrr=$(sed -n 's/^Rules-Requires-Root:[[:space:]]*//p' debian/control | head -n 1)
DEB_RULES_REQUIRES_ROOT="${rrr:-no}"
# Даты файлов в пакете — из changelog, как у dpkg-buildpackage.
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(dpkg-parsechangelog -STimestamp)}"
export DEB_RULES_REQUIRES_ROOT SOURCE_DATE_EPOCH
# Как у dpkg-buildpackage; права в пакете дополнительно выравнивает dh_fixperms.
umask 022
# Как dpkg-buildpackage («debian/rules is not executable; fixing that»): бит +x
# теряется при переносе проекта архивом, а dh тоже зовёт debian/rules
# (цели override_dh_*).
[ -x debian/rules ] || chmod 0755 debian/rules

echo "deb_rules_build: debian/rules clean → build → binary (Rules-Requires-Root: $DEB_RULES_REQUIRES_ROOT)"
debian/rules clean
debian/rules build
debian/rules binary
