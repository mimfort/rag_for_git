"""Локальное чтение коммиченного `.review.yml` с фолбэком на VCS (PRI-235)."""
from __future__ import annotations

import subprocess

import pytest

from reviewer.config.committed import (
    CommittedLayerFetcher,
    CommittedLayerUnavailable,
    DriftReport,
    validate_clone,
    worktree_drift,
)


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def _make_clone(tmp_path, name="clone", remote: str | None = None,
                content: str = "summary_cluster_depth: 3\n"):
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / ".review.yml").write_text(content, encoding="utf-8")
    _git(root, "add", ".review.yml")
    _git(root, "commit", "-qm", "policy")
    if remote:
        _git(root, "remote", "add", "origin", remote)
    return root


def test_validate_clone_accepts_repo_without_remote(tmp_path):
    """Клон без remote принимается: ради него задача и затевалась."""
    root = _make_clone(tmp_path)
    assert validate_clone(str(root), "owner/name") == str(root)


def test_validate_clone_rejects_foreign_remote(tmp_path):
    """Клон чужого репозитория отвергается: путь в БД мог устареть."""
    root = _make_clone(tmp_path, remote="https://github.com/other/project.git")
    assert validate_clone(str(root), "owner/name") is None


def test_validate_clone_accepts_matching_remote_and_subdirectory(tmp_path):
    """Подкаталог допустим — используется корень клона."""
    root = _make_clone(tmp_path, remote="https://github.com/Owner/Name.git")
    nested = root / "pkg"
    nested.mkdir()
    assert validate_clone(str(nested), "owner/name") == str(root)


def test_validate_clone_rejects_non_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert validate_clone(str(plain), "owner/name") is None
    assert validate_clone(None, "owner/name") is None


def test_local_clone_wins_and_vcs_is_never_created(tmp_path):
    """При валидном клоне VCS-фабрика не дёргается вовсе."""
    root = _make_clone(tmp_path)
    calls: list[str] = []

    def factory():
        calls.append("created")
        return lambda ref: "from-vcs: true\n"

    fetcher = CommittedLayerFetcher(
        "owner/name", clone_path=str(root), vcs_fetch_factory=factory
    )
    assert fetcher.clone_available
    assert fetcher("main") == "summary_cluster_depth: 3\n"
    assert fetcher.source == "git-blob"
    assert calls == []


def test_missing_file_locally_is_absence_not_fallback(tmp_path):
    """Нет файла на ref → слоя нет (None), это не повод идти в VCS."""
    root = _make_clone(tmp_path)
    _git(root, "rm", "-q", ".review.yml")
    _git(root, "commit", "-qm", "drop policy")

    fetcher = CommittedLayerFetcher(
        "owner/name", clone_path=str(root),
        vcs_fetch_factory=lambda: (lambda ref: "from-vcs: true\n"),
    )
    assert fetcher("main") is None
    assert fetcher.source == "git-blob"


def test_unresolvable_ref_falls_back_to_vcs(tmp_path):
    """Ветка не выкачана в клоне → читаем через VCS, а не считаем слой пустым."""
    root = _make_clone(tmp_path)
    fetcher = CommittedLayerFetcher(
        "owner/name", clone_path=str(root),
        vcs_fetch_factory=lambda: (lambda ref: "from-vcs: true\n"),
    )
    assert fetcher("no-such-branch") == "from-vcs: true\n"
    assert fetcher.source == "vcs"


def test_without_clone_falls_back_to_vcs(tmp_path):
    fetcher = CommittedLayerFetcher(
        "owner/name", clone_path=None,
        vcs_fetch_factory=lambda: (lambda ref: "from-vcs: true\n"),
    )
    assert fetcher("main") == "from-vcs: true\n"
    assert fetcher.source == "vcs"
    assert not fetcher.clone_available


def test_without_clone_and_without_vcs_raises(tmp_path):
    """Нечем прочитать слой → исключение, которое резолвер обработает fail-soft."""
    fetcher = CommittedLayerFetcher("owner/name")
    with pytest.raises(CommittedLayerUnavailable):
        fetcher("main")


def test_resolver_uses_local_layer(tmp_path, monkeypatch):
    """Сквозной путь: resolve_policy_data берёт коммиченный слой из клона."""
    from reviewer.config.layers import resolve_policy_data

    root = _make_clone(tmp_path)
    config_root = tmp_path / "home"
    config_root.mkdir()
    fetcher = CommittedLayerFetcher("owner/name", clone_path=str(root))
    data, meta = resolve_policy_data(
        "owner/name", "main", fetcher, config_root=config_root
    )
    assert data["summary_cluster_depth"] == 3
    assert meta.skipped == ()
    assert fetcher.source == "git-blob"


def test_clean_worktree_reports_no_drift(tmp_path):
    root = _make_clone(tmp_path)
    assert worktree_drift(str(root), "main") == DriftReport("clean", ())


def test_worktree_edit_names_diverging_leaf_keys_without_values(tmp_path):
    """Правка в рабочем дереве видна по ключам; значения в отчёт не попадают."""
    root = _make_clone(tmp_path, content="max_comments: 5\ncontext_limits: {graph: {hops: 1}}\n")
    (root / ".review.yml").write_text(
        "max_comments: 9\ncontext_limits: {graph: {hops: 1}}\n", encoding="utf-8"
    )

    report = worktree_drift(str(root), "main")

    assert report.status == "drifted"
    assert report.keys == ("max_comments",)
    assert "9" not in " ".join(report.keys)


def test_missing_worktree_file_is_its_own_status(tmp_path):
    root = _make_clone(tmp_path)
    (root / ".review.yml").unlink()

    assert worktree_drift(str(root), "main").status == "absent_in_worktree"


def test_untracked_worktree_file_is_its_own_status(tmp_path):
    root = _make_clone(tmp_path)
    _git(root, "rm", "-q", ".review.yml")
    _git(root, "commit", "-qm", "drop policy")
    (root / ".review.yml").write_text("max_comments: 3\n", encoding="utf-8")

    assert worktree_drift(str(root), "main").status == "absent_in_blob"


def test_ref_other_than_head_is_not_compared(tmp_path):
    """Сравнение с рабочим деревом осмысленно только когда ref == HEAD клона."""
    root = _make_clone(tmp_path)
    _git(root, "checkout", "-q", "-b", "feature")
    (root / ".review.yml").write_text("max_comments: 42\n", encoding="utf-8")
    _git(root, "commit", "-qam", "feature policy")

    assert worktree_drift(str(root), "main").status == "ref_not_head"


def test_broken_yaml_in_worktree_is_fail_soft(tmp_path):
    root = _make_clone(tmp_path)
    (root / ".review.yml").write_text("max_comments: [unclosed\n", encoding="utf-8")

    assert worktree_drift(str(root), "main").status == "unknown"


def test_committed_source_label_names_the_blob(tmp_path):
    root = _make_clone(tmp_path)
    fetcher = CommittedLayerFetcher("owner/name", clone_path=str(root))

    assert fetcher("main") == "summary_cluster_depth: 3\n"
    assert fetcher.source == "git-blob"
    assert fetcher.clone_root == str(root)
