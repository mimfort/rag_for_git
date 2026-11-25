"""Оценить порядок одних и тех же фрагментов в брифе на архиве PRI-278."""

from __future__ import annotations

import argparse
import gzip
import json
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from eval.pri278.briefs import MODEL, PROMPT, SCHEMA
from eval.pri299.pilot import digest, index_response, run_condition, score_markdown
from reviewer.mcp.brief_evidence import _HEADER, _TRUNCATED, _visible_lines

CORPUS = Path(__file__).resolve().parents[1] / "pri278"
CONDITIONS = ("rank", "edges", "graph_fixed")
POSITION_GROUPS = ("first", "middle", "last")
LABEL_GROUPS = {"core": "obligatory", "dependencies": "read_only"}
BATCH_SIZE = 5
REPEATS = (1, 2)
MIN_LARGE_FILES = 10
MIN_CORE_GAIN_PP = 5


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def split_blocks(context: str) -> list[str]:
    # Это точный разделитель ContextPack.as_context. Обрезанный блок не
    # перемещаем: положение маркера меняет карту полностью видимых строк.
    blocks = context.split("\n\n")
    if any(not _HEADER.fullmatch(block.partition("\n")[0]) for block in blocks):
        raise ValueError("Выдача не состоит только из блоков кода")
    if _TRUNCATED in context.splitlines():
        raise ValueError("Обрезанная выдача не допускается к переупорядочиванию")
    return blocks


def edge_order(indices: list[int]) -> list[int]:
    return indices[::2] + indices[1::2][::-1]


def condition_contexts(context: str, ranked_paths: list[str], expanded_paths: list[str]):
    blocks = split_blocks(context)
    paths = [_HEADER.fullmatch(block.partition("\n")[0])["path"] for block in blocks]
    if len(paths) != len(set(paths)) or len(paths) > 20:
        raise ValueError("Ожидается один фрагмент на файл, максимум 20 файлов")
    if set(paths) - set(expanded_paths):
        raise ValueError("Фрагмент отсутствует в архиве расширенных путей")

    # Позиции графовых путей сохранены без обращения к экспертной разметке.
    # Этот признак охватывает только добавленные графом пути.
    ranked = set(ranked_paths)
    movable = [index for index, path in enumerate(paths) if path in ranked]
    graph_fixed = blocks.copy()
    for destination, source in zip(movable, edge_order(movable), strict=True):
        graph_fixed[destination] = blocks[source]
    outputs = {
        "rank": context,
        "edges": "\n\n".join(blocks[index] for index in edge_order(list(range(len(blocks))))),
        "graph_fixed": "\n\n".join(graph_fixed),
    }
    for variant in outputs.values():
        if (
            len(variant.encode("utf-8")) != len(context.encode("utf-8"))
            or Counter(split_blocks(variant)) != Counter(blocks)
            or _visible_lines(variant) != _visible_lines(context)
        ):
            raise ValueError("Переупорядочивание изменило состав или видимость строк")
    return outputs


def load_inputs() -> tuple[list[dict], dict]:
    corpus = read_json(CORPUS / "corpus.json")
    independent_keys = {case["key"] for case in read_json(CORPUS / "independent-corpus.json")}
    archive = {}
    with gzip.open(CORPUS / "pool_size/evidence.jsonl.gz", "rt", encoding="utf-8") as stream:
        for record in map(json.loads, stream):
            if record["run"] == "all-pool-30" and (row := record.get("retrieval")):
                if row["key"] in archive:
                    raise ValueError(f"Дубликат выдачи для {row['key']}")
                archive[row["key"]] = row

    if len(corpus) != 20 or {case["key"] for case in corpus} != archive.keys():
        raise ValueError("Ожидаются 20 зафиксированных пар постановка/выдача")
    inputs, labels = [], {}
    for case in corpus:
        key = case["key"]
        if key in independent_keys or case["base_sha"] != archive[key]["base_sha"]:
            raise ValueError(f"Пересечение с пилотом или другой снимок для {key}")
        mode = archive[key]["modes"]["hybrid"]
        inputs.append({
            "key": key,
            "task": case["input"],
            "base_sha": case["base_sha"],
            "contexts": condition_contexts(
                mode["context"], mode["ranked_paths"], mode["expanded_paths"],
            ),
        })
        labels[key] = {
            name: {row["path"] for row in case[field]} for name, field in LABEL_GROUPS.items()
        }
    if sum(len(gold["core"]) >= MIN_LARGE_FILES for gold in labels.values()) < 5:
        raise ValueError("Требуется не менее пяти крупных задач")
    return inputs, labels


