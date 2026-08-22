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
    x = 1

def g():
    new()
"""

# Левая сторона тех же хунков. Фикстура обязана отличаться от правой: значимость
# считается сравнением содержимого, и один блоб на обе стороны означал бы «ничего
# не изменилось» — то есть тест мерил бы не то, что заявляет.
SOURCE_BEFORE = """def f():
    pass

def g():
    old()
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
            return SOURCE_BEFORE if "^1" in args[1] else SOURCE
        raise AssertionError(args)

    seeds = context_seeds.seeds_for_merge("deadbeef", {"reviewer/a.py"}, run_git)
    assert seeds.symbols == {"reviewer/a.py#f", "reviewer/a.py#g"}


def test_seeds_for_merge_skips_non_core_paths():
    """Тесты и доки в сиды не идут: линейка та же, что у core-recall."""
    def run_git(args):
        raise AssertionError("git не должен вызываться для не-core путей")

    assert context_seeds.seeds_for_merge("x", {"tests/test_a.py"}, run_git).symbols == set()


def test_seeds_for_merge_survives_git_failure():
    """Файл, удалённый после мержа, git show не отдаст. Прогон корпуса не падает:
    у задачи просто меньше сидов, и это видно по их числу."""
    def run_git(args):
        if args[0] == "diff":
            return DIFF
        raise context_seeds.ground_truth.GitError("no such path")

    assert context_seeds.seeds_for_merge("x", {"reviewer/a.py"}, run_git).symbols == set()


def test_seeds_for_merge_skips_unparsable_source():
    """Не-Python или битый файл не роняет прогон, сидов при этом нет."""
    def run_git(args):
        return DIFF if args[0] == "diff" else "\x00\x01 not python"

    assert context_seeds.seeds_for_merge("x", {"reviewer/a.py"}, run_git).symbols == set()


# --- PRI-262: значимость хунка, внутренний символ, имена вызовов ---

DOCSTRING_BEFORE = '''def f():
    """Старый текст."""
    return helper()
'''

DOCSTRING_AFTER = '''def f():
    """Новый текст."""
    return helper()
'''

DOCSTRING_DIFF = """@@ -2 +2 @@ def f():
-    \"\"\"Старый текст.\"\"\"
+    \"\"\"Новый текст.\"\"\"
"""


def _git(before: str, after: str, diff: str):
    def run_git(args):
        if args[0] == "diff":
            return diff
        if args[0] == "show":
            return before if args[1].count("^1") else after
        raise AssertionError(args)
    return run_git


def test_docstring_only_hunk_yields_no_seed():
    """PRI-227 в чистом виде: переименование докстринга не меняет исполняемого
    кода, поэтому не имеет права тащить call-граф своей функции."""
    result = context_seeds.seeds_for_merge(
        "sha", {"reviewer/a.py"}, _git(DOCSTRING_BEFORE, DOCSTRING_AFTER, DOCSTRING_DIFF)
    )
    assert result.symbols == set()
    assert result.called_names == set()


COMMENT_DIFF = """@@ -2 +2 @@ def f():
-    # старый комментарий
+    # новый комментарий
"""

COMMENT_BEFORE = "def f():\n    # старый комментарий\n    return helper()\n"
COMMENT_AFTER = "def f():\n    # новый комментарий\n    return helper()\n"


def test_comment_only_hunk_yields_no_seed():
    result = context_seeds.seeds_for_merge(
        "sha", {"reviewer/a.py"}, _git(COMMENT_BEFORE, COMMENT_AFTER, COMMENT_DIFF)
    )
    assert result.symbols == set()


HELP_BEFORE = '''import click


@click.option("--path", help="Старый help.")
def config_show(path):
    fetcher = CommittedLayerFetcher()
    return fetcher.read(path)
'''

HELP_AFTER = '''import click


@click.option("--path", help="Новый help.")
def config_show(path):
    fetcher = CommittedLayerFetcher()
    return fetcher.read(path)
'''

HELP_DIFF = """@@ -4 +4 @@
-@click.option("--path", help="Старый help.")
+@click.option("--path", help="Новый help.")
"""


def test_help_text_only_hunk_yields_no_seed():
    """PRI-236: строка ЯВЛЯЕТСЯ кодом (декоратор-вызов), но исполняемого в ней
    ничего не изменилось. Позиционное правило тут не спасает — нужен разбор
    содержимого с вымаранными строковыми литералами."""
    result = context_seeds.seeds_for_merge(
        "sha", {"reviewer/cli.py"}, _git(HELP_BEFORE, HELP_AFTER, HELP_DIFF)
    )
    assert result.symbols == set()


NESTED_BEFORE = '''class Service:
    def a(self):
        return old()

    def b(self):
        return other()
'''

NESTED_AFTER = '''class Service:
    def a(self):
        return fresh()

    def b(self):
        return other()
'''

NESTED_DIFF = """@@ -3 +3 @@ class Service:
-        return old()
+        return fresh()
"""


