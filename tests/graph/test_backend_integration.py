"""Интеграционные тесты backend: git worktree round-trip и реальный прогон scip-python.

Тест worktree использует только git (запускается всегда). Тест реального SCIP
пропускается, если ``scip-python`` не найден в PATH.
"""
import os
import shutil
import subprocess

import pytest

from reviewer.graph.backend import build_with_scip
from reviewer.gitutil import add_worktree, remove_worktree


def _git(repo: str, *args: str) -> None:
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True)


def _init_repo(repo: str, files: dict[str, str]) -> None:
    """Инициализировать git-репо с заданными файлами и одним коммитом."""
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    for path, src in files.items():
        with open(os.path.join(repo, path), "w") as f:
            f.write(src)
    _git(repo, "add", *files.keys())
    _git(repo, "commit", "-qm", "init")


def test_worktree_roundtrip(tmp_path):
    """add_worktree чекаутит ref на диск, remove_worktree убирает worktree и temp-каталог."""
    repo = str(tmp_path / "repo")
    os.mkdir(repo)
    _init_repo(repo, {"a.py": "x = 1\n"})

    wt = add_worktree(repo, "HEAD")
    try:
        checked_out = os.path.join(wt, "a.py")
        assert os.path.exists(checked_out)
        with open(checked_out) as f:
            assert f.read() == "x = 1\n"
    finally:
        parent = os.path.dirname(wt)
        remove_worktree(repo, wt)

    # worktree и его временный родитель удалены
    assert not os.path.exists(wt)
    assert not os.path.exists(parent)


@pytest.mark.skipif(shutil.which("scip-python") is None, reason="scip-python не установлен")
def test_build_with_scip_real(tmp_path):
    """Реальный scip-python: точное кросс-файловое ребро CALLS a.py#f -> b.py#g."""
    repo = str(tmp_path / "repo")
    os.mkdir(repo)
    files = {
        "b.py": "def g():\n    return 1\n",
        "a.py": "from b import g\n\n\ndef f():\n    return g()\n",
    }
    _init_repo(repo, files)

    nodes, edges = build_with_scip(repo, "HEAD", dict(files))

    assert "a.py#f" in nodes
    assert "b.py#g" in nodes
    assert ("a.py#f", "CALLS", "b.py#g") in edges


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("scip-python") is None,
                    reason="scip-python не установлен")
def test_scip_still_drops_forward_referenced_class_symbol(tmp_path):
    """Фиксирует дефект scip-python 0.6.6, ради которого наследование берётся
    из tree-sitter: для класса, упомянутого выше своего определения,
    SymbolInformation не эмитится вовсе.

    Если будущая версия начнёт его эмитить — тест покраснеет, и это сигнал
    пересмотреть комментарии, а не поломка: слияние в build_with_scip
    дедуплицирует рёбра и останется корректным.
    """
    import subprocess

    from reviewer.graph.scip_pb2 import Index

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "base.py").write_text("class Base:\n    pass\n")
    (pkg / "adapter.py").write_text(
        "from pkg.base import Base\n\n\n"
        "def spec():\n    return Adapter\n\n\n"
        "class Adapter(Base):\n    pass\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "probe"\nversion = "0.0.1"\n'
    )
    subprocess.run(["scip-python", "index", ".", "--project-name=probe"],
                   cwd=tmp_path, check=True, capture_output=True)

    idx = Index()
    idx.ParseFromString((tmp_path / "index.scip").read_bytes())
    symbols = {
        si.symbol for doc in idx.documents for si in doc.symbols
        if doc.relative_path.endswith("adapter.py")
    }
    assert not [s for s in symbols if s.endswith("/Adapter#")]