def batches(inputs: list[dict]) -> list[list[dict]]:
    return [inputs[start:start + BATCH_SIZE] for start in range(0, len(inputs), BATCH_SIZE)]


def schedule(repeat: int, batch: int) -> tuple[str, ...]:
    offset = batch % len(CONDITIONS)
    order = CONDITIONS[offset:] + CONDITIONS[:offset]
    return order if repeat == 1 else order[::-1]


def prompt_input(batch: list[dict], condition: str) -> str:
    return json.dumps([
        {"key": row["key"], "task": row["task"], "context": row["contexts"][condition]}
        for row in batch
    ], ensure_ascii=False)


def protocol(inputs: list[dict]) -> dict:
    return {
        "model": MODEL,
        "reasoning_effort": "low",
        "prompt_sha256": digest(PROMPT),
        "schema_sha256": digest(json.dumps(SCHEMA)),
        "conditions": list(CONDITIONS),
        "batches": [
            {
                "keys": [row["key"] for row in batch],
                "base_sha": [row["base_sha"] for row in batch],
                "sha256": {name: digest(prompt_input(batch, name)) for name in CONDITIONS},
                "orders": [list(schedule(repeat, index)) for repeat in REPEATS],
            }
            for index, batch in enumerate(batches(inputs))
        ],
    }


