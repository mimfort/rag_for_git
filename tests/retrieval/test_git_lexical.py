"""Git-снимок и общий файловый бюджет секции code (PRI-298)."""
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from eval.solve_task_metrics.context_paths import extract_context_paths
from reviewer.index.store import Retrieved
from reviewer.policy.context_limits import CodeSectionLimits
from reviewer.retrieval.augment import AugmentSource
from reviewer.retrieval.git_lexical import read_git_blob, resolve_git_commit, search_git
from reviewer.retrieval.multiquery import reserve_git_excerpts, search_multi
from reviewer.retrieval.retriever import ContextPack


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _write(repo: Path, path: str, content: str | bytes) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content.encode() if isinstance(content, str) else content)


@pytest.fixture
def git_repo(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "PRI-298 test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    return tmp_path


def _commit(repo: Path) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "snapshot")
    return _git(repo, "rev-parse", "HEAD")


def test_git_excerpts_come_from_requested_commit_and_keep_visible_lines(git_repo):
    _write(git_repo, "schema.sql", "first\nsecond\nTARGET UNIQUE\nfourth\nfifth\n")
    sha = _commit(git_repo)
    _git(git_repo, "branch", "frozen")
    _write(git_repo, "schema.sql", "new commit\n")
    _commit(git_repo)
    _write(git_repo, "schema.sql", "TARGET from worktree")
    _write(git_repo, "untracked.sql", "TARGET")

    [item] = search_git(git_repo, "frozen", "target UNIQUE", ignore=[], limit=3,
                        chars_per_file=24)
    assert item.node_id == f"schema.sql#git-lexical@{sha}"
    assert item.start_line == 3
    assert item.end_line == 4
    assert item.text == "TARGET UNIQUE\nfourth"
    rendered = ContextPack([item]).as_context(line_numbers=True)
    assert extract_context_paths(rendered) == {"schema.sql"}
    assert "    3 | TARGET UNIQUE\n    4 | fourth" in rendered


def test_git_scope_obeys_ignore_and_skips_binary_symlink_submodule_and_test_paths(git_repo):
    eligible = {
        "schema": "schema.sql", "compose": "compose.yml", "pipeline": "pipeline.yaml",
        "settings": "settings.toml", "example": "plugin/skills/example/SKILL.md",
    }
    excluded = {
        "probe": "eval/probe.md", "plan": "docs/superpowers/plan.md",
        "fixture": "sub/tests/fixture.yml", "sample": "test_sample.sql",
        "ignored": "ignored/nested/config.yaml", "generated": "skip.gen.toml",
        "python": "reviewer/base.py",
    }
    for term, path in (eligible | excluded | {"readme": "README.md"}).items():
        _write(git_repo, path, term)
    _write(git_repo, "binary.sql", b"binary\0more")
    _write(git_repo, "undecodable.sql", b"undecodable\xff")
    sha = _commit(git_repo)
    symlink_blob = _git(git_repo, "hash-object", "-w", "schema.sql")
    _git(git_repo, "update-index", "--add", "--cacheinfo", f"120000,{symlink_blob},symlink.sql")
    _git(git_repo, "update-index", "--chmod=+x", "schema.sql")
    _git(git_repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},submodule.sql")
    _git(git_repo, "commit", "-m", "gitlink")

    for wide in (False, True):
        expected = eligible | ({"readme": "README.md"} if wide else {})
        for query in (*eligible, *excluded, "readme", "binary", "undecodable",
                      "symlink", "submodule"):
            actual = search_git(
                git_repo, "HEAD", query, ignore=["ignored", "*.gen.toml"], limit=3,
                chars_per_file=120, wide=wide,
            )
            assert [item.path for item in actual] == ([expected[query]] if query in expected else [])


def test_missing_blob_outside_candidate_scope_does_not_abort_search(git_repo):
    _write(git_repo, "missing.py", "missing Python blob")
    _write(git_repo, "schema.sql", "create table sample")
    _commit(git_repo)
    oid = _git(git_repo, "rev-parse", "HEAD:missing.py")
    (git_repo / ".git" / "objects" / oid[:2] / oid[2:]).unlink()

    [item] = search_git(git_repo, "HEAD", "sample", ignore=[], limit=3, chars_per_file=50)
    assert item.path == "schema.sql"


