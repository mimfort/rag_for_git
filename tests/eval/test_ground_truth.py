"""Unit-тесты ground truth: только настоящие PR-мержи считаются работой задачи."""
from eval.solve_task_metrics import ground_truth as gt

# Реальная форма граблей PRI-134: sync-мерж с тем же ключом в тексте тащит
# чужие файлы и раздувает знаменатель.
ROWS = [
    ("aaa111", "Merge pull request #148 from mimfort/feature/pri-134-x"),
    ("bbb222", "Merge remote-tracking branch 'origin/dev' into feature/pri-134-x"),
    ("ccc333", "merge: dev в feature/pri-134-x"),
    ("ddd444", "Merge pull request #149 from mimfort/fix/pri-134-followup"),
]


def test_filter_pr_merges_keeps_only_real_pr_merges():
    shas, skipped = gt.filter_pr_merges(ROWS)

    assert shas == ["aaa111", "ddd444"]
    assert skipped == 2


def test_filter_pr_merges_counts_sync_merges_instead_of_silently_dropping():
    _, skipped = gt.filter_pr_merges(ROWS[1:3])

    assert skipped == 2


def test_filter_pr_merges_empty_input():
    assert gt.filter_pr_merges([]) == ([], 0)


def test_merge_rows_parses_git_output():
    def fake_git(args):
        assert args[0] == "log"
        assert "--merges" in args
        return (
            "aaa111 Merge pull request #148 from mimfort/feature/pri-134-x\n"
            "bbb222 merge: dev в feature/pri-134-x\n"
            "\n"
        )

    assert gt.merge_rows("PRI-134", fake_git) == [
        ("aaa111", "Merge pull request #148 from mimfort/feature/pri-134-x"),
        ("bbb222", "merge: dev в feature/pri-134-x"),
    ]


def test_changed_files_splits_names():
    def fake_git(args):
        assert args[:2] == ["diff", "--name-only"]
        return "reviewer/a.py\ntests/test_a.py\n\n"

    assert gt.changed_files("aaa111", fake_git) == {"reviewer/a.py", "tests/test_a.py"}


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

    result = gt.collect("PRI-134", fake_git)

    assert result.merge_shas == ["aaa111"]
    assert result.sync_merges_skipped == 1
    assert result.changed == {"reviewer/a.py"}
    assert result.parent_ref == "aaa111^1"
    assert not any(args[0] == "diff" and "bbb222" in args for args in calls)


def test_collect_without_merges_is_empty():
    def fake_git(args):
        return ""

    result = gt.collect("PRI-999", fake_git)

    assert result.merge_shas == []
    assert result.changed == set()
    assert result.parent_ref is None


def test_path_existed_true_when_git_exits_zero():
    def fake_git(args):
        assert args[:2] == ["cat-file", "-e"]
        return ""

    assert gt.path_existed("aaa111^1", "reviewer/a.py", fake_git) is True


def test_path_existed_false_when_git_raises():
    def fake_git(args):
        raise gt.GitError("нет такого объекта")

    assert gt.path_existed("aaa111^1", "reviewer/new.py", fake_git) is False


def test_path_existed_without_parent_assumes_existing():
    def fake_git(args):
        raise AssertionError("git не должен вызываться без parent_ref")

    assert gt.path_existed(None, "reviewer/a.py", fake_git) is True


def test_collect_counts_diff_failures():
    """Сбой diff'а настоящего PR-мержа считается, а не теряется молча."""

    def fake_git(args):
        if args[0] == "log":
            return "aaa111 Merge pull request #1 from o/b\n"
        raise gt.GitError("объект недоступен")

    truth = gt.collect("PRI-1", fake_git)

    assert truth.changed == set()
    assert truth.diff_failures == 1
