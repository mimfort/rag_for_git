"""Guard: глобальные branch-методы Settings вызываются только из branches.py.

`Settings.review_branches_list()`/`Settings.primary_branch()` — env-фолбэк для
резолва веток. Любой прямой вызов этих методов вне `reviewer/config/branches.py`
(и самого `settings.py`, где они определены) обходит per-repo резолвер
(`resolve_repo_branches`) и рискует взять чужие ветки в мульти-репо деплое.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "reviewer"
PATTERN = re.compile(r"\b(review_branches_list|primary_branch)\s*\(")

ALLOWED = {
    Path("config/branches.py"),
    Path("config/settings.py"),
}


def test_branch_methods_are_called_only_from_branches_module():
    offenders = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if rel in ALLOWED:
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # упоминание в комментарии — не вызов
            if PATTERN.search(line) and "def " not in line:
                offenders.append(f"{rel}:{number}: {line.strip()}")
    assert offenders == [], "\n".join(offenders)
