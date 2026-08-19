"""Сиды контекстного ядра: разбор hunk'ов и сшивка с символами коммита."""
from __future__ import annotations

from eval.solve_task_metrics import context_seeds

DIFF = """diff --git a/reviewer/a.py b/reviewer/a.py
index 111..222 100644
--- a/reviewer/a.py
+++ b/reviewer/a.py
@@ -1,0 +1,2 @@ def f():
+    x = 1
+    y = 2
@@ -5 +5 @@ def g():
-    old()
+    new()
"""

SOURCE = """def f():
    pass


def g():
    pass
"""


def test_parse_hunk_ranges_reads_right_side():
    assert context_seeds.parse_hunk_ranges(DIFF) == [(1, 2), (5, 5)]


def test_parse_hunk_ranges_pure_deletion_marks_the_seam():
    """У чистого удаления длина правой стороны 0; сид — строка стыка, иначе
    удалённый код не имел бы сида вовсе и задача теряла бы знаменатель."""
    diff = "@@ -10,3 +9,0 @@ def f():\n-    a()\n"
    assert context_seeds.parse_hunk_ranges(diff) == [(9, 9)]


def test_parse_hunk_ranges_defaults_length_to_one():
    assert context_seeds.parse_hunk_ranges("@@ -1 +7 @@\n+x\n") == [(7, 7)]


def test_seeds_for_merge_maps_ranges_to_symbols():
    calls = []

    def run_git(args):
        calls.append(args)
        if args[0] == "diff":
            return DIFF
        if args[0] == "show":
            return SOURCE
        raise AssertionError(args)

    seeds = context_seeds.seeds_for_merge("deadbeef", {"reviewer/a.py"}, run_git)
    assert seeds == {"reviewer/a.py#f", "reviewer/a.py#g"}


def test_seeds_for_merge_skips_non_core_paths():
    """Тесты и доки в сиды не идут: линейка та же, что у core-recall."""
    def run_git(args):
        raise AssertionError("git не должен вызываться для не-core путей")

    assert context_seeds.seeds_for_merge("x", {"tests/test_a.py"}, run_git) == set()


def test_seeds_for_merge_survives_git_failure():
    """Файл, удалённый после мержа, git show не отдаст. Прогон корпуса не падает:
    у задачи просто меньше сидов, и это видно по их числу."""
    def run_git(args):
        if args[0] == "diff":
            return DIFF
        raise context_seeds.ground_truth.GitError("no such path")

    assert context_seeds.seeds_for_merge("x", {"reviewer/a.py"}, run_git) == set()


def test_seeds_for_merge_skips_unparsable_source():
    """Не-Python или битый файл не роняет прогон, сидов при этом нет."""
    def run_git(args):
        return DIFF if args[0] == "diff" else "\x00\x01 not python"

    assert context_seeds.seeds_for_merge("x", {"reviewer/a.py"}, run_git) == set()
