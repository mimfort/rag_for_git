# PRI-297. Проверка свежести сводок на живом индексе

## Условия

21 ноября 2026 года сравнили `MCPReviewService.get_subsystem_summaries` и адаптер `_TaskContextDeps.subsystems`, который наполняет секцию `subsystems` в `prepare_task_context`. Базовый код взят из `bfcefdf96b9ea67beea766aa75adf0acbbcbcdc2`, исправленный из `c935a0a1af17a6956964baf6654502f5b6b57ea1`. Оба варианта читали один индекс ParadeDB для `mimfort/rag_for_git`, ветка `dev`, SHA `bfcefdf96b9ea67beea766aa75adf0acbbcbcdc2`.

В индексе было 7 989 чанков, в таблице сводок 40 строк, у всех 40 есть эмбеддинги. Метаданные индекса `indexed_at=2026-11-21T12:16:18.845339+00:00` и SHA-256 всех строк сводок `40a20069d8211e2f23351d28b6f5755052eaa21168030d9f936d03d4588ba9c5` совпали до и после замера. Отпечаток включает ключи кластеров, заголовки, тексты, `source_hash`, время обновления и векторы. Прогон использовал чтение существующих данных. Действующий порог ANN равен 20 и задан в `home:repos/mimfort/rag_for_git.yml`. При 40 сводках запрос с текстом выбирает ANN с обычным `top_k=8`.

Для каждого из трёх запросов один раз получили реальный вектор Voyage `voyage-code-3` размерности 1024. При тайминге подавали обоим вариантам один и тот же сохранённый вектор. Подключения прогрели чтением счётчика сводок и метаданных индекса. Длительность вызова измеряли через `time.perf_counter`. Холодный замер означает первый прямой ANN-вызов после создания экземпляра MCP-сервиса с пустым кэшем структурных хешей. Для повторных замеров сделали по семь пар вызовов на запрос и путь, чередуя порядок вариантов в паре и используя те же экземпляры сервиса.

## Выдача и источники solve-task

| № | Запрос | Кластеры обычной ANN-выдачи сверху вниз |
|---|---|---|
| 1 | `task context and multiquery ranking` | `reviewer/retrieval`, `tests/retrieval`, `reviewer/tasks`, `tests/index`, `reviewer/policy`, `tests/skills`, `tests/tasks`, `reviewer/entrypoints` |
| 2 | `paradedb neo4j stores index chunk storage` | `tests/index`, `reviewer/index`, `tests/graph`, `reviewer/graph`, `reviewer/entrypoints`, `reviewer/tasks`, `tests/integration`, `reviewer/services` |
| 3 | `verify task brief with evidence and guard plugin` | `plugin/hooks`, `tests/hooks`, `tests/skills`, `tests/install`, `tests/policy`, `reviewer`, `tests/entrypoints`, `scripts` |

Распределение дано в порядке `stale=false / true / null`.

| Путь | До | После |
|---|---:|---:|
| Прямой ANN, каждый из трёх запросов | 0 / 0 / 8 | 0 / 8 / 0 |
| Адаптер solve-task, каждый из трёх запросов | 0 / 0 / 8 | 0 / 0 / 0 |
| Полная выдача без запроса | 5 / 35 / 0 | 5 / 35 / 0 |

Прямой ANN сохранил перечисленные ключи и порядок. До исправления адаптер solve-task возвращал все восемь текстов для каждого запроса. После исправления он исключил все восемь и передал `note` с числом устаревших кандидатов. В диагностике прямого ANN и адаптера также указаны два текущих кластера без сохранённых строк, `reviewer/bugreport` и `reviewer/metrics`. Все пять сохранённых свежих сводок находились ниже обычной отсечки ANN.

Для проверки порядка повторили запросы с явным `top_k=40`. В каждой паре совпали все 40 ключей, пять свежих строк вышли вперёд и 35 устаревших сохранили относительный порядок по близости. Оценки совпали с полной выдачей. Стрелки показывают исходный и новый ранги по каждому запросу в порядке новой выдачи.

