"""Сид-символы контекстного ядра: что дифф задачи реально трогал (PRI-261, PRI-262).

Сиды — НЕ все символы изменённых файлов. Замер: сидирование целыми файлами даёт
медиану 57 новых core-файлов (37 % репозитория, «мусорное ядро»), сидирование
затронутыми символами — 14.5 на том же графе и той же глубине. Разница вчетверо
решается здесь, а не выбором числа хопов.

Символы берутся на КОММИТЕ МЕРЖА, а не из сегодняшнего индекса: номера строк
диффа относятся к своему коммиту, и наложение их на сегодняшние диапазоны чанков
попадает не в те символы у любого файла, который с тех пор менялся. С сегодняшним
графом результат сшивается по имени символа, а не по строке.

PRI-262 сужает гранулярность с чанка до СТРОКИ, потому что ручная сверка PRI-261
не взяла гейт (41.8 % при пороге 50 %) ровно на двух формах мусора:

1. Хунк, не меняющий исполняемого кода (докстринг, комментарий, help-текст),
   тащил весь call-граф своей функции. Лечится ``hunk_is_significant``.
2. Нетронутое тело задетой функции отдавало в ядро своих callee. Лечится
   ``called_names``: имя обязано прозвучать на ИЗМЕНЁННОЙ строке.

Значимость считается не по позиции, а по содержанию: строка ``help="..."``
внутри декоратора ЯВЛЯЕТСЯ кодом, и позиционное правило её бы пропустило.
Сравниваются левый и правый блоки хунка с вымаранными строковыми литералами и
комментариями — равенство означает, что исполняемого не изменилось ничего.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from reviewer.index.chunker import chunk_python
from reviewer.metrics.brief_quality.classify import is_core_production_path
from reviewer.metrics.brief_quality.config import BriefQualityConfig

from . import ground_truth

_PY = Language(tspython.language())
_PARSER = Parser(_PY)

# '@@ -a,b +c,d @@ [контекст]'.
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class Hunk:
    """Один хунк диффа: обе стороны с номерами строк своего коммита."""
    left_start: int
    left_len: int
    right_start: int
    right_len: int

    @property
    def right_range(self) -> tuple:
        """Диапазон правой стороны; у чистого удаления — строка стыка.

        У удалённого кода иначе не было бы сида вовсе, и задача теряла бы
        часть знаменателя молча (инвариант PRI-261).
        """
        if self.right_len == 0:
            return (self.right_start, self.right_start)
        return (self.right_start, self.right_start + self.right_len - 1)

    @property
    def left_range(self) -> tuple:
        if self.left_len == 0:
            return (self.left_start, self.left_start)
        return (self.left_start, self.left_start + self.left_len - 1)


@dataclass(frozen=True)
class SeedSet:
    """Сиды задачи: символы обхода и имена, разрешающие соседа.

    Имена разделены по источнику, а не слиты в одно поле: вклад шапки символа
    в покрытие и в точность обязан быть измерен отдельно (критерий 3 PRI-266),
    и обе стороны замера обязаны сниматься одним и тем же кодом.
    """
    symbols: set = field(default_factory=set)
    called_names: set = field(default_factory=set)
    signature_names: set = field(default_factory=set)

    def __or__(self, other: "SeedSet") -> "SeedSet":
        return SeedSet(self.symbols | other.symbols,
                       self.called_names | other.called_names,
                       self.signature_names | other.signature_names)


def parse_hunks(diff_text: str) -> list:
    """Хунки диффа с номерами строк обеих сторон."""
    hunks: list = []
    for line in (diff_text or "").splitlines():
        match = _HUNK_RE.match(line)
        if not match:
            continue
        hunks.append(Hunk(
            left_start=int(match.group(1)),
            left_len=int(match.group(2)) if match.group(2) is not None else 1,
            right_start=int(match.group(3)),
            right_len=int(match.group(4)) if match.group(4) is not None else 1,
        ))
    return hunks


def parse_hunk_ranges(diff_text: str) -> list:
    """Диапазоны строк правой стороны диффа: [(start, end), ...]."""
    return [h.right_range for h in parse_hunks(diff_text)]


def _masked_lines(source: str) -> list:
    """Строки файла с вымаранными строковыми литералами и комментариями.

    Пробелы схлопываются: правка внутри литерала меняет длину строки, и без
    нормализации две одинаковые по коду строки различались бы отступом.

    У f-строк вымарывается только ``string_content``: выражение в ``{...}`` —
    настоящий код, и стирать его вместе с текстом значило бы не заметить
    правку интерполяции.
    """
    lines = source.splitlines()
    if not lines:
        return []
    grid = [list(line) for line in lines]

    def blank(node) -> None:
        (r0, c0), (r1, c1) = node.start_point, node.end_point
        for row in range(r0, min(r1 + 1, len(grid))):
            start = c0 if row == r0 else 0
            end = c1 if row == r1 else len(grid[row])
            for col in range(start, min(end, len(grid[row]))):
                grid[row][col] = " "

    def visit(node) -> None:
        if node.type == "comment":
            blank(node)
            return
        if node.type == "string":
            contents = [c for c in node.children if c.type == "string_content"]
            if contents:
                for child in contents:
                    blank(child)
            else:
                blank(node)
            for child in node.children:
                if child.type == "interpolation":
                    visit(child)
            return
        for child in node.children:
            visit(child)

    try:
        visit(_PARSER.parse(source.encode("utf-8", "replace")).root_node)
    except Exception:  # noqa: BLE001 — битый файл не роняет прогон
        return [" ".join(line.split()) for line in lines]
    return [" ".join("".join(row).split()) for row in grid]


def _block(masked: list, start: int, length: int) -> list:
    """Непустые нормализованные строки блока (1-based, length может быть 0)."""
    if length <= 0:
        return []
    lo, hi = max(start, 1), min(start + length - 1, len(masked))
    return [masked[i - 1] for i in range(lo, hi + 1) if masked[i - 1]]


def hunk_is_significant(hunk: Hunk, before_masked: list, after_masked: list) -> bool:
    """Изменил ли хунк исполняемый код.

    Сравниваются вымаранные блоки обеих сторон: если после удаления литералов
    и комментариев они совпали, изменилось только то, что код не исполняет —
    докстринг, комментарий, help-текст или форматирование.
    """
    left = _block(before_masked, hunk.left_start, hunk.left_len)
    right = _block(after_masked, hunk.right_start, hunk.right_len)
    return left != right


def _innermost_symbols(path: str, source: str, lines: set) -> set:
    """Самый внутренний символ, содержащий каждую из строк.

    ``chunk_python`` вкладывает чанки: у метода есть и свой чанк, и чанк класса.
    Сид по обоим означал бы, что хунк в одном методе god-класса тянет исходящие
    рёбра всего класса (измеренный отказ PRI-221: 6 осмысленных путей из 30).
    """
    if not lines:
        return set()
    try:
        chunks = chunk_python(path, source.encode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — не-Python или битый файл не роняет прогон
        return set()
    hits: set = set()
    for line in lines:
        covering = [c for c in chunks if c.start_line <= line <= c.end_line]
        if covering:
            best = min(covering, key=lambda c: (c.end_line - c.start_line, c.symbol_fqn))
            hits.add(f"{path}#{best.symbol_fqn}")
    return hits


def _simple_name(node) -> str:
    """Последний идентификатор выражения: у 'a.b.c' — 'c'."""
    while node is not None and node.type == "attribute":
        node = node.child_by_field_name("attribute")
    return node.text.decode("utf-8", "replace") if node is not None else ""


def called_names(source: str, lines: set) -> set:
    """Имена, вызванные или унаследованные НА изменённых строках.

    Ровно те формы, из которых граф строит рёбра: вызовы (CALLS, включая
    декораторы) и базы классов (IMPLEMENTS). Идентификаторы вообще брать
    нельзя: имя локальной переменной пропустило бы в ядро посторонний файл.
    """
    if not lines:
        return set()
    names: set = set()

    def visit(node) -> None:
        if node.type == "call":
            func = node.child_by_field_name("function")
            if func is not None and func.end_point[0] + 1 in lines:
                name = _simple_name(func)
                if name:
                    names.add(name)
        elif node.type == "class_definition":
            supers = node.child_by_field_name("superclasses")
            if supers is not None:
                for child in supers.children:
                    if child.type in ("identifier", "attribute") and \
                            child.end_point[0] + 1 in lines:
                        name = _simple_name(child)
                        if name:
                            names.add(name)
        for child in node.children:
            visit(child)

    try:
        visit(_PARSER.parse(source.encode("utf-8", "replace")).root_node)
    except Exception:  # noqa: BLE001
        return set()
    return names


def _def_node_at(root, start_line: int):
    """Узел определения (`def`/`class`), чей ЧАНК начинается на данной строке.

    Сопоставление по строке, а не по имени: имя может повторяться в файле, а
    `chunk_python` уже выбрал нужный символ. Сравнивается строка охватывающего
    `decorated_definition`, если он есть: `chunk_python` включает декораторы в
    `start_line`, а `function_definition` начинается со слова `def`, и без
    этого ни один декорированный символ не нашёлся бы.
    """
    found = []

    def visit(node) -> None:
        if node.type in ("function_definition", "class_definition"):
            parent = node.parent
            outer = parent if parent is not None and \
                parent.type == "decorated_definition" else node
            if outer.start_point[0] + 1 == start_line:
                found.append(node)
        for child in node.children:
            visit(child)

    visit(root)
    return found[0] if found else None


def _names_in(node) -> set:
    """Простые имена всех идентификаторов и атрибутов поддерева.

    В шапке символа идентификатор — это тип, дефолт или декоратор, то есть
    ровно та зависимость, которую граф выражает ребром. Запрет `called_names`
    на голые идентификаторы здесь не нужен: локальных переменных в шапке нет.
    """
    names: set = set()

    def visit(child) -> None:
        if child.type in ("identifier", "attribute"):
            name = _simple_name(child)
            if name:
                names.add(name)
            return
        for sub in child.children:
            visit(sub)

    visit(node)
    return names


def _param_parts(parameters) -> list:
    """Части параметров, называющие зависимость: аннотации и дефолты.

    ИМЯ параметра не берётся: оно локально для функции, ребром графа не
    выражается, и в фильтре имён пропустило бы посторонний файл — тот же
    запрет, по которому `called_names` не берёт голые идентификаторы.
    """
    parts = []
    for child in parameters.children:
        for field_name in ("type", "value"):
            node = child.child_by_field_name(field_name)
            if node is not None:
                parts.append(node)
    return parts


def _signature_parts(node) -> list:
    """Части шапки определения: декораторы, аннотации и дефолты параметров,
    тип возврата, базы класса."""
    parts = []
    parent = node.parent
    if parent is not None and parent.type == "decorated_definition":
        parts.extend(c for c in parent.children if c.type == "decorator")
    parameters = node.child_by_field_name("parameters")
    if parameters is not None:
        parts.extend(_param_parts(parameters))
    for field_name in ("return_type", "superclasses"):
        child = node.child_by_field_name(field_name)
        if child is not None:
            parts.append(child)
    return parts


def signature_names(source: str, symbols: set) -> set:
    """Имена из шапок символов-сидов: декораторы, аннотации, дефолты, базы.

    Второй источник разрешённых имён (PRI-266). Фильтр по именам с изменённых
    строк не видит зависимость, которую изменённые строки не называют: PRI-251
    потеряла chunker/models/gitutil, потому что ведущие к ним вызовы лежат на
    нетронутых строках. Шапка изменённого символа такую зависимость называет.

    Тело символа не читается: оттуда и приходил мусор god-модулей, который
    чинила PRI-262. Сиды этой функцией не расширяются — только фильтр.
    """
    if not symbols or not source:
        return set()
    names: set = set()
    try:
        chunks = chunk_python("x.py", source.encode("utf-8", "replace"))
        root = _PARSER.parse(source.encode("utf-8", "replace")).root_node
    except Exception:  # noqa: BLE001 — не-Python или битый файл не роняет прогон
        return set()
    wanted = {sym.split("#", 1)[1] for sym in symbols if "#" in sym}
    for chunk in chunks:
        if chunk.symbol_fqn not in wanted:
            continue
        node = _def_node_at(root, chunk.start_line)
        if node is None:
            continue
        for part in _signature_parts(node):
            names |= _names_in(part)
    return names


def _lines_of(rng: tuple) -> set:
    return set(range(rng[0], rng[1] + 1))


def seeds_for_merge(sha: str, core_paths: set, run_git, config: BriefQualityConfig) -> SeedSet:
    """Сид-символы и имена одного PR-мержа по его core-путям."""
    result = SeedSet()
    for path in sorted(p for p in core_paths if is_core_production_path(p, config)):
        try:
            diff = run_git(["diff", "--unified=0", f"{sha}^1", sha, "--", path])
            after = run_git(["show", f"{sha}:{path}"])
        except ground_truth.GitError:
            # Путь удалён или недостижим на этом коммите: сидов меньше, прогон жив.
            continue
        try:
            before = run_git(["show", f"{sha}^1:{path}"])
        except ground_truth.GitError:
            # Файл создан этим мержем: левой стороны нет, всё добавленное значимо.
            before = ""

        hunks = parse_hunks(diff)
        if not hunks:
            continue
        before_masked, after_masked = _masked_lines(before), _masked_lines(after)

        right_lines: set = set()
        left_lines: set = set()
        for hunk in hunks:
            if not hunk_is_significant(hunk, before_masked, after_masked):
                continue
            right_lines |= _lines_of(hunk.right_range)
            left_lines |= _lines_of(hunk.left_range)

        symbols = _innermost_symbols(path, after, right_lines)
        result = result | SeedSet(
            symbols=symbols,
            called_names=called_names(after, right_lines)
            | called_names(before, left_lines),
            signature_names=signature_names(after, symbols),
        )
    return result


def collect_seeds(truth, run_git, config: BriefQualityConfig) -> SeedSet:
    """Сиды задачи: объединение по всем её настоящим PR-мержам."""
    core_paths = {p for p in truth.changed if is_core_production_path(p, config)}
    result = SeedSet()
    for sha in truth.merge_shas:
        result = result | seeds_for_merge(sha, core_paths, run_git, config)
    return result
