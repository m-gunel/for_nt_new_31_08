#!/bin/sh
# Сборка .deb через debian/rules напрямую — то, что делает `dpkg-buildpackage -b`,
# без его служебных шагов вокруг.
#
# Зачем. dpkg-buildpackage после сборки пакета вызывает dpkg-genbuildinfo, а
# тот читает базу установленных пакетов /var/lib/dpkg/status, чтобы записать
# их список в служебный файл .buildinfo (он нужен только для загрузки пакета в
# архив Debian). На SberOS эта база закрыта от обычного пользователя
# («не удалось открыть /var/lib/dpkg/status: Operation not permitted»), и
# dpkg-buildpackage падает, хотя .deb уже собран. Проверку Build-Depends
# (dpkg-checkbuilddeps) он делает по той же базе — её в режиме без sudo всё
# равно приходится снимать.
#
# Сама сборка та же: debhelper, dh_fixperms, вставки в maintainer-скрипты —
# всё это делает debian/rules. Зовёт этот скрипт `make deb` через
# DEB_BUILD_CMD (так его подставляет scripts/build_deb_nosudo.sh), поэтому
# гейты до сборки и check_deb.sh после неё отрабатывают как обычно.
set -eu

cd "$(dirname "$0")/.."

# Окружение, которое dpkg-buildpackage выставляет для debian/rules.
#
# DEB_RULES_REQUIRES_ROOT — ОБЯЗАТЕЛЬНО. Без него debhelper считает, что
# пакет собирается от root, и не велит dpkg-deb записать владельцем файлов
# root: в .deb уехал бы uid сборщика. Значение — то, что объявлено в
# debian/control (Rules-Requires-Root).
rrr=$(sed -n 's/^Rules-Requires-Root:[[:space:]]*//p' debian/control | head -n 1)
DEB_RULES_REQUIRES_ROOT="${rrr:-no}"
# Время сборки — из changelog, как у dpkg-buildpackage: даты файлов в
# пакете не зависят от того, когда и где его собрали.
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(dpkg-parsechangelog -STimestamp)}"
export DEB_RULES_REQUIRES_ROOT SOURCE_DATE_EPOCH
# dpkg-buildpackage приводит umask к 0022; права в пакете всё равно
# выравнивает dh_fixperms, но пусть и остальное совпадает.
umask 022

echo "deb_rules_build: debian/rules clean → build → binary (Rules-Requires-Root: $DEB_RULES_REQUIRES_ROOT)"
debian/rules clean
debian/rules build
debian/rules binary