def test_committed_policy_read_ignores_replacements_and_distinguishes_absent_path(git_repo):
    _write(git_repo, ".review.yml", "paths:\n  ignore: [local]\n")
    sha = _commit(git_repo)
    _git(git_repo, "branch", "frozen")
    _write(git_repo, ".review.yml", "paths:\n  ignore: []\n")
    replacement = _commit(git_repo)
    _git(git_repo, "replace", sha, replacement)

    assert resolve_git_commit(git_repo, "frozen") == sha
    assert read_git_blob(git_repo, sha, ".review.yml") == "paths:\n  ignore: [local]\n"
    assert read_git_blob(git_repo, sha, "absent.yml") is None


def test_git_files_rank_by_weighted_path_with_stable_ties_and_at_most_three_results(git_repo):
    _write(git_repo, "focus.sql", "one")
    for name in ("aa", "bb", "cc", "dd"):
        _write(git_repo, f"{name}.sql", "focus")
    _commit(git_repo)

    items = search_git(git_repo, "HEAD", "FOCUS", ignore=[], limit=99, chars_per_file=20)
    assert [item.path for item in items] == ["focus.sql", "aa.sql", "bb.sql"]


def test_git_fragment_uses_complete_matching_line_when_previous_match_does_not_fit(git_repo):
    _write(git_repo, "schema.sql", "match " + 90 * "z" + "\nmatch line\nspare\n")
    _write(git_repo, "long-only.sql", "match " + 90 * "z")
    _commit(git_repo)

    [item] = search_git(git_repo, "HEAD", "match", ignore=[], limit=3, chars_per_file=10)
    assert (item.start_line, item.end_line, item.text) == (2, 2, "match line")


def test_git_search_skips_large_blobs_even_if_they_would_rank(git_repo):
    _write(git_repo, "focus.sql", "focus\n" * 50_000)
    _write(git_repo, "schema.sql", "focus")
    _commit(git_repo)

    [item] = search_git(git_repo, "HEAD", "focus", ignore=[], limit=3, chars_per_file=10)
    assert item.path == "schema.sql"


def test_git_noop_keeps_missing_clone_unopened_and_returns_empty_without_matches(git_repo):
    _write(git_repo, "schema.sql", "create table sample")
    _commit(git_repo)
    assert search_git(git_repo / "missing", "HEAD", "create", ignore=[], limit=0,
                      chars_per_file=50) == []
    assert search_git(git_repo, "HEAD", "unmatched", ignore=[], limit=3,
                      chars_per_file=50) == []
    with pytest.raises(subprocess.CalledProcessError):
        search_git(git_repo / "missing", "HEAD", "create", ignore=[], limit=3, chars_per_file=50)


def _item(path: str, start: int = 1) -> Retrieved:
    return Retrieved(
        node_id=f"{path}#{start}", path=path, symbol_fqn="", kind="",
        start_line=start, end_line=start, text="visible", score=1.0, bm25_hit=True,
    )


def test_git_slot_reservation_deduplicates_and_displaces_whole_files():
    a, b, c = (_item(f"{name}.py") for name in "abc")
    a2, c2 = _item("a.py", 5), _item("c.py", 5)
    x, y = _item("x.sql"), _item("y.yml")
    assert reserve_git_excerpts(
        [a, b, a2, c, c2], [a, x, x, y], max_files=3, max_chunks_per_file=2,
    ) == [a, a2, x, y]


def test_git_preserves_reserved_similar_diff_slot_and_reports_displacement():
    a, b, c, d, augmented = (_item(f"{name}.py") for name in "abcdx")
    git_items = [_item("schema.sql"), _item("compose.yml")]
    retriever = SimpleNamespace(
        graph=None,
        embedder=SimpleNamespace(embed_queries=lambda queries: [[0.1] for _ in queries]),
        store=SimpleNamespace(
            hybrid_search=lambda *args, **kwargs: [a, b, c, d],
            fetch_retrieved_at_paths=lambda *args, **kwargs: [augmented],
        ),
    )
    pack = search_multi(
        retriever, "owner/repo", ["query"], section_limits=CodeSectionLimits(max_files=4),
        augment_sources=[AugmentSource("similar-diffs", [augmented.path], 1)],
        git_excerpts=git_items, include_selection_stats=True,
    )
    assert [item.path for item in pack.items] == [
        "a.py", "schema.sql", "compose.yml", "x.py",
    ]
    assert pack.selection_stats == {
        "file_limit": 4,
        "candidate_files": 4,
        "omitted_by_file_limit": 0,
        "displaced_by_augmentation": 1,
        "displaced_by_git_lexical": 2,
        "git_lexical_files": 2,
        "selected_files": 4,
    }