| № | Ранги пяти свежих сводок |
|---|---|
| 1 | `reviewer/agent` 10 → 1, `reviewer/launcher` 15 → 2, `reviewer/tools` 30 → 3, `reviewer/vcs` 35 → 4, `reviewer/llm` 37 → 5 |
| 2 | `reviewer/tools` 15 → 1, `reviewer/agent` 16 → 2, `reviewer/vcs` 25 → 3, `reviewer/launcher` 26 → 4, `reviewer/llm` 29 → 5 |
| 3 | `reviewer/agent` 16 → 1, `reviewer/launcher` 30 → 2, `reviewer/vcs` 34 → 3, `reviewer/tools` 36 → 4, `reviewer/llm` 37 → 5 |

Модульный тест проверяет смешанный случай с обычным ANN. Он сопоставляет свежие и устаревшие строки в выдаче и проверяет упоминание отсутствующей сводки в диагностике. Там же проверены пустой результат ANN, недоступность каталога и передача сводок с неизвестной свежестью. [Тесты сводок](../tests/mcp/test_subsystem_summaries.py)

## Задержка и обращения к данным

Первый прямой ANN-вызов для запроса № 1 занял **137,409 → 1 732,503 мс**. В исправленной версии в эту задержку входит построение снимка. Повторные вызовы уложились в 30-секундный срок жизни снимка. Ниже приведены медианы семи повторов и диапазоны от минимума до максимума в миллисекундах.

| № | Путь | До, мс | После, мс |
|---|---|---:|---:|
| 1 | Прямой ANN | 81,580 (76,980–103,873) | 85,465 (79,689–111,503) |
| 1 | Адаптер solve-task | 83,913 (74,061–112,313) | 78,808 (74,797–99,690) |
| 2 | Прямой ANN | 72,465 (67,655–144,250) | 76,908 (70,975–159,009) |
| 2 | Адаптер solve-task | 83,898 (73,911–125,179) | 89,861 (78,604–113,761) |
| 3 | Прямой ANN | 76,491 (69,851–83,495) | 75,815 (71,861–86,813) |
| 3 | Адаптер solve-task | 90,933 (74,546–175,717) | 97,446 (78,543–177,595) |

Изменение медиан повторных вызовов составило от −5,105 до +6,513 мс по шести сравнениям. Диапазоны повторов пересекаются во всех шести парах. Однократный вызов полной выдачи после серии ANN занял 1 018,788 → 87,374 мс. Порядок и оценки всех 40 строк полной выдачи совпали.

Счётчики ниже показывают вызовы методов за один запрос. Базовый и исправленный ANN на обоих путях по одному разу вызвали `store.get_repo_clone`, `summary_store.count_summaries`, `embedder.embed_query` и `summary_store.search_summaries`. `embed_query` в прогоне возвращал сохранённый вектор из памяти.

| Дополнительные вызовы | ANN до | ANN после, первый | ANN после, повтор | Полная до | Полная после, повтор |
|---|---:|---:|---:|---:|---:|
| `summary_store.get_summaries` | 0 | 0 | 0 | 1 | 1 |
| `summary_store.get_source_hashes` | 0 | 1 | 1 | 0 | 0 |
| `store.get_index_meta_row` | 0 | 2 | 1 | 0 | 1 |
| `store.list_base_members` | 0 | 1 | 0 | 1 | 0 |

В полном пути также был один вызов `store.get_repo_clone` в каждом варианте. При первом ANN-вызове исправленный сервис читает состав ветки для вычисления структурных хешей и проверяет метаданные до и после. При повторном обращении он сверяет метаданные и использует тот же снимок. Отдельное чтение каталога `get_source_hashes` даёт ключи сохранённых сводок для диагностики отсутствующих строк.

## Воспроизведение

Для повторного прогона нужен доступ к тому же снимку ParadeDB и те же правила кластеризации. Приведённый ниже скрипт сохраните как `probe.py` во временном каталоге. На коммите исправления выполните из корня checkout с активированным окружением проекта.

```bash
PRI297_ENV=/path/to/local/.env PYTHONPATH="$PWD" python \
  /path/to/local/probe.py
```

