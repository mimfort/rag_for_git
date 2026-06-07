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
