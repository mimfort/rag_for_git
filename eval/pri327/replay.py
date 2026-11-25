"""Офлайн пересчёт двух вариантов отбора фрагмента по сохранённым ответам."""

from __future__ import annotations

import gzip
import json
import os
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from eval.pri278.briefs import PROMPT, SCHEMA
from eval.pri278.replay import blob_reader, snapshot_sources
from eval.pri324 import replay
from eval.pri327.literal import require_literal_signal
from eval.pri327.windows import preserve_original_anchor
from reviewer.mcp.brief_anchors import check_brief_anchors
from reviewer.mcp.brief_evidence import BriefEvidence, _visible_lines

_ARCHIVE = Path(__file__).with_name("responses.json.gz")
_ARMS = {"literal": ("reframe", "guarded"), "two_views": ("reframe", "dual")}


def _prepare_cases() -> dict:
    cases = replay._prepare(Path("eval/pri278"))
    with blob_reader() as read:
        cache = {}
        for data in cases.values():
            case, base, selected = (data[name] for name in ("case", "base", "reframe"))
            sources = snapshot_sources(case["base_sha"], read, cache)
            data["guarded"] = require_literal_signal(base, selected, case["input"])
            data["dual"] = preserve_original_anchor(base, selected)
            for arm in ("guarded", "dual"):
                section = data[arm]
                eligible, counts = check_brief_anchors(
                    {"code": section},
                    lambda paths, source=sources: {path: source[path] for path in paths}
                )
                numbers = _visible_lines(section)
                if eligible.get("code") != numbers or list(numbers) != list(_visible_lines(base)):
                    raise ValueError(f"{case['key']}, {arm}: изменились пути или строки")
                if len(section) > len(base) or (arm == "dual" and len(section.encode()) > len(base.encode())):
                    raise ValueError(f"{case['key']}, {arm}: превышен исходный бюджет")
                data[f"{arm}_eligible"] = eligible
                data[f"{arm}_git_lines"] = counts["code"]["checked"]
    return cases


def _validate_archive(cases: dict, experiment: dict, arms: tuple[str, str]) -> None:
    protocol, responses = (experiment[field] for field in ("protocol", "responses"))
    keys = {key for group in protocol["groups"] for key in group}
    fingerprints = {
        key: {
            **{arm: replay._digest_text(cases[key][arm]) for arm in arms},
            "sha": cases[key]["case"]["base_sha"],
        }
        for key in keys
    }
    if (
        fingerprints != protocol["cases"]
        or protocol["prompt_sha256"] != replay._digest_text(PROMPT)
        or protocol["schema_sha256"] != replay._digest_text(json.dumps(SCHEMA))
    ):
        raise ValueError("Входы или промпт расходятся с архивом")
    expected = {
        f"r{repeat}-b{batch}-{arm}"
        for repeat in (1, 2)
        for batch in range(1, len(protocol["groups"]) + 1)
        for arm in arms
    }
    actual = {
        name: replay._digest_text(json.dumps(response, sort_keys=True, ensure_ascii=False))
        for name, response in responses.items()
    }
    if expected != responses.keys() or actual != experiment["response_sha256"]:
        raise ValueError("Неполный архив или изменённые ответы")


def _slices(rows: list[dict], arms: tuple[str, str]) -> dict:
    groups = {
        "all_bulk": [row for row in rows if row["large"]],
        "changed_bulk": [row for row in rows if row["large"] and row["context_changed"]],
        "ordinary": [row for row in rows if not row["large"]],
        "ordinary_changed": [
            row for row in rows if not row["large"] and row["context_changed"]
        ],
        "identical_controls": [row for row in rows if not row["context_changed"]],
    }
    return {
        label: {
            arm: replay._summarize([row for row in group if row["arm"] == arm])
            for arm in arms
        }
        for label, group in groups.items() if group
    }


def _score_experiment(
    cases: dict, experiment: dict, arms: tuple[str, str], evidence: BriefEvidence
) -> dict:
    _validate_archive(cases, experiment, arms)
    protocol, responses = (experiment[field] for field in ("protocol", "responses"))
    metrics, excluded = [], []
    token_usage = {arm: Counter() for arm in arms}
    for repeat in (1, 2):
        for index, group in enumerate(protocol["groups"], 1):
            pair = {arm: responses[f"r{repeat}-b{index}-{arm}"] for arm in arms}
            incomplete = {}
            for arm, response in pair.items():
                replay._validate_envelope(response, protocol, group)
                if response["retry_attempts"]:
                    incomplete[arm] = list(response["retry_attempts"])
            if incomplete:
                excluded.append({"repeat": repeat, "batch": index, "retry": incomplete})
                continue
            for arm, response in pair.items():
                entries = replay._score_batch(cases, group, arm, response, protocol, evidence)
                for row in entries:
                    data = cases[row["key"]]
                    metrics.append({
                        "repeat": repeat,
                        "batch": index,
                        "context_changed": data[arms[0]] != data[arms[1]],
                        **row,
                    })
                token_usage[arm].update({
                    name: response["usage"].get(name, 0)
                    for name in ("input_tokens", "output_tokens")
                })
    return {
        "slices": _slices(metrics, arms),
        "pairs_excluded": excluded,
        "usage": token_usage,
        "per_brief": metrics,
    }


def main() -> None:
    cases = _prepare_cases()
    with gzip.open(_ARCHIVE, "rt", encoding="utf-8") as stream:
        archive = json.load(stream)
    with TemporaryDirectory(prefix="pri327-evidence-") as temp:
        with patch.dict(os.environ, {"XDG_STATE_HOME": temp}):
            evidence = BriefEvidence()
        result = {
            name: _score_experiment(cases, archive[name], arms, evidence)
            for name, arms in _ARMS.items()
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
