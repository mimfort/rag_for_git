"""Пересчитать сохранённое сравнение low и medium на тех же секциях кода."""
from __future__ import annotations

import gzip
import json
import os
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from eval.pri299.pilot import digest, index_response, load_conditions
from eval.pri324.drafting.replay import REF, checked_sections, make_batches, score_case


def main() -> None:
    with gzip.open(Path(__file__).with_name("effort.json.gz"), "rt", encoding="utf-8") as stream:
        archive = json.load(stream)
    protocol = archive["protocol"]
    batches, labels = make_batches()
    prompt, schema = load_conditions()["structured"]
    input_hashes = {
        f"{corpus}-{size}": digest(json.dumps(
            [{field: case[field] for field in ("key", "task", "context")} for case in batch],
            ensure_ascii=False,
        ))
        for (corpus, size), batch in batches.items()
    }
    if (
        protocol["base_sha"] != REF or protocol["prompt_sha"] != digest(prompt)
        or protocol["schema_sha"] != digest(json.dumps(schema, ensure_ascii=False))
        or protocol["input_sha"] != input_hashes
    ):
        raise ValueError("Входные данные отличаются от протокола")

    allowed = {case["key"]: checked_sections(case) for batch in batches.values() for case in batch}
    responses = archive["responses"]
    expected_keys = {
        f"r{repeat}-{corpus}-{size}-{condition}"
        for corpus, size in batches
        for repeat in ((1, 2) if size == "large" else (1,))
        for condition in ("low", "medium")
    }
    if responses.keys() != expected_keys:
        raise ValueError("Не совпал набор ожидаемых парных ответов")

    result = {}
    seen = set()
    for metric in archive["metrics"]:
        corpus, size, condition, repeat = (
            metric[field] for field in ("corpus", "size", "condition", "repeat")
        )
        key = f"r{repeat}-{corpus}-{size}-{condition}"
        response = responses[key]
        batch = batches[corpus, size]
        briefs = index_response(response["response"])
        if key in seen or briefs.keys() != {case["key"] for case in batch}:
            raise ValueError(f"Неполный или повторный ответ {key}")
        seen.add(key)
        tasks = [
            score_case(briefs[case["key"]], case, labels[case["key"]], allowed[case["key"]])
            for case in batch
        ]
        if tasks != metric["tasks"]:
            raise ValueError(f"Пересчёт разошёлся с сохранёнными данными {key}")
        totals = result.setdefault(size, {}).setdefault(corpus, {}).setdefault(condition, Counter())
        for field in ("core", "core_total", "dep", "dep_total", "paths", "invalid"):
            totals[field] += sum(task[field] for task in tasks)
    if seen != expected_keys:
        raise ValueError("В сохранённых метриках не все ожидаемые ответы")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    with TemporaryDirectory(prefix="pri324-effort-score-") as state:
        os.environ["XDG_STATE_HOME"] = state
        main()