Скрипт читает конфигурацию через `Settings`. При первом запуске нужен доступ к Voyage, чтобы получить и сохранить локальный `query-vectors.json`. Повторный запуск использует этот файл и пишет результаты в `probe-results.json`. Оба варианта получают одни и те же компоненты с живыми хранилищами. Для базовой реализации скрипт загружает `reviewer/mcp/service.py` из Git на `bfcefdf`. Для исправленной импортирует модуль из текущего checkout. В замере заменяется только получение вектора запроса и оборачиваются перечисленные методы источников для подсчёта обращений. Каждая пара сравнивает две реализации на одном векторе. Проверки скрипта сравнивают строки и диагностику между повторами, фиксируют распределение свежести и сверяют состав выдачи, фильтр адаптера и стабильность сортировки. При изменении живого индекса воспроизведение требует восстановить исходный снимок.

<details>
<summary>Скрипт живого прогона</summary>

```python
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from statistics import median
from time import perf_counter
from types import ModuleType

from reviewer.app import build_components
from reviewer.config.settings import Settings
from reviewer.index.refs import base_ref
from reviewer.mcp import service

REPO = "mimfort/rag_for_git"
BRANCH = "dev"
SHA = "bfcefdf96b9ea67beea766aa75adf0acbbcbcdc2"
QUERIES = (
    "task context and multiquery ranking",
    "paradedb neo4j stores index chunk storage",
    "verify task brief with evidence and guard plugin",
)
SETTINGS = Settings(_env_file=os.environ["PRI297_ENV"])
DATA = Path(__file__).parent
VECTORS = DATA / "query-vectors.json"
SOURCE_METHODS = {
    "store": ("get_index_meta_row", "list_base_members", "get_repo_clone"),
    "summary_store": ("count_summaries", "search_summaries", "get_summaries", "get_source_hashes"),
    "embedder": ("embed_query",),
}
BASELINE = "reviewer.mcp._pri297_baseline"


def load_baseline():
    module = ModuleType(BASELINE)
    sys.modules[BASELINE] = module
    source = subprocess.check_output(["git", "show", f"{SHA}:reviewer/mcp/service.py"])
    exec(compile(source, f"{SHA}:reviewer/mcp/service.py", "exec"), module.__dict__)
    return module


def snapshot(components):
    meta = components.store.get_index_meta_row(REPO, base_ref(BRANCH))
    assert meta[0] == SHA, meta
    digester = hashlib.sha256()
    with components.summary_store._connect() as conn:
        # Include even candidates without vectors and compare the actual vectors.
        rows = conn.execute(
            "SELECT cluster_key, title, summary, source_hash, updated_at::text, "
            "embedding::text FROM subsystem_summaries WHERE repo=%s AND branch=%s "
            "ORDER BY cluster_key",
            (REPO, BRANCH),
        ).fetchall()
    digester.update(json.dumps(rows, ensure_ascii=False).encode())
    return {
        "sha": meta[0],
        "indexed_at": meta[1].isoformat(),
        "summaries": len(rows),
        "embedded": sum(row[-1] is not None for row in rows),
        "fingerprint": digester.hexdigest(),
    }


def wrap_sources(components):
    counts = Counter()
    originals = []
    for component_name, methods in SOURCE_METHODS.items():
        component = getattr(components, component_name)
        for method in methods:
            original = getattr(component, method)
            originals.append((component, method, original))

            def counted(*args, target=original, label=f"{component_name}.{method}", **kwargs):
                counts[label] += 1
                return target(*args, **kwargs)

            setattr(component, method, counted)
    return counts, originals


def rows(payload):
    return [(entry["cluster_key"], entry["stale"]) for entry in payload["summaries"]]


def run(components, vectors):
    components.embedder.embed_query = vectors.__getitem__
    before = snapshot(components)
    # Warm connections independently of the summary hash cache.
    components.summary_store.count_summaries(REPO, BRANCH)
    components.store.get_index_meta_row(REPO, base_ref(BRANCH))
    counts, originals = wrap_sources(components)
    baseline = load_baseline()
    instances = {
        name: mod.MCPReviewService(SETTINGS, components)
        for name, mod in [("before", baseline), ("after", service)]
    }
    adapters = {
        name: mod._TaskContextDeps(instances[name], None)
        for name, mod in [("before", baseline), ("after", service)]
    }

    def call(fn):
        counts.clear()
        start = perf_counter()
        result = fn()
        return {
            "ms": round(1000 * (perf_counter() - start), 3),
            "sources": dict(counts),
            "rows": rows(result),
            "note": result.get("note"),
        }

    def direct(name, query):
        return call(lambda: instances[name].get_subsystem_summaries(REPO, BRANCH, query=query))

    threshold = instances["before"]._resolve_summary_topk_threshold(REPO, BRANCH)
    cold = {name: direct(name, QUERIES[0]) for name in instances}
    results = []
    for query_index, query in enumerate(QUERIES):
        record = {}
        for route in ("ann", "solve-task"):
            samples = {name: [] for name in instances}
            for repetition in range(7):
                order = tuple(instances) if (repetition + query_index) % 2 == 0 else tuple(reversed(instances))
                for name in order:
                    measurement = (
                        direct(name, query) if route == "ann"
                        else call(lambda: adapters[name].subsystems(REPO, BRANCH, query))
                    )
                    samples[name].append(measurement)
            record[route] = {}
            for name, measurements in samples.items():
                reference = measurements[0]
                assert all(m["rows"] == reference["rows"] and m["note"] == reference["note"] for m in measurements)
                assert all(m["sources"] == reference["sources"] for m in measurements), measurements
                ms = [m["ms"] for m in measurements]
                record[route][name] = {
                    **reference,
                    "ms": ms,
                    "median_ms": round(median(ms), 3),
                }
        results.append({"query": query, **record})

    full = {
        name: call(lambda: svc.get_subsystem_summaries(REPO, BRANCH))
        for name, svc in instances.items()
    }
    ordering = [
        {
            "query": query,
            **{
                name: call(lambda: svc.get_subsystem_summaries(REPO, BRANCH, query=query, top_k=40))
                for name, svc in instances.items()
            },
        }
        for query in QUERIES
    ]
    for component, method, original in originals:
        setattr(component, method, original)
    assert before == snapshot(components)
    all_rows = dict(full["before"]["rows"])
    assert dict(full["after"]["rows"]) == all_rows
    assert set(all_rows.values()) == {True, False}
    for result in results:
        old_ann = result["ann"]["before"]["rows"]
        ann = result["ann"]["after"]["rows"]
        assert len(old_ann) == len(ann) == 8
        assert set(stale for _, stale in old_ann) == {None}
        assert sorted(ann) == sorted((key, all_rows[key]) for key, _ in old_ann)
        assert result["solve-task"]["before"]["rows"] == old_ann
        assert result["solve-task"]["after"]["rows"] == [row for row in ann if row[1] is False]
    for query in ordering:
        classified = [(key, all_rows[key]) for key, _ in query["before"]["rows"]]
        assert len(classified) == len(all_rows)
        assert query["after"]["rows"] == sorted(classified, key=lambda row: row[1])
    return {"snapshot": before, "threshold": threshold, "cold": cold, "results": results, "full": full, "ordering_top40": ordering}


def main():
    components = build_components(SETTINGS, connect=False)
    try:
        if not VECTORS.exists():
            # Real nearby queries use their Voyage query embeddings. Replay uses
            # exactly these vectors to exclude network jitter from both timings.
            start = perf_counter()
            vectors = {query: components.embedder.embed_query(query) for query in QUERIES}
            VECTORS.write_text(json.dumps(vectors))
            print(f"Received {len(vectors)} Voyage query vectors in {perf_counter() - start:.3f}s")
        if "--vectors-only" not in sys.argv:
            results = run(components, json.loads(VECTORS.read_text()))
            (DATA / "probe-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
            print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        components.close()


if __name__ == "__main__":
    main()
```

</details>

Замер охватывает серверную выдачу сводок и вход для одноимённой секции solve-task. Суммарная задержка `prepare_task_context`, затраты сетевого запроса к Voyage и качество сгенерированного LLM-брифа остались за пределами замера. Живая выборка показывает устранение подтверждённо устаревших текстов для трёх запросов на одном снимке индекса.