def run_batch(repeat: int, index: int, batch: list[dict], schema: Path, work: Path) -> None:
    for name in schedule(repeat, index):
        target = work / f"r{repeat}-b{index + 1}-{name}.json"
        if target.exists() and "error" not in read_json(target):
            continue
        started = perf_counter()
        try:
            result = run_condition(name, repeat, prompt_input(batch, name), PROMPT, schema)
        except (ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            result = {
                "condition": name,
                "rep": repeat,
                "elapsed_s": round(perf_counter() - started, 3),
                "error": type(exc).__name__,
            }
            print(f"Ошибка {target.stem}: {type(exc).__name__}", flush=True)
        write_json(target, result)


def run(inputs: list[dict], work: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    lock = work / "protocol.json"
    current = protocol(inputs)
    current["codex_version"] = subprocess.check_output(["codex", "--version"], text=True).strip()
    if lock.exists() and read_json(lock) != current:
        raise ValueError("Артефакты относятся к другому протоколу")
    write_json(lock, current)
    with TemporaryDirectory(prefix="pri310-codex-") as temporary:
        schema = Path(temporary) / "schema.json"
        write_json(schema, SCHEMA)
        with ThreadPoolExecutor(max_workers=4) as pool:
            for repeat in REPEATS:
                jobs = [
                    pool.submit(run_batch, repeat, index, batch, schema, work)
                    for index, batch in enumerate(batches(inputs))
                ]
                for job in jobs:
                    job.result()


def score_task(row: dict, condition: str, brief: dict | None, gold: dict) -> dict:
    context = row["contexts"][condition]
    visible = _visible_lines(context)
    if brief is None:
        valid, checked, errors = [], [], ["brief_missing_or_repeated"]
    else:
        valid, checked, errors, _ = score_markdown(brief, visible)
    selected = {path for path, _ in valid}
    blocks = split_blocks(context)
    path_positions = {}
    for index, block in enumerate(blocks):
        path = _HEADER.fullmatch(block.partition("\n")[0])["path"]
        if path in visible:
            path_positions[path] = POSITION_GROUPS[index * len(POSITION_GROUPS) // len(blocks)]
    labelled = gold["core"] | gold["dependencies"]
    per_position = {}
    for group in POSITION_GROUPS:
        paths = {path for path, position in path_positions.items() if position == group}
        per_position[group] = {
            "labelled": len(paths & labelled),
            "visible": len(paths),
            "cited": len(paths & selected),
            "labelled_cited": len(paths & selected & labelled),
        }
    return {
        "key": row["key"],
        "large": len(gold["core"]) >= MIN_LARGE_FILES,
        "coverage": {
            name: {"matched": len(selected & paths), "all": len(paths)}
            for name, paths in gold.items()
        },
        "positions": per_position,
        "format_errors": errors,
        "rejected_references": sum(status != "verified" for status in checked),
    }


def aggregate(tasks: list[dict], batches_scored: list[dict]) -> dict:
    runtimes = [batch["elapsed_s"] for batch in batches_scored if "usage" in batch]
    return {
        **{
            subset: {
                name: {field: sum(row["coverage"][name][field] for row in rows)
                       for field in ("matched", "all")}
                for name in LABEL_GROUPS
            }
            for subset, rows in (("all", tasks), ("large", [row for row in tasks if row["large"]]))
        },
        "positions": {
            group: {field: sum(row["positions"][group][field] for row in tasks)
                    for field in ("labelled", "visible", "cited", "labelled_cited")}
            for group in POSITION_GROUPS
        },
        "missing_briefs": sum("brief_missing_or_repeated" in row["format_errors"] for row in tasks),
        "format_errors": sum(bool(row["format_errors"]) for row in tasks),
        "rejected_references": sum(row["rejected_references"] for row in tasks),
        "batches_completed": sum("usage" in batch for batch in batches_scored),
        "batches_expected": len(batches_scored),
        "median_batch_s": median(runtimes) if runtimes else None,
        "usage": dict(sum((Counter(batch.get("usage", {})) for batch in batches_scored), Counter())),
    }


def assess(baseline: dict, candidate: dict) -> dict:
    def change(subset, group):
        control = baseline[subset][group]
        variant = candidate[subset][group]
        if control["all"] != variant["all"]:
            raise ValueError("Различаются знаменатели покрытия")
        return 100 * (variant["matched"] - control["matched"]) / control["all"]

    core_gain = change("all", "core")
    dependency_gain = change("all", "dependencies")
    dependency_gain_large = change("large", "dependencies")
    eligible = (
        not baseline["missing_briefs"] and not candidate["missing_briefs"]
        and core_gain >= MIN_CORE_GAIN_PP and dependency_gain >= 0 and dependency_gain_large >= 0
    )
    return {
        "core_delta_pp": round(core_gain, 2),
        "dependencies_delta_pp": round(dependency_gain, 2),
        "large_dependencies_delta_pp": round(dependency_gain_large, 2),
        "eligible": eligible,
    }


def read_responses(work: Path, batch_count: int) -> tuple[dict, dict]:
    archive = work / "responses.json.gz"
    staged = [work / f"r{repeat}-b{index}-{name}.json"
              for repeat in REPEATS for index in range(1, batch_count + 1) for name in CONDITIONS]
    staged = [path for path in staged if path.exists()]
    if not staged and archive.exists():
        bundle = json.loads(gzip.decompress(archive.read_bytes()))
        return bundle["protocol"], bundle["records"]
    metadata = read_json(work / "protocol.json")
    records = {path.stem: read_json(path) for path in staged}
    bundle = json.dumps({"protocol": metadata, "records": records}, ensure_ascii=False)
    packed = archive.with_suffix(".tmp")
    packed.write_bytes(gzip.compress(bundle.encode("utf-8"), mtime=0))
    packed.replace(archive)
    return metadata, records


def score(inputs: list[dict], labels: dict, work: Path) -> None:
    grouped = batches(inputs)
    metadata, responses = read_responses(work, len(grouped))
    if any(metadata[field] != expected for field, expected in protocol(inputs).items()):
        raise ValueError("Артефакты не соответствуют зафиксированному входу")

    by_repeat, pooled = {}, {}
    for name in CONDITIONS:
        all_tasks, all_records = [], []
        for repeat in REPEATS:
            tasks, records = [], []
            for index, batch in enumerate(grouped, 1):
                target = f"r{repeat}-b{index}-{name}"
                record = responses.get(target, {})
                if record and (record["condition"], record["rep"]) != (name, repeat):
                    raise ValueError(f"Ошибка идентификации лога: {target}")
                records.append(record)
                observed = index_response(record.get("response", ""))
                tasks.extend(score_task(row, name, observed.get(row["key"]), labels[row["key"]])
                             for row in batch)
            by_repeat.setdefault(repeat, {})[name] = {**aggregate(tasks, records), "tasks": tasks}
            all_tasks.extend(tasks)
            all_records.extend(records)
        pooled[name] = aggregate(all_tasks, all_records)
    decisions = {name: assess(pooled["rank"], pooled[name]) for name in CONDITIONS[1:]}
    result = {"protocol": metadata, "repeats": by_repeat, "pooled": pooled, "gate": decisions}
    write_json(work / "metrics.json", result)
    print(json.dumps({"pooled": pooled, "gate": decisions}, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["protocol", "run", "score"])
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    inputs, labels = load_inputs()
    if args.action == "protocol":
        print(json.dumps(protocol(inputs), ensure_ascii=False, indent=2))
    elif args.action == "run":
        run(inputs, args.work.resolve())
    else:
        score(inputs, labels, args.work.resolve())


if __name__ == "__main__":
    main()
