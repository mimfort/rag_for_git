"""Одинаковый короткий бриф по отрисованному коду для обоих вариантов."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import perf_counter

from eval.pri294.replay import CASES, read, write

MODEL = "gpt-6-luna"
EFFORT = "low"
PROMPT = """Собери отдельный краткий бриф для каждой карточки, используя только её
постановку и переданную секцию кода. Карточки независимы. Достаточно одной
строки с сутью задачи и раздела ## Relevant code. Сохрани в нём каждый файл,
непосредственно нужный для изменения или понимания реализации, по одной
ссылке на файл в порядке выдачи. Формат пункта: '- path:line — зачем читать
или менять файл'. Указывай только видимые номера строк. Заверши раздел
строкой '(dropped N: краткая причина)'. Не вызывай инструменты.
Верни JSON заданной схемы с Markdown в поле brief.
"""
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["briefs"],
    "properties": {
        "briefs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "brief"],
                "properties": {"key": {"type": "string"}, "brief": {"type": "string"}},
            },
        },
    },
}


def run_batch(batch, invocation, output):
    command = [
        "codex", "exec", "--cd", str(invocation), "--skip-git-repo-check",
        "--ephemeral", "--json", "--sandbox", "read-only", "--ignore-user-config",
        "-m", MODEL, "-c", f'model_reasoning_effort="{EFFORT}"',
        "--output-schema", str(invocation / "schema.json"), "-",
    ]
    start = perf_counter()
    result = subprocess.run(
        command, input=PROMPT + "\n" + json.dumps(batch, ensure_ascii=False),
        capture_output=True, text=True, timeout=540, check=False,
    )
    elapsed = round(perf_counter() - start, 6)
    output.with_suffix(".events.jsonl").write_text(result.stdout, encoding="utf-8")
    output.with_suffix(".stderr").write_text(result.stderr, encoding="utf-8")
    result.check_returncode()
    events = [json.loads(line) for line in result.stdout.splitlines()]
    items = [event["item"] for event in events if event["type"] == "item.completed"]
    if unexpected := [item["type"] for item in items
                      if item["type"] not in ("agent_message", "reasoning")]:
        raise RuntimeError(f"Модель вызвала инструменты: {unexpected}")
    messages = [item["text"] for item in items if item["type"] == "agent_message"]
    received = json.loads(messages[-1])["briefs"]
    by_key = {row["key"]: row["brief"] for row in received}
    if len(by_key) != len(received) or set(by_key) != {row["key"] for row in batch}:
        raise ValueError("В выдаче пропущены или повторены карточки")
    usage = [event["usage"] for event in events if event["type"] == "turn.completed"][-1]
    write(output, {
        "model": MODEL, "effort": EFFORT, "elapsed_seconds": elapsed,
        "usage": usage, "briefs": by_key,
        "context_sha256": {row["key"]: hashlib.sha256(row["context"].encode()).hexdigest()
                           for row in batch},
    })
    print(output.name, len(batch), f"{elapsed:.1f} с", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--baseline-only", action="store_true",
                        help="Собрать брифы по предварительной baseline-выдаче")
    parser.add_argument("--arm", choices=("baseline", "candidate"),
                        help="На парной выдаче ограничиться одним вариантом")
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.batch_size < 1 or args.workers < 1:
        parser.error("--batch-size и --workers должны быть положительными")
    work = args.work.resolve()
    snapshot = read(work / "snapshot.json")
    retrieval = read(work / ("baseline.json" if args.baseline_only else "retrieval.json"))
    invocation = work / "invocation"
    invocation.mkdir(exist_ok=True)
    write(invocation / "schema.json", SCHEMA)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        jobs = []
        for start in range(0, len(CASES), args.batch_size):
            keys = CASES[start:start + args.batch_size]
            for arm, rows in retrieval["arms"].items():
                if args.arm and args.arm != arm:
                    continue
                output = work / f"brief-{arm}-{start}.json"
                batch = [{"key": key, "task": snapshot["tasks"][key],
                          "context": rows[key]["context"]} for key in keys]
                expected = {row["key"]: hashlib.sha256(row["context"].encode()).hexdigest()
                            for row in batch}
                if not output.exists() or read(output).get("context_sha256") != expected:
                    jobs.append(executor.submit(run_batch, batch, invocation, output))
        for job in jobs:
            job.result()


if __name__ == "__main__":
    main()
