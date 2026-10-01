#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hunt_crash.py — автоматический поиск условий, при которых падает индексатор.

ЗАДАЧА
    Assertion `(pDb->pCsr!=0||pDb->nTransOpen>0)==(pDb->iReader>=0||pDb->bRoTrans)'
    failed  (src/sqlite/ext/lsm1/lsm_main.c:43)

    Синтетические тесты (голый persistqueue / python-lsm-db) assert НЕ вызывают.
    Значит нужен живой индексатор. Этот скрипт сам поднимает main.py, приводит
    его в нужное состояние, шлёт POST /config и смотрит, выжил ли процесс.

ЧТО ПРОВЕРЯЕТ (гипотезы)
    H1 empty_queue      папка ПУСТА -> `nothing new` -> POST /config
    H2 indexed_queue    всё уже проиндексировано -> `nothing new` -> POST /config
    H3 active_indexing  индексация ИДЁТ (contrl: ожидаем, что НЕ упадёт)
    H4 double_config    два POST /config подряд на пустой очереди
    H5 config_same_dir  POST /config на ТУ ЖЕ папку (без смены пути)
    H6 config_new_dir   POST /config на ДРУГУЮ папку
    H7 idle_delay       пустая очередь + пауза N секунд перед POST /config
    H8 search_then_cfg  поиск (открывает читателей) -> POST /config

    Каждая гипотеза = отдельный цикл: чистый старт индексатора -> подготовка
    состояния -> POST /config -> проверка живости. Результат: УПАЛ / выжил.

ЗАПУСК (от пользователя, БЕЗ sudo, из каталога lib-local-rag)
    cd lib-local-rag && source .venv/bin/activate
    python3 hunt_crash.py

    python3 hunt_crash.py --only empty_queue     # одна гипотеза
    python3 hunt_crash.py --repeat 3             # каждую по 3 раза (баг плавающий)
    python3 hunt_crash.py --keep-logs            # не удалять логи выживших

РЕЗУЛЬТАТ
    Таблица «гипотеза -> упал/выжил», логи каждого прогона в hunt_<дата>/,
    и готовая формулировка условий воспроизведения для баг-репорта.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import signal
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ── оформление ────────────────────────────────────────────────────────────────
T = sys.stdout.isatty()
R = "\033[0;31m" if T else ""
G = "\033[0;32m" if T else ""
Y = "\033[1;33m" if T else ""
C = "\033[0;36m" if T else ""
B = "\033[1m" if T else ""
D = "\033[2m" if T else ""
N = "\033[0m" if T else ""

LOG_FILE: Path | None = None


def log(msg: str = "") -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if LOG_FILE is not None:
        try:
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def ok(m): log(f"  {G}OK{N} {m}")
def bad(m): log(f"  {R}!!{N} {m}")
def inf(m): log(f"  {C}->{N} {m}")
def hdr(m): log(""); log(f"{B}{m}{N}")


# ── HTTP ──────────────────────────────────────────────────────────────────────
class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def _req(self, method: str, path: str, body: dict | None = None,
             timeout: float = 20.0) -> tuple[int, str]:
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")[:400]
        except Exception as e:
            return 0, str(e)

    def health(self) -> bool:
        return self._req("GET", "/api/v1/health", timeout=5)[0] == 200

    def status(self) -> dict:
        code, body = self._req("GET", "/api/v1/status", timeout=5)
        if code != 200:
            return {}
        try:
            return json.loads(body)
        except Exception:
            return {}

    def config(self, folder: str, email_folder: str = "") -> tuple[int, str]:
        return self._req("POST", "/api/v1/config", {
            "folders": [folder],
            "email": {"enabled": False, "path_to_email_folder": email_folder,
                      "index_last_n_days": 90},
            "sberchat": {"enabled": False},
        })

    def search(self, q: str = "договор отчёт") -> tuple[int, str]:
        return self._req("POST", "/api/v1/search",
                         {"query": q, "limit": 10, "query_type": "hybrid"})


# ── генерация корпуса ─────────────────────────────────────────────────────────
WORDS = ("договор акт отчёт заявка клиент платёж счёт система сервер модуль "
         "конфигурация процесс память ядро планировщик нагрузка индексация "
         "документ файл поиск запрос результат метрика анализ оптимизация").split()


def _paras(rnd: random.Random, k: int) -> list[str]:
    return [(" ".join(rnd.choice(WORDS) for _ in range(45)) + ".").capitalize()
            for _ in range(k)]


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def make_docx(path: Path, paras: list[str]) -> None:
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{_esc(p)}</w:t></w:r></w:p>'
                   for p in paras)
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f'<w:body>{body}<w:sectPr/></w:body></w:document>')
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)


def fill_dir(d: Path, n: int, seed: int = 1) -> None:
    d.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(seed)
    for i in range(n):
        make_docx(d / f"hunt_{i:05d}.docx", _paras(rnd, rnd.randint(20, 50)))


def clean_dir(d: Path) -> None:
    if d.exists():
        for f in d.glob("hunt_*"):
            try:
                f.unlink()
            except OSError:
                pass


# ── управление индексатором ───────────────────────────────────────────────────
@dataclass
class Indexer:
    src_dir: Path
    store: Path
    log_path: Path
    proc: subprocess.Popen | None = None

    def wipe_store(self) -> None:
        shutil.rmtree(self.store, ignore_errors=True)

    def start(self, api: Api, timeout: int = 120) -> bool:
        """Поднимает main.py в venv, ждёт health."""
        venv = self.src_dir / ".venv" / "bin" / "activate"
        cmd = (f"cd '{self.src_dir}' && "
               + (f"source '{venv}' && " if venv.exists() else "")
               + "python main.py")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        f = self.log_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            ["bash", "-c", cmd], stdout=f, stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,
        )
        waited = 0
        while waited < timeout:
            if api.health():
                return True
            if self.proc.poll() is not None:
                return False
            time.sleep(1)
            waited += 1
        return False

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            self.proc.wait(timeout=15)
        except Exception:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except Exception:
                pass
        self.proc = None

    def died(self) -> bool:
        return self.proc is not None and self.proc.poll() is not None

    def exit_code(self) -> int | None:
        return self.proc.poll() if self.proc else None

    def tail(self, n: int = 12) -> list[str]:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        except OSError:
            return []

    def has_assert(self) -> bool:
        try:
            return "assert_db_state" in self.log_path.read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            return False


# ── результат одной проверки ──────────────────────────────────────────────────
@dataclass
class Result:
    name: str
    desc: str
    expect_crash: bool
    crashed: bool = False
    assert_found: bool = False
    exit_code: int | None = None
    note: str = ""
    log_file: str = ""
    status_before: dict = field(default_factory=dict)


# ── ожидания состояния ────────────────────────────────────────────────────────
def wait_nothing_new(idx: Indexer, api: Api, timeout: int = 90) -> bool:
    """Ждём, пока индексатор скажет `nothing new` (очередь пуста)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if idx.died():
            return False
        try:
            txt = idx.log_path.read_text(encoding="utf-8", errors="replace")
            if "nothing new for" in txt:
                return True
        except OSError:
            pass
        time.sleep(1)
    return False


def wait_indexing_active(api: Api, timeout: int = 60) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = api.status()
        if st.get("is_indexing") is True:
            return True
        time.sleep(0.5)
    return False


def wait_indexing_done(api: Api, timeout: int = 900) -> bool:
    t0 = time.time()
    seen = False
    while time.time() - t0 < timeout:
        st = api.status()
        act = st.get("is_indexing")
        if act is True:
            seen = True
        if act is False and (seen or time.time() - t0 > 10):
            return True
        time.sleep(2)
    return False


# ── сами гипотезы ─────────────────────────────────────────────────────────────
HYPOTHESES: dict[str, tuple[str, bool]] = {
    "empty_queue":    ("папка ПУСТА -> nothing new -> POST /config", True),
    "indexed_queue":  ("всё проиндексировано -> nothing new -> POST /config", True),
    "active_indexing": ("индексация ИДЁТ -> POST /config (контроль)", False),
    "double_config":  ("два POST /config подряд на пустой очереди", True),
    "config_same_dir": ("POST /config на ту же папку", True),
    "config_new_dir": ("POST /config на другую папку", True),
    "idle_delay":     ("пустая очередь + пауза 60с -> POST /config", True),
    "search_then_cfg": ("поиск -> POST /config на пустой очереди", True),
}


def run_hypothesis(name: str, out_dir: Path, work: Path, src_dir: Path,
                   api: Api, store: Path, run_no: int, files: int,
                   idle_sec: int) -> Result:
    desc, expect = HYPOTHESES[name]
    res = Result(name=name, desc=desc, expect_crash=expect)
    log_path = out_dir / f"{name}_run{run_no}.log"
    res.log_file = log_path.name

    dir_a = work / "dir_a"
    dir_b = work / "dir_b"
    for d in (dir_a, dir_b):
        clean_dir(d)
        d.mkdir(parents=True, exist_ok=True)

    idx = Indexer(src_dir=src_dir, store=store, log_path=log_path)
    idx.wipe_store()

    # ── подготовка состояния под конкретную гипотезу ──────────────────────────
    if name in ("indexed_queue", "active_indexing", "search_then_cfg"):
        fill_dir(dir_a, files)
    if name == "config_new_dir":
        fill_dir(dir_b, max(5, files // 10))

    inf(f"старт индексатора (папка: {dir_a.name}, файлов: "
        f"{len(list(dir_a.glob('hunt_*')))})")
    if not idx.start(api, timeout=120):
        res.note = "индексатор не поднялся"
        bad(res.note)
        for l in idx.tail(8):
            log(f"      {D}{l}{N}")
        idx.stop()
        return res

    # Нацелить на dir_a. ВАЖНО: этот config сам может уронить сервис — тогда
    # условие уже найдено, и это надо зафиксировать отдельно от целевого шага.
    code, _ = api.config(str(dir_a))
    time.sleep(3)
    if idx.died() or not api.health():
        res.crashed = True
        res.assert_found = idx.has_assert()
        res.exit_code = idx.exit_code()
        n_files = len(list(dir_a.glob("hunt_*")))
        res.note = (f"упал на ПЕРВОМ POST /config (файлов в папке: {n_files})")
        bad(res.note)
        for l in idx.tail(6):
            log(f"      {D}{l}{N}")
        idx.stop()
        return res
    if code != 200:
        inf(f"первичный config -> {code}")

    # довести до нужного состояния
    if name == "active_indexing":
        if not wait_indexing_active(api, 60):
            res.note = "индексация не стартовала — гипотеза не проверена"
            inf(res.note)
    elif name == "indexed_queue":
        wait_indexing_done(api, 900)
        wait_nothing_new(idx, api, 60)
    elif name == "search_then_cfg":
        wait_indexing_done(api, 900)
        for _ in range(5):
            api.search()
            time.sleep(0.3)
    elif name == "idle_delay":
        wait_nothing_new(idx, api, 90)
        inf(f"пауза {idle_sec}с в простое...")
        time.sleep(idle_sec)
    else:
        wait_nothing_new(idx, api, 90)

    res.status_before = api.status()
    inf(f"состояние перед config: {json.dumps(res.status_before, ensure_ascii=False)[:120]}")

    if idx.died():
        res.crashed = True
        res.assert_found = idx.has_assert()
        res.exit_code = idx.exit_code()
        res.note = "упал ДО POST /config"
        bad(res.note)
        idx.stop()
        return res

    # ── целевое действие ──────────────────────────────────────────────────────
    target = str(dir_b) if name == "config_new_dir" else str(dir_a)
    inf(f"POST /config -> {Path(target).name}")
    code, _ = api.config(target)
    inf(f"ответ: {code if code else 'нет ответа (процесс умер?)'}")

    if name == "double_config":
        time.sleep(1)
        inf("второй POST /config")
        code2, _ = api.config(target)
        inf(f"ответ: {code2 if code2 else 'нет ответа'}")

    # ── вердикт ───────────────────────────────────────────────────────────────
    time.sleep(4)
    alive = api.health()
    res.assert_found = idx.has_assert()
    res.exit_code = idx.exit_code()
    res.crashed = (not alive) or idx.died() or res.assert_found

    if res.crashed:
        bad(f"ИНДЕКСАТОР УПАЛ (assert в логе: {'да' if res.assert_found else 'нет'})")
        for l in idx.tail(6):
            log(f"      {D}{l}{N}")
    else:
        ok("индексатор жив")

    idx.stop()
    return res


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    global LOG_FILE

    ap = argparse.ArgumentParser(
        description="Автопоиск условий краша индексатора (assert_db_state)")
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--src", default=".",
                    help="каталог lib-local-rag (где main.py)")
    ap.add_argument("--work", default=None,
                    help="рабочий каталог для тестовых папок (по умолчанию ~/hunt_work)")
    ap.add_argument("--files", type=int, default=120,
                    help="файлов в корпусе для гипотез с индексацией")
    ap.add_argument("--idle-sec", type=int, default=60,
                    help="пауза для гипотезы idle_delay")
    ap.add_argument("--repeat", type=int, default=1,
                    help="повторов каждой гипотезы (баг плавающий)")
    ap.add_argument("--only", choices=list(HYPOTHESES))
    ap.add_argument("--keep-logs", action="store_true")
    args = ap.parse_args()

    src_dir = Path(args.src).resolve()
    if not (src_dir / "main.py").exists():
        print(f"{R}Не найден main.py в {src_dir}. Укажи --src /путь/к/lib-local-rag{N}")
        return 2

    home = Path(os.path.expanduser("~"))
    work = Path(args.work).resolve() if args.work else home / "hunt_work"
    work.mkdir(parents=True, exist_ok=True)
    store = home / ".files-search"

    out_dir = Path.cwd() / f"hunt_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    LOG_FILE = out_dir / "hunt.log"

    api = Api(args.url)

    log(f"{B}Автопоиск условий краша индексатора{N}")
    log(f"  исходники:  {src_dir}")
    log(f"  рабочая:    {work}")
    log(f"  хранилище:  {store}")
    log(f"  результаты: {out_dir}")
    log(f"  повторов:   {args.repeat}")

    if os.geteuid() == 0:
        log(f"  {Y}ВНИМАНИЕ: запущено под root — индексатор должен работать")
        log(f"  от пользователя. Запусти без sudo.{N}")

    # если индексатор уже запущен — гасим, он будет мешать
    try:
        subprocess.run(["pkill", "-f", "lib-local-rag.*main.py"],
                       capture_output=True, timeout=10)
        time.sleep(2)
    except Exception:
        pass

    names = [args.only] if args.only else list(HYPOTHESES)
    results: list[Result] = []

    for name in names:
        desc, expect = HYPOTHESES[name]
        for run_no in range(1, args.repeat + 1):
            suffix = f" (прогон {run_no}/{args.repeat})" if args.repeat > 1 else ""
            hdr(f"Гипотеза '{name}'{suffix}: {desc}")
            inf("ожидается: " + ("КРАШ" if expect else "индексатор выживет"))
            try:
                r = run_hypothesis(name, out_dir, work, src_dir, api, store,
                                   run_no, args.files, args.idle_sec)
            except KeyboardInterrupt:
                log("прервано пользователем")
                raise
            except Exception as e:
                r = Result(name=name, desc=desc, expect_crash=expect,
                           note=f"ошибка скрипта: {e}")
                bad(r.note)
            results.append(r)
            # НЕ прерываем серию: баг плавающий, нужна частота падений,
            # иначе один случайный краш выглядит как «условие найдено».

    # ── сводка ────────────────────────────────────────────────────────────────
    hdr("СВОДКА (частота падений)")
    log(f"  {'гипотеза':<18} {'падений':<10} {'частота':<9} assert")
    log("  " + "-" * 56)
    agg: dict[str, list[Result]] = {}
    for r in results:
        agg.setdefault(r.name, []).append(r)
    crashed_names: list[str] = []
    for name, rs in agg.items():
        n_crash = sum(1 for r in rs if r.crashed)
        n_tot = len(rs)
        pct = round(100 * n_crash / n_tot) if n_tot else 0
        col = R if n_crash else G
        asrt = "да" if any(r.assert_found for r in rs) else "-"
        log(f"  {name:<18} {col}{n_crash}/{n_tot:<8}{N} {pct:>3}%      {asrt}")
        if n_crash:
            crashed_names.append(name)

    total_crash = sum(1 for r in results if r.crashed)
    total_runs = len(results)
    if total_runs:
        log("")
        log(f"  ИТОГО: {total_crash} падений из {total_runs} прогонов "
            f"({round(100*total_crash/total_runs)}%)")
        if 0 < total_crash < total_runs:
            log(f"  {Y}Баг ПЛАВАЮЩИЙ: падает не каждый раз при одинаковых условиях.{N}")
            log(f"  {Y}Не делай вывод «падает только при X» по одному прогону —{N}")
            log(f"  {Y}многие гипотезы отличаются лишь подготовкой, а действие одно.{N}")

    log("")
    log(f"{B}=============================================={N}")
    if crashed_names:
        log(f"  {R}{B}БАГ ВОСПРОИЗВЕДЁН{N}")
        log(f"  Условия, при которых падает: {', '.join(crashed_names)}")
        log("")
        log(f"  {B}Общее во ВСЕХ падениях:{N}")
        log("   POST /api/v1/config -> stop watcher -> clear index queue ->")
        log("   restart watcher -> assert_db_state (процесс убит).")
        log("")
        log(f"  {B}Сценарии, где наблюдалось падение:{N}")
        for n in crashed_names:
            rs = agg[n]
            log(f"   * {HYPOTHESES[n][0]} — {sum(1 for r in rs if r.crashed)}/{len(rs)}")
        never = [n for n in agg if n not in crashed_names]
        if never:
            log(f"  {B}Не падало (в этой серии):{N} {', '.join(sorted(never))}")
            log(f"  {D}  при малом числе повторов это может быть случайностью —{N}")
            log(f"  {D}  для вывода нужен --repeat 5 и больше{N}")
        log("")
        log(f"  Логи индексатора по каждому прогону: {out_dir}")
        rc = 0
    else:
        log(f"  {Y}{B}НЕ ВОСПРОИЗВЕЛОСЬ{N} ни в одной гипотезе.")
        log("  Возможные причины:")
        log("   * баг уже исправлен в этой сборке;")
        log("   * нужен другой сценарий — увеличь --repeat и --files;")
        log("   * влияет длительность работы: попробуй --idle-sec 3600.")
        rc = 1
    log(f"{B}=============================================={N}")

    # уборка
    clean_dir(work / "dir_a")
    clean_dir(work / "dir_b")
    if not args.keep_logs:
        for r in results:
            if not r.crashed:
                p = out_dir / r.log_file
                if p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass
        inf("логи выживших прогонов удалены (--keep-logs чтобы оставить)")

    log(f"  Общий лог: {LOG_FILE}")
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано.")
        sys.exit(130)
