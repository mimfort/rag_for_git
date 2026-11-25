"""Ground truth включает только настоящие PR-мержи для точного ключа задачи."""
import subprocess

import pytest

from reviewer.metrics.brief_quality import ground_truth

# Реальная форма граблей PRI-134: sync-мерж с тем же ключом в тексте тащит
# чужие файлы и раздувает знаменатель.
ROWS = [
    ("aaa111", "Merge pull request #148 from mimfort/feature/pri-134-x"),
    ("bbb222", "Merge remote-tracking branch 'origin/dev' into feature/pri-134-x"),
    ("ccc333", "merge: dev в feature/pri-134-x"),
    ("ddd444", "Merge pull request #149 from mimfort/fix/pri-134-followup"),
]


def test_filter_pr_merges_extracts_number():
    rows = [
        ("abc", "Merge pull request #232 from mimfort/feat/pri-275"),
        ("def", "Merge remote-tracking branch 'origin/dev' into feat/pri-275"),
    ]
    merges, skipped = ground_truth.filter_pr_merges(rows)
    assert [(m.sha, m.number) for m in merges] == [("abc", 232)]
    assert skipped == 1


def test_filter_pr_merges_keeps_only_real_pr_merges():
    merges, skipped = ground_truth.filter_pr_merges(ROWS)

    assert [m.sha for m in merges] == ["aaa111", "ddd444"]
    assert skipped == 2


def test_filter_pr_merges_counts_sync_merges_instead_of_silently_dropping():
    _, skipped = ground_truth.filter_pr_merges(ROWS[1:3])

    assert skipped == 2


def test_filter_pr_merges_empty_input():
    assert ground_truth.filter_pr_merges([]) == ([], 0)


def test_changed_status_parses_name_status():
    def run_git(args):
        assert args[:3] == ["diff", "--name-status", "-z"]
        return "M\0reviewer/модуль.py\0A\0reviewer/new.py\0R100\0old.py\0new\tname.py\0"

    assert ground_truth.changed_status("abc", run_git) == {
        "reviewer/модуль.py": "modified",
        "reviewer/new.py": "added",
        "new\tname.py": "renamed",
    }


def test_changed_status_distinguishes_empty_diff_from_git_error():
    def fake_git(args):
        raise ground_truth.GitError("объект недоступен")

    assert ground_truth.changed_status("abc", lambda args: "") == {}
    with pytest.raises(ground_truth.GitError, match="объект недоступен"):
        ground_truth.changed_status("abc", fake_git)


def test_merge_rows_parses_git_output():
    def fake_git(args):
        assert args[0] == "log"
        assert "--merges" in args
        return (
            "aaa111 Merge pull request #148 from mimfort/feature/pri-134-x\n"
            "bbb222 merge: dev в feature/pri-134-x\n"
            "\n"
        )

    assert ground_truth.merge_rows("PRI-134", fake_git) == [
        ("aaa111", "Merge pull request #148 from mimfort/feature/pri-134-x"),
        ("bbb222", "merge: dev в feature/pri-134-x"),
    ]


def test_merge_rows_uses_literal_complete_case_insensitive_keys(tmp_path, commit_snapshot):
    base = commit_snapshot()

    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "-c", "commit.gpgsign=false", *args],
            cwd=tmp_path, capture_output=True, check=True,
        )

    git("switch", "-c", "history")
    cases = [
        (1, "feat/pri-27-short", ""),
        (2, "feat/pri-270-long", ""),
        (3, "feat/xpri-27-prefix", ""),
        (4, "feat/no-key", "Fixes (pRi-27)."),
        (5, "feat/literal", "Fixes [ops+[x]-7]."),
        (6, "feat/regex-lookalike", "Fixes OPS+X-7."),
        (7, "feat/literal-long", "Fixes OPS+[X]-70."),
    ]
    for number, branch, body in cases:
        git("switch", "-c", branch)
        git("commit", "--allow-empty", "-m", "fixture")
        git("switch", "history")
        git("merge", "--no-ff", branch, "-m", f"Merge pull request #{number} from owner/{branch}",
            "-m", body)
    git("switch", "--detach", base)

    run_git = ground_truth.git_runner(tmp_path)

    def matching_prs(key):
        merges, _ = ground_truth.filter_pr_merges(ground_truth.merge_rows(key, run_git))
        return {merge.number for merge in merges}

    assert matching_prs("PRI-27") == {1, 4}
    assert matching_prs("PRI-270") == {2}
    assert matching_prs("OPS+[X]-7") == {5}


def test_changed_files_splits_names():
    def fake_git(args):
        assert args[:2] == ["diff", "--name-only"]
        return "reviewer/a.py\ntests/test_a.py\n\n"

    assert ground_truth.changed_files("aaa111", fake_git) == {
        "reviewer/a.py",
        "tests/test_a.py",
    }


def test_collect_uses_only_pr_merges_for_changed_files():
    calls = []

    def fake_git(args):
        calls.append(args)
        if args[0] == "log":
            return (
                "aaa111 Merge pull request #148 from mimfort/feature/pri-134-x\n"
                "bbb222 Merge remote-tracking branch 'origin/dev' into feature/pri-134-x\n"
            )
        if args[0] == "diff":
            assert args[2] == "aaa111^1"
            return "reviewer/a.py\n"
        raise AssertionError(f"неожиданный вызов git: {args}")

    result = ground_truth.collect("PRI-134", fake_git)

    assert result.merge_shas == ["aaa111"]
    assert [(m.sha, m.number) for m in result.merges] == [("aaa111", 148)]
    assert result.sync_merges_skipped == 1
    assert result.changed == {"reviewer/a.py"}
    assert result.parent_ref == "aaa111^1"
    assert not any(args[0] == "diff" and "bbb222" in args for args in calls)


def test_collect_without_merges_is_empty():
    def fake_git(args):
        return ""

    result = ground_truth.collect("PRI-999", fake_git)

    assert result.merges == []
    assert result.merge_shas == []
    assert result.changed == set()
    assert result.parent_ref is None


def test_path_existed_true_when_git_exits_zero():
    def fake_git(args):
        assert args[:2] == ["cat-file", "-e"]
        return ""

    assert ground_truth.path_existed("aaa111^1", "reviewer/a.py", fake_git) is True


def test_path_existed_false_when_git_raises():
    def fake_git(args):
        raise ground_truth.GitError("нет такого объекта")

    assert ground_truth.path_existed("aaa111^1", "reviewer/new.py", fake_git) is False


def test_path_existed_without_parent_assumes_existing():
    def fake_git(args):
        raise AssertionError("git не должен вызываться без parent_ref")

    assert ground_truth.path_existed(None, "reviewer/a.py", fake_git) is True


def test_collect_counts_diff_failures():
    """Сбой diff'а настоящего PR-мержа считается, а не теряется молча."""

    def fake_git(args):
        if args[0] == "log":
            return "aaa111 Merge pull request #1 from o/b\n"
        raise ground_truth.GitError("объект недоступен")

    truth = ground_truth.collect("PRI-1", fake_git)

    assert truth.changed == set()
    assert truth.diff_failures == 1
