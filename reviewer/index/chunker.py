import hashlib

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from reviewer.index.models import Chunk

_PY = Language(tspython.language())
_PARSER = Parser(_PY)

_DEF_TYPES = {"function_definition", "class_definition"}

def chunk_python(path: str, source: bytes) -> list[Chunk]:
    tree = _PARSER.parse(source)
    chunks: list[Chunk] = []

    def name_of(defn) -> str:
        n = defn.child_by_field_name("name")
        return n.text.decode("utf-8") if n is not None else "<anonymous>"

    def visit(node, scope: str, class_scope: bool) -> None:
        for child in node.children:
            defn, outer = child, child
            if child.type == "decorated_definition":
                defn = child.child_by_field_name("definition")
                outer = child          # диапазон с декораторами
            if defn is not None and defn.type in _DEF_TYPES:
                name = name_of(defn)
                fqn = f"{scope}.{name}" if scope else name
                is_class = defn.type == "class_definition"
                kind = "class" if is_class else ("method" if class_scope else "function")
                chunks.append(Chunk(
                    path=path, lang="python", symbol_fqn=fqn, kind=kind,
                    start_line=outer.start_point[0] + 1,
                    end_line=outer.end_point[0] + 1,
                    text=source[outer.start_byte:outer.end_byte].decode("utf-8", "replace"),
                ))
                body = defn.child_by_field_name("body")
                if body is not None:
                    visit(body, fqn, class_scope=is_class)
            else:
                visit(child, scope, class_scope)

    visit(tree.root_node, "", class_scope=False)
    return chunks


def python_skeleton(source: bytes) -> list[int]:
    """AST-скелет файла: 1-based номера строк заголовков def/class (с декораторами,
    многострочные сигнатуры — до ':'), первой строки docstring каждого определения и
    модульного docstring. Рендер (N|код) и кап — на стороне вызывающего (read_file).
    Отсортированный уникальный список; [] если нет ни определений, ни модульного docstring.
    Не падает на битом коде."""
    tree = _PARSER.parse(source)
    lines: set[int] = set()

    def first_docstring_line(body) -> None:
        if body is None:
            return
        for stmt in body.children:
            if not stmt.is_named or stmt.type == "comment":
                continue
            if (stmt.type == "expression_statement" and stmt.children
                    and stmt.children[0].type == "string"):
                lines.add(stmt.children[0].start_point[0] + 1)
            return  # первый значимый statement рассмотрен — дальше docstring быть не может

    first_docstring_line(tree.root_node)  # модульный docstring

    def visit(node) -> None:
        for child in node.children:
            defn, outer = child, child
            if child.type == "decorated_definition":
                defn = child.child_by_field_name("definition")
                outer = child
            if defn is not None and defn.type in _DEF_TYPES:
                colon_line = defn.start_point[0]
                for c in defn.children:
                    if c.type == ":":
                        colon_line = c.start_point[0]
                        break
                for ln in range(outer.start_point[0], colon_line + 1):
                    lines.add(ln + 1)
                body = defn.child_by_field_name("body")
                first_docstring_line(body)
                if body is not None:
                    visit(body)
            else:
                visit(child)

    visit(tree.root_node)
    return sorted(lines)


def symbol_skeleton_hash(text: str) -> str:
    """Хэш структурного скелета символа: сигнатуры def/class (до ':') + 1-я строка docstring.

    Ключ свежести сводок подсистем по структуре (PRI-165): меняется при смене сигнатуры/
    docstring, но НЕ при правке тела. Хэшируется ТЕКСТ строк скелета (rstrip, как content_hash),
    а не их номера, поэтому сдвиг/реордеринг символа в файл не протекают в хэш. Пустой скелет
    (битый код / нет определений) → fallback на нормализованный полный текст (безопасно: любая
    правка ре-стейлит)."""
    nums = python_skeleton(text.encode("utf-8"))      # 1-based строки скелета относительно text
    lines = text.splitlines()
    if nums:
        body = "\n".join(lines[n - 1].rstrip() for n in nums if 1 <= n <= len(lines))
    else:
        body = "\n".join(ln.rstrip() for ln in lines)
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()


def file_skeleton_lines(chunks: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Скелет файла как объединение скелетов его символов-чанков (PRI-245).

    ``chunks`` — пары (start_line, text) чанков ОДНОГО файла. Возвращает
    отсортированные (абсолютный номер строки, текст строки).

    Источник намеренно тот же, из которого считается ``symbol_skeleton_hash``:
    так вход файлового job'а совпадает с тем, что инвалидирует его результат.
    Цена — module-level docstring и код вне символов в чанки не попадают.
    Вложенные символы схлопываются множеством: скелет класса уже содержит
    сигнатуры своих методов.
    """
    out: dict[int, str] = {}
    for start_line, text in chunks:
        lines = text.splitlines()
        for rel in python_skeleton(text.encode("utf-8")):
            if 1 <= rel <= len(lines):
                # first-write-wins: chunk_python отдаёт родителя раньше потомков
                # (pre-order), а у вложенного символа своя первая строка (def/class)
                # начинается сразу с токена, без исходного отступа — текст родителя
                # для той же абсолютной строки полный и приоритетнее.
                out.setdefault(start_line + rel - 1, lines[rel - 1])
    return sorted(out.items())
