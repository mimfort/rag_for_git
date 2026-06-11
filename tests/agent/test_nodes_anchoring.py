"""Тесты валидации координат находок в assemble (anti-галлюцинация file:line).

Модель иногда возвращает несуществующие номера строк (напр. строку класса из
другого модуля). Такие находки не должны порождать inline на несуществующей
строке и не должны показывать ложный ``file:line`` в сводке.
"""
from reviewer.agent.nodes import make_assemble_node
from reviewer.agent.state import Deps
from reviewer.policy.policy import ReviewPolicy
from reviewer.vcs.base import Finding


class _NoExistingVCS:
    def list_existing_fingerprints(self, n):
        return set()

    def publish_review(self, n, sha, summary, comments):
        pass


def _deps(changed_paths, patches, sources):
    return Deps(
        vcs=_NoExistingVCS(),
        retriever=None,
        graph=None,
        policy=ReviewPolicy(),
        analyzer=None,
        verifier=None,
        pr_number=1,
        head_sha="s",
        overlay_ref="pr:1",
        changed_paths=changed_paths,
        patches=patches,
        sources=sources,
    )


def test_summary_drops_hallucinated_line_beyond_file_length():
    """Строка за пределами файла (a.py — 10 строк, находка на 616) уходит в сводку
    без ложного :line и не становится inline."""
    src = "\n".join(f"line{i}" for i in range(1, 11))   # 10 строк
    deps = _deps(["a.py"], {"a.py": "@@ -1,2 +1,2 @@\n x\n+y\n"}, {"a.py": src})
    f = Finding("correctness", "high", "a.py", 616, "RIGHT", "выдуманная строка", None, 0.9)
    node = make_assemble_node(deps)

    result = node({"verified": [f], "failed_units": []})

    assert result["inline_comments"] == []
    assert "a.py:616" not in result["summary"]
    assert "`a.py`" in result["summary"]      # файл показан, но без номера строки


def test_summary_drops_line_for_file_not_in_pr():
    """Находка про файл, которого нет среди изменённых, теряет ложную строку."""
    deps = _deps(["a.py"], {"a.py": "@@ -1,2 +1,2 @@\n x\n+y\n"}, {"a.py": "x\ny\n"})
    f = Finding("correctness", "medium", "reviewer/llm/other.py", 110,
                "RIGHT", "кросс-файловая находка", None, 0.8)
    node = make_assemble_node(deps)

    result = node({"verified": [f], "failed_units": []})

    assert result["inline_comments"] == []
    assert "other.py:110" not in result["summary"]
    assert "`reviewer/llm/other.py`" in result["summary"]


def test_valid_line_still_anchored_inline():
    """Регрессия: реальная строка диффа по-прежнему даёт inline-комментарий."""
    deps = _deps(["a.py"], {"a.py": "@@ -1,2 +1,2 @@\n x\n+y\n"}, {"a.py": "x\ny\n"})
    f = Finding("correctness", "high", "a.py", 2, "RIGHT", "реальная строка", None, 0.9)
    node = make_assemble_node(deps)

    result = node({"verified": [f], "failed_units": []})

    assert len(result["inline_comments"]) == 1
    assert result["inline_comments"][0].path == "a.py"
    assert result["inline_comments"][0].line == 2
