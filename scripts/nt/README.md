# Сценарии нагрузочного тестирования индексатора

> Порядок запуска для передачи тестировщику — в `docs/HANDOVER.md`.

Инструментарий НТ (Иван Черепанов, SO2D.Atlant, 28.08.2026), перенесённый в
репозиторий: приёмка после правок индексатора должна ездить вместе с кодом.

Запускать **от пользователя, без sudo** — индексатору нужен домашний каталог
пользователя (под root `HOME=/root`, и почтовый корень не находится).
Исключение — `test_sber_indexer.sh`: ему root нужен для пассивного монитора.

Ни один путь стенда больше не захардкожен — всё задаётся переменными
окружения (значения ниже — умолчания).

| Переменная | Умолчание | Что это |
|---|---|---|
| `INDEXER_URL` | `http://127.0.0.1:8000` | адрес индексатора |
| `INDEXER_DIR` | `$PWD` | каталог `lib-local-rag` (где `main.py`) |
| `WATCHED_DIR` / `DIR` | `$HOME/index_test_files` | индексируемая тестовая папка |
| `MONITOR_DIR` | `$HOME/datahound` | каталог монитора (`cli/main.py`) |
| `LOADTEST` | `scripts/nt/sber_indexer_loadtest.py` | движок нагрузки |

---

## Что чем проверять

### `hunt_crash.py` — детектор `assert_db_state`

Сам поднимает индексатор с чистого `~/.files-search`, приводит в нужное
состояние, шлёт `POST /config` и смотрит, выжил ли процесс. Восемь гипотез,
каждая — отдельный цикл с чистым стартом.

```bash
cd <каталог lib-local-rag> && source .venv/bin/activate

python3 scripts/nt/hunt_crash.py --repeat 5                        # 40 прогонов, ~1 ч
python3 scripts/nt/hunt_crash.py --only search_then_cfg --repeat 20
python3 scripts/nt/hunt_crash.py --only double_config   --repeat 20
python3 scripts/nt/hunt_crash.py --only active_indexing --repeat 20
```

Флаги: `--src` (каталог с `main.py`, по умолчанию текущий), `--work`
(рабочий каталог, по умолчанию `~/hunt_work`), `--files N`, `--url`,
`--keep-logs` (не удалять логи выживших прогонов).

**Критерий приёмки: 0 падений.** До правок было 5 из 50 (10%): серия A —
4/40, серия B — 1/10.

### `mcp_overhead.py` — цена MCP-моста

Гоняет одни и те же запросы напрямую в `POST /api/v1/search` и через инструмент
`local_search`, считает p50/p95/p99 для обеих фаз и разницу между ними. Третья
фаза шлёт пачку запросов не дожидаясь ответов — мост читает stdin по одной
строке, и фаза показывает, во что это обходится.

```bash
python3 scripts/nt/mcp_overhead.py --url http://127.0.0.1:8000 --repeat 30 --burst 8
```

Индексатор должен быть поднят и уже проиндексирован. Отчёт — в
`scripts/nt/mcp_overhead.json`. Критерий: накладные расходы моста по p50 в
пределах единиц миллисекунд, `errors = 0`.

Отдельный скрипт, а не фаза в `sber_indexer_loadtest.py`: тот держится
байт-в-байт с версией из пакета тестировщика.

### `check_indexer_fix.sh` — проверка за минуту

Создаёт файлы с уникальным маркером и ищет его через `/search`.

```bash
DIR=~/index_test_files ./scripts/nt/check_indexer_fix.sh
```

Критерий: маркер найден.

### `smoke_user_path.sh` — смоук ~6 минут

Сжатая фаза ПОЛЬЗОВАТЕЛЬ: индексация корпуса, поиск параллельно с ней,
доиндексация новых файлов, контроль живости.

```bash
FILES=150 MINUTES=6 DIR=~/index_test_files ./scripts/nt/smoke_user_path.sh
```

Критерий: вердикт «РАБОТАЕТ».

### `test_sber_indexer.sh` + `sber_indexer_loadtest.py` — полный протокол НТ

Четыре фазы по часу с пассивным монитором.

```bash
INDEXER_DIR=~/lib-local-rag WATCHED_DIR=~/index_test_files \
MONITOR_DIR=~/datahound sudo -E ./scripts/nt/test_sber_indexer.sh
```

Критерии: все фазы завершены, `phases.log` без обрывов, `error_rate = 0`,
а в `summary.json` у маркера `bulk_index_end` поле `indexed` содержит
реальное число файлов (раньше там был `0` даже на успешных прогонах).

---

## Сквозные критерии по логам

После правок ни в одном логе прогона не должно быть:

* `assert_db_state` / `lsm_main.c` — аварийное завершение индексатора;
* `task_done() called too many times` — порча очереди сканера;
* `Connection refused` в `timeline.log` после успешного старта.

И наоборот, должно быть: за каждой строкой `restart watcher` следует
`count total items for files` — признак, что перезапущенная индексация
дожила до работы.

С включённым `faulthandler` (`main.py`) любое падение в нативной библиотеке
теперь печатает стек всех потоков Python — раньше в логе оставалась одна
строка ассерта без единого следа вызывающего кода.

---

## Соответствие «дефект → тест»

| Дефект | pytest | Сценарий здесь |
|---|---|---|
| Доступ к `lsm_db` из нескольких пулов потоков | `tests/test_lsm_concurrency.py` | `hunt_crash.py --repeat 5` |
| Порядок шагов в `POST /config` | `tests/test_config_reload.py` | `hunt_crash.py --only double_config` |
| Отмена задачи не ждёт LSM-поток | `tests/test_config_reload.py` | `hunt_crash.py --only active_indexing` |
| `rmtree` живой очереди, висящий `iter()` | `tests/test_scanners.py` | `hunt_crash.py --only active_indexing`, `smoke_user_path.sh` |
| `/status` теряет итог прохода | `tests/test_status_reporting.py` | `test_sber_indexer.sh` → `summary.json` |
