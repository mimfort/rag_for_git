"""Прогон набора PR-кейсов: для каждого кейса известны ожидаемые проблемы (файл+строка+категория).
Считает precision/recall сматченных findings. Кейсы кладутся в eval/cases/<name>/ с:
  - before/ и after/ (снапшоты кода),
  - patch.diff,
  - expected.json [{file,line,category}].
Запуск: python eval/run_eval.py
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass

from reviewer.config.settings import Settings
from reviewer.services.review_service import ReviewService
from reviewer.vcs.base import PullRequest, ChangedFile


# ---------------------------------------------------------------------------
# Парсинг unified diff
# ---------------------------------------------------------------------------


def _split_patch(diff_text: str) -> dict[str, tuple[str, str | None]]:
    """Разбить unified diff на словарь {path: (status, patch)}.

    Распознаёт добавленные/удалённые/изменённые файлы по заголовкам diff.
    """
    files: dict[str, tuple[str, str | None]] = {}
    current_path: str | None = None
    current_lines: list[str] = []

    def _flush() -> None:
        nonlocal current_path, current_lines
        if current_path is not None:
            files[current_path] = (
                _guess_status(current_lines),
                "\n".join(current_lines) if current_lines else None,
            )
        current_path = None
        current_lines = []

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            _flush()
            current_path = _extract_path(line)
            current_lines = [line]
        else:
            current_lines.append(line)
    _flush()
    return files


def _extract_path(diff_git_line: str) -> str:
    """Извлечь путь файла из строки ``diff --git a/... b/...``."""
    parts = diff_git_line.split()
    if len(parts) >= 4:
        b_path = parts[3]
        if b_path.startswith("b/"):
            return b_path[2:]
        a_path = parts[2]
        if a_path.startswith("a/"):
            return a_path[2:]
        return b_path
    return "unknown"


def _guess_status(lines: list[str]) -> str:
    """Определить статус файла (added/removed/modified) по заголовкам."""
    old_line = None
    new_line = None
    for line in lines[:10]:
        if line.startswith("--- "):
            old_line = line
        elif line.startswith("+++ "):
            new_line = line
    is_old_null = old_line is not None and "/dev/null" in old_line
    is_new_null = new_line is not None and "/dev/null" in new_line
    if is_old_null:
        return "added"
    if is_new_null:
        return "removed"
    return "modified"


# ---------------------------------------------------------------------------
# VCS-провайдер для локальных снапшотов
# ---------------------------------------------------------------------------


class SnapshotProvider:
    """VCS-провайдер для локальных before/after-снапшотов (eval-кейсы)."""

    def __init__(self, case_dir: pathlib.Path) -> None:
        self.case_dir = case_dir
        self._patches = _split_patch(self._read_patch())

    def _read_patch(self) -> str:
        patch_file = self.case_dir / "patch.diff"
        if patch_file.exists():
            return patch_file.read_text(encoding="utf-8", errors="replace")
        return ""

    def get_pull_request(self, number: int) -> PullRequest:
        return PullRequest(
            number=number,
            base_sha="base",
            head_sha="head",
            base_ref="main",
            title="Eval case",
            body="",
            draft=False,
        )

    def get_changed_files(self, number: int) -> list[ChangedFile]:
        return [
            ChangedFile(path=path, status=status, patch=patch)
            for path, (status, patch) in self._patches.items()
        ]

    def get_file_at_ref(self, path: str, ref: str) -> str | None:
        dir_name = "before" if ref == "base" else "after"
        file_path = self.case_dir / dir_name / path
        if file_path.exists():
            return file_path.read_text(encoding="utf-8", errors="replace")
        return None

    def list_existing_fingerprints(self, number: int) -> set[str]:
        return set()

    def publish_review(
        self,
        number: int,
        head_sha: str,
        summary: str,
        comments: list,
    ) -> None:
        pass

    def compare_files(self, base_sha: str, head_sha: str) -> list[ChangedFile]:
        return self.get_changed_files(0)

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Скоринг
# ---------------------------------------------------------------------------


def score(expected: list[dict], produced: list[dict]) -> dict:
    exp = {(e["file"], e["line"], e["category"]) for e in expected}
    prod = {(p["file"], p["line"], p["category"]) for p in produced}
    tp = len(exp & prod)
    precision = tp / len(prod) if prod else 0.0
    recall = tp / len(exp) if exp else 0.0
    return {"precision": precision, "recall": recall, "tp": tp}


# ---------------------------------------------------------------------------
# Прогон кейсов
# ---------------------------------------------------------------------------


@dataclass
class EvalResult:
    """Результат прогона одного eval-кейса."""

    case: str
    precision: float
    recall: float
    f1: float
    tp: int
    expected_count: int
    produced_count: int
    cost: float
    tokens: int
    duration_ms: int


def run_case(
    case_dir: pathlib.Path,
    settings: Settings,
    components,
) -> EvalResult:
    """Прогнать один eval-кейс через ReviewService(dry_run=True).

    Args:
        case_dir: директория кейса (before/, after/, patch.diff, expected.json).
        settings: настройки проекта.
        components: компоненты из ``build_components(settings)``.

    Returns:
        EvalResult с метриками и usage.
    """
    expected_path = case_dir / "expected.json"
    expected = (
        json.loads(expected_path.read_text(encoding="utf-8"))
        if expected_path.exists()
        else []
    )

    vcs = SnapshotProvider(case_dir)
    service = ReviewService(settings, components)
    result = service.run_review("owner", "repo", 1, dry_run=True, vcs_provider=vcs)

    produced = [
        {"file": f.file, "line": f.line, "category": f.category}
        for f in result.state.get("verified", [])
    ]

    s = score(expected, produced)
    precision = s["precision"]
    recall = s["recall"]
    tp = s["tp"]
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )

    usage_snap = result.usage.snapshot()
    total_cost = sum(v.get("cost", 0.0) for v in usage_snap.values())
    total_tokens = result.usage.total_tokens

    return EvalResult(
        case=case_dir.name,
        precision=precision,
        recall=recall,
        f1=f1,
        tp=tp,
        expected_count=len(expected),
        produced_count=len(produced),
        cost=total_cost,
        tokens=total_tokens,
        duration_ms=result.duration_ms,
    )


def run_dataset(
    dataset_dir: pathlib.Path,
    settings: Settings,
    components,
    out_path: pathlib.Path | None = None,
) -> list[EvalResult]:
    """Прогнать все кейсы из набора и записать результаты в JSONL.

    Args:
        dataset_dir: директория с поддиректориями-кейсами.
        settings: настройки проекта.
        components: компоненты из ``build_components(settings)``.
        out_path: путь к JSONL-файлу для записи результатов.

    Returns:
        Список результатов по каждому кейсу.
    """
    cases = sorted(
        p for p in dataset_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )
    results: list[EvalResult] = []

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("", encoding="utf-8")

    for case_dir in cases:
        result = run_case(case_dir, settings, components)
        results.append(result)

        if out_path is not None:
            record = {
                "case": result.case,
                "precision": result.precision,
                "recall": result.recall,
                "f1": result.f1,
                "tp": result.tp,
                "expected_count": result.expected_count,
                "produced_count": result.produced_count,
                "cost": result.cost,
                "tokens": result.tokens,
                "duration_ms": result.duration_ms,
            }
            with out_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return results


# ---------------------------------------------------------------------------
# CLI-точка входа
# ---------------------------------------------------------------------------


def main() -> None:
    """Ручной запуск eval через реальные компоненты."""
    dataset = pathlib.Path("eval/cases")
    out = pathlib.Path("eval/results.jsonl")

    from reviewer.app import build_components  # noqa: PLC0415

    settings = Settings()
    components = build_components(settings)
    try:
        results = run_dataset(dataset, settings, components, out)
        print(f"Кейсов пройдено: {len(results)}")
        for r in results:
            print(
                f"{r.case}: P={r.precision:.2f} R={r.recall:.2f} F1={r.f1:.2f} "
                f"(cost=${r.cost:.4f}, tokens={r.tokens}, time={r.duration_ms}ms)"
            )
    finally:
        components.store.close()
        if components.graph:
            components.graph.close()


if __name__ == "__main__":
    main()