def test_seeds_innermost_symbol_only():
    """chunk_python отдаёт и класс, и метод. Сид по обоим означал бы, что хунк в
    одном методе god-класса тянет исходящие рёбра всего класса."""
    result = context_seeds.seeds_for_merge(
        "sha", {"reviewer/svc.py"}, _git(NESTED_BEFORE, NESTED_AFTER, NESTED_DIFF)
    )
    assert result.symbols == {"reviewer/svc.py#Service.a"}


def test_called_names_come_only_from_changed_lines():
    """Вызовы нетронутых строк той же функции в множество не попадают."""
    result = context_seeds.seeds_for_merge(
        "sha", {"reviewer/svc.py"}, _git(NESTED_BEFORE, NESTED_AFTER, NESTED_DIFF)
    )
    assert "fresh" in result.called_names
    assert "other" not in result.called_names


DELETE_CODE_DIFF = "@@ -3,1 +2,0 @@ def f():\n-    return helper()\n"
DELETE_COMMENT_DIFF = "@@ -2,1 +1,0 @@ def f():\n-    # комментарий\n"


def test_pure_deletion_of_code_still_seeds():
    """У удалённого кода иначе не было бы сида вовсе (инвариант PRI-261)."""
    before = "def f():\n    # комментарий\n    return helper()\n"
    after = "def f():\n    pass\n"
    result = context_seeds.seeds_for_merge(
        "sha", {"reviewer/a.py"}, _git(before, after, DELETE_CODE_DIFF)
    )
    assert result.symbols == {"reviewer/a.py#f"}


def test_pure_deletion_of_comment_does_not_seed():
    before = "def f():\n    # комментарий\n    return helper()\n"
    after = "def f():\n    return helper()\n"
    result = context_seeds.seeds_for_merge(
        "sha", {"reviewer/a.py"}, _git(before, after, DELETE_COMMENT_DIFF)
    )
    assert result.symbols == set()


def test_inheritance_base_counts_as_called_name():
    """IMPLEMENTS-ребро тоже обязано иметь имя на изменённой строке, иначе
    смена базы не дала бы прочитать контракт."""
    before = "class A(Old):\n    pass\n"
    after = "class A(Fresh):\n    pass\n"
    diff = "@@ -1 +1 @@\n-class A(Old):\n+class A(Fresh):\n"
    result = context_seeds.seeds_for_merge(
        "sha", {"reviewer/a.py"}, _git(before, after, diff)
    )
    assert result.symbols == {"reviewer/a.py#A"}
    assert "Fresh" in result.called_names


SIG_SOURCE = '''import functools

@functools.cache
def f(a: Chunk, b=make_default()) -> Report:
    helper()
    return None


class C(Base):
    def m(self, x: Other) -> None:
        inner()
'''


def test_signature_names_takes_decorator_annotations_defaults_and_return():
    """Шапка символа: декоратор, аннотация параметра, дефолт, тип возврата."""
    names = context_seeds.signature_names(SIG_SOURCE, {"reviewer/a.py#f"})
    assert {"cache", "Chunk", "make_default", "Report"} <= names


def test_signature_names_ignores_the_body():
    """Имя из ТЕЛА символа в шапку не попадает — иначе рычаг возвращает
    мусор god-модулей, ровно тот, который чинила PRI-262."""
    names = context_seeds.signature_names(SIG_SOURCE, {"reviewer/a.py#f"})
    assert "helper" not in names


def test_signature_names_takes_class_bases():
    names = context_seeds.signature_names(SIG_SOURCE, {"reviewer/a.py#C"})
    assert "Base" in names
    assert "inner" not in names


def test_signature_names_reads_method_signature_by_fqn():
    names = context_seeds.signature_names(SIG_SOURCE, {"reviewer/a.py#C.m"})
    assert "Other" in names
    assert "inner" not in names


def test_signature_names_folds_dotted_names_and_skips_parameter_names():
    """`mod.pkg.Thing` сворачивается до `Thing`; ИМЯ параметра `a` в шапку не
    попадает — оно локально для функции и ребром графа не выражается.
    `None` в аннотации возврата — узел `none`, а не идентификатор, поэтому в
    множество не попадает и он."""
    source = "def f(a: mod.pkg.Thing) -> None:\n    pass\n"
    assert context_seeds.signature_names(source, {"reviewer/a.py#f"}) == {"Thing"}


def test_signature_names_unknown_symbol_is_empty_not_error():
    assert context_seeds.signature_names(SIG_SOURCE, {"reviewer/a.py#nope"}) == set()


def test_signature_names_broken_source_is_empty_not_error():
    """Не-Python или битый файл не роняет прогон корпуса."""
    assert context_seeds.signature_names("{ not python", {"reviewer/a.py#f"}) == set()
    assert context_seeds.signature_names("", set()) == set()


def test_seed_set_union_merges_signature_names():
    left = context_seeds.SeedSet(symbols={"a#f"}, signature_names={"X"})
    right = context_seeds.SeedSet(called_names={"g"}, signature_names={"Y"})
    merged = left | right
    assert merged.symbols == {"a#f"}
    assert merged.called_names == {"g"}
    assert merged.signature_names == {"X", "Y"}
