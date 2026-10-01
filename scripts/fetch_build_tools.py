#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Скачать .deb инструментов сборки из зеркала Debian — без apt и без root.

Зачем. scripts/build_deb_nosudo.sh сначала пробует `apt-get download`. На
SberOS это не работает у обычного пользователя: файлы с адресами репозиториев
(/etc/apt/sources.list.d/*) закрыты для чтения, и apt «не знает», откуда
качать. Этот скрипт идёт в то же корпоративное зеркало Debian, которым
пользуется Dockerfile (SberOSC, debian_yandex), напрямую по HTTPS с токеном
из .secret.

Проверки — те же, что делает apt:
  1. подпись dists/<suite>/Release (Release.gpg) ключом архива Debian —
     через gpgv или sqv (в Debian 13 apt проверяет подписи именно sqv), если
     они есть в системе; иначе предупреждение, опора на HTTPS и хэши ниже;
  2. SHA256 индекса Packages.xz — по Release;
  3. SHA256 каждого .deb — по Packages.

Только stdlib, совместимо с python3.6.

    python3 fetch_build_tools.py --dest DIR debhelper libdebhelper-perl dh-autoreconf

Окружение:
    OSC_TOKEN         токен SberOSC (Basic-авторизация, пользователь «token»)
    LQRAG_DEB_MIRROR  корень зеркала (по умолчанию — зеркало SberOSC)
    LQRAG_DEB_SUITE   ветка Debian (по умолчанию — по /etc/debian_version)
"""

import argparse
import base64
import glob
import hashlib
import lzma
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

DEFAULT_MIRROR = "https://sberosc.sigma.sbrf.ru/repo/debian/debian_yandex"
# Номер из /etc/debian_version → кодовое имя ветки. SberOS основана на Debian
# и хранит в этом файле номер базы (13.x — trixie).
SUITES = {"11": "bullseye", "12": "bookworm", "13": "trixie", "14": "forky"}
DEFAULT_SUITE = "trixie"
KEYRING_GLOBS = (
    "/usr/share/keyrings/debian-archive-keyring.gpg",
    "/usr/share/keyrings/debian-archive-keyring.pgp",
)


def fail(message):
    sys.stderr.write("fetch_build_tools: %s\n" % message)
    sys.exit(1)


def detect_suite():
    try:
        with open("/etc/debian_version", "r") as handle:
            text = handle.read().strip()
    except OSError:
        return DEFAULT_SUITE
    major = text.split(".", 1)[0]
    if major in SUITES:
        return SUITES[major]
    # «trixie/sid» и подобное — уже кодовое имя.
    name = text.split("/", 1)[0]
    return name if name.isalpha() else DEFAULT_SUITE


def detect_arch():
    try:
        out = subprocess.check_output(["dpkg", "--print-architecture"])
        return out.decode().strip() or "amd64"
    except (OSError, subprocess.CalledProcessError):
        return "amd64"


def fetch(url, token):
    request = urllib.request.Request(url)
    if token:
        auth = base64.b64encode(("token:%s" % token).encode()).decode()
        request.add_header("Authorization", "Basic %s" % auth)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            fail(
                "зеркало ответило %d на %s — токен не подошёл или не задан. "
                "Проверьте файл .secret (строка OSC_TOKEN=...)." % (exc.code, url)
            )
        if exc.code == 404:
            fail("в зеркале нет %s (404). Ветка Debian — LQRAG_DEB_SUITE." % url)
        fail("зеркало ответило %d на %s" % (exc.code, url))
    except urllib.error.URLError as exc:
        fail("не удалось скачать %s: %s. Нужен доступ к корпоративной сети." % (url, exc.reason))


def find_keyring():
    for pattern in KEYRING_GLOBS:
        for path in glob.glob(pattern):
            if os.path.isfile(path):
                return path
    return None


def verify_release(release, signature):
    """Проверить подпись Release. True — проверено, None — нечем проверить."""
    keyring = find_keyring()
    verifier = shutil.which("gpgv") or shutil.which("sqv")
    if not keyring or not verifier:
        return None
    tmp = tempfile.mkdtemp(prefix="lqrag-release-")
    try:
        rel_path = os.path.join(tmp, "Release")
        sig_path = os.path.join(tmp, "Release.gpg")
        with open(rel_path, "wb") as handle:
            handle.write(release)
        with open(sig_path, "wb") as handle:
            handle.write(signature)
        # И у gpgv, и у sqv порядок один: --keyring КЛЮЧИ ПОДПИСЬ ФАЙЛ.
        result = subprocess.run(
            [verifier, "--keyring", keyring, sig_path, rel_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            fail(
                "подпись Release не прошла проверку (%s, %s): %s"
                % (os.path.basename(verifier), keyring, result.stderr.decode(errors="replace").strip())
            )
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def release_sha256(release_text, relpath):
    in_sha = False
    for line in release_text.splitlines():
        if not line.startswith(" "):
            in_sha = line.strip() == "SHA256:"
            continue
        if in_sha:
            parts = line.split()
            if len(parts) == 3 and parts[2] == relpath:
                return parts[0]
    return None


def parse_packages(text, wanted):
    """Package → (Version, Filename, SHA256) для нужных пакетов."""
    found = {}
    for stanza in text.split("\n\n"):
        fields = {}
        for line in stanza.splitlines():
            if line and not line[0].isspace() and ":" in line:
                key, value = line.split(":", 1)
                fields[key] = value.strip()
        name = fields.get("Package")
        if name in wanted and "Filename" in fields and "SHA256" in fields:
            found[name] = (fields.get("Version", "?"), fields["Filename"], fields["SHA256"])
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest", required=True, help="куда положить .deb")
    parser.add_argument("packages", nargs="+")
    args = parser.parse_args()

    mirror = os.environ.get("LQRAG_DEB_MIRROR", DEFAULT_MIRROR).rstrip("/")
    suite = os.environ.get("LQRAG_DEB_SUITE") or detect_suite()
    arch = detect_arch()
    token = os.environ.get("OSC_TOKEN", "")
    print("fetch_build_tools: зеркало %s, ветка %s, архитектура %s" % (mirror, suite, arch))

    base = "%s/dists/%s" % (mirror, suite)
    release = fetch(base + "/Release", token)
    signature = fetch(base + "/Release.gpg", token)
    verified = verify_release(release, signature)
    if verified:
        print("fetch_build_tools: подпись Release проверена ключом архива Debian")
    else:
        print(
            "fetch_build_tools: ВНИМАНИЕ: нечем проверить подпись Release (нет gpgv/sqv или "
            "ключа Debian) — полагаемся на HTTPS и контрольные суммы"
        )

    relpath = "main/binary-%s/Packages.xz" % arch
    expected = release_sha256(release.decode("utf-8", errors="replace"), relpath)
    if not expected:
        fail("в Release нет SHA256 для %s" % relpath)
    packages_xz = fetch("%s/%s" % (base, relpath), token)
    if hashlib.sha256(packages_xz).hexdigest() != expected:
        fail("контрольная сумма %s не совпала с Release" % relpath)
    index = parse_packages(lzma.decompress(packages_xz).decode("utf-8", errors="replace"), set(args.packages))

    missing = [name for name in args.packages if name not in index]
    if missing:
        fail("в ветке %s нет пакетов: %s" % (suite, ", ".join(missing)))

    os.makedirs(args.dest, exist_ok=True)
    for name in args.packages:
        version, filename, sha = index[name]
        data = fetch("%s/%s" % (mirror, filename), token)
        if hashlib.sha256(data).hexdigest() != sha:
            fail("контрольная сумма %s не совпала с индексом Packages" % filename)
        target = os.path.join(args.dest, os.path.basename(filename))
        with open(target, "wb") as handle:
            handle.write(data)
        print("fetch_build_tools: %s %s — скачан и проверен" % (name, version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
