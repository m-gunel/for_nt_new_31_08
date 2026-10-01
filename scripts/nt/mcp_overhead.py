#!/usr/bin/env python3
"""Накладные расходы MCP-моста поверх поиска индексатора.

Отвечает на два вопроса, которых прошлое НТ не касалось, потому что моста в
той версии не было:

  1. Сколько стоит сам мост. Одни и те же запросы гоняются напрямую в
     POST /api/v1/search и через инструмент local_search; разница p50/p95 —
     цена трансляции.
  2. Что происходит при параллельных вызовах. Мост обрабатывает stdin строго
     по одной строке, поэтому пачка запросов от агента упирается в него, а не
     в индексатор. Фаза concurrency показывает это числом.

Отдельным скриптом, а не фазой в sber_indexer_loadtest.py: тот приехал из
пакета тестировщика и держится байт-в-байт, чтобы прогоны со стенда и из
репозитория можно было сверять.

Только stdlib. Запуск (индексатор уже должен быть поднят и проиндексирован):

    python3 scripts/nt/mcp_overhead.py --url http://127.0.0.1:8000 --repeat 30
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "mcp_server.py"

DEFAULT_QUERIES = [
    "договор поставки",
    "регламент по закупкам",
    "отчёт о статусе проекта",
    "протокол совещания",
    "бюджет на квартал",
]


def percentile(values, share):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))
    return ordered[index]


def summarize(name, latencies, errors):
    return {
        "phase": name,
        "calls": len(latencies) + errors,
        "errors": errors,
        "p50_ms": round(percentile(latencies, 0.50), 1),
        "p95_ms": round(percentile(latencies, 0.95), 1),
        "p99_ms": round(percentile(latencies, 0.99), 1),
        "mean_ms": round(statistics.fmean(latencies), 1) if latencies else 0.0,
    }


def http_search(url, query, limit, timeout):
    body = json.dumps({"query": query, "limit": limit, "query_type": "hybrid"}).encode()
    request = urllib.request.Request(
        url + "/api/v1/search",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class BridgeClient:
    """MCP-мост подпроцессом: пишем в stdin, читаем из stdout."""

    def __init__(self, url, timeout):
        self.proc = subprocess.Popen(
            [sys.executable, str(BRIDGE)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "LQRAG_URL": url, "LQRAG_TIMEOUT": str(timeout)},
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.next_id = 0
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})

    def send(self, method, params):
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        return self.next_id

    def read(self):
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("мост закрыл stdout")
        return json.loads(line)

    def request(self, method, params):
        self.send(method, params)
        return self.read()

    def search(self, query, limit):
        return self.request(
            "tools/call",
            {"name": "local_search", "arguments": {"query": query, "limit": limit}},
        )

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def phase_http(url, queries, repeat, limit, timeout):
    latencies, errors = [], 0
    for index in range(repeat):
        query = queries[index % len(queries)]
        started = time.perf_counter()
        try:
            http_search(url, query, limit, timeout)
        except (urllib.error.URLError, OSError, ValueError):
            errors += 1
            continue
        latencies.append((time.perf_counter() - started) * 1000)
    return summarize("http_direct", latencies, errors)


def phase_mcp(url, queries, repeat, limit, timeout):
    client = BridgeClient(url, timeout)
    latencies, errors = [], 0
    try:
        for index in range(repeat):
            query = queries[index % len(queries)]
            started = time.perf_counter()
            response = client.search(query, limit)
            latencies.append((time.perf_counter() - started) * 1000)
            if response.get("result", {}).get("isError") or "error" in response:
                errors += 1
    finally:
        client.close()
    return summarize("mcp_sequential", latencies, errors)


def phase_concurrency(url, queries, burst, limit, timeout):
    """Пачка запросов без ожидания ответов: мост отвечает по одному."""
    client = BridgeClient(url, timeout)
    try:
        started = time.perf_counter()
        for index in range(burst):
            client.send(
                "tools/call",
                {
                    "name": "local_search",
                    "arguments": {"query": queries[index % len(queries)], "limit": limit},
                },
            )
        latencies, errors = [], 0
        for _ in range(burst):
            response = client.read()
            latencies.append((time.perf_counter() - started) * 1000)
            if response.get("result", {}).get("isError") or "error" in response:
                errors += 1
        total_ms = (time.perf_counter() - started) * 1000
    finally:
        client.close()
    result = summarize("mcp_burst", latencies, errors)
    result["burst"] = burst
    result["wall_ms"] = round(total_ms, 1)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--repeat", type=int, default=30, help="запросов на фазу")
    parser.add_argument("--burst", type=int, default=8, help="размер пачки в фазе concurrency")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--report", default="scripts/nt/mcp_overhead.json")
    args = parser.parse_args()

    try:
        with urllib.request.urlopen(args.url + "/api/v1/health", timeout=5) as response:
            json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print("Индексатор недоступен по %s: %s" % (args.url, exc), file=sys.stderr)
        return 1

    # Прогрев: первый запрос платит за создание infer-request'а модели, и без
    # прогрева он один задирает p95 обеих фаз.
    try:
        http_search(args.url, DEFAULT_QUERIES[0], args.limit, args.timeout)
    except Exception:
        pass

    phases = [
        phase_http(args.url, DEFAULT_QUERIES, args.repeat, args.limit, args.timeout),
        phase_mcp(args.url, DEFAULT_QUERIES, args.repeat, args.limit, args.timeout),
        phase_concurrency(args.url, DEFAULT_QUERIES, args.burst, args.limit, args.timeout),
    ]
    by_name = {phase["phase"]: phase for phase in phases}
    overhead = round(by_name["mcp_sequential"]["p50_ms"] - by_name["http_direct"]["p50_ms"], 1)

    print("%-16s %7s %8s %8s %8s %7s" % ("фаза", "вызовов", "p50 мс", "p95 мс", "p99 мс", "ошибок"))
    for phase in phases:
        print(
            "%-16s %7d %8.1f %8.1f %8.1f %7d"
            % (
                phase["phase"],
                phase["calls"],
                phase["p50_ms"],
                phase["p95_ms"],
                phase["p99_ms"],
                phase["errors"],
            )
        )
    print("\nНакладные расходы моста по p50: %+.1f мс" % overhead)
    burst = by_name["mcp_burst"]
    print(
        "Пачка из %d запросов обработана за %.1f мс (последний ответ), "
        "то есть мост отвечает последовательно."
        % (burst["burst"], burst["wall_ms"])
    )

    report = {
        "url": args.url,
        "repeat": args.repeat,
        "phases": phases,
        "mcp_overhead_p50_ms": overhead,
    }
    Path(args.report).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("Отчёт: %s" % args.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
