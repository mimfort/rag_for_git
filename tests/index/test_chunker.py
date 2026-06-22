from reviewer.index.chunker import chunk_python, python_skeleton

SRC = b'''\
import os

@dec
def top():
    def inner():
        pass
    return inner

class A:
    def method(self):
        pass
'''

def test_extracts_functions_classes_methods_with_ranges():
    chunks = chunk_python("m.py", SRC)
    by_fqn = {c.symbol_fqn: c for c in chunks}
    assert by_fqn["top"].kind == "function"
    assert by_fqn["top"].start_line == 3          # включает строку @dec
    assert by_fqn["top.inner"].kind == "function"
    assert by_fqn["A"].kind == "class"
    assert by_fqn["A.method"].kind == "method"
    assert by_fqn["A.method"].path == "m.py"

def test_content_hash_stable_and_distinct():
    a1 = {c.symbol_fqn: c for c in chunk_python("m.py", SRC)}
    a2 = {c.symbol_fqn: c for c in chunk_python("m.py", SRC)}
    assert a1["A.method"].content_hash == a2["A.method"].content_hash   # стабилен
    assert a1["A.method"].content_hash != a1["top"].content_hash        # различен для разных тел

def test_handles_syntax_errors_without_crashing():
    chunks = chunk_python("bad.py", b"def f(:\n    pass\n")
    assert isinstance(chunks, list)


SKEL_SRC = b'''\
"""Module doc.
more."""
import os

@dec
def top(a,
        b):
    """Top doc."""
    return a + b

class A:
    """Class A."""
    def method(self):
        x = 1
        return x
'''

def test_skeleton_includes_signatures_and_docstrings_not_bodies():
    nums = python_skeleton(SKEL_SRC)
    lines = SKEL_SRC.decode().splitlines()
    picked = [lines[n - 1] for n in nums]
    assert any('"""Module doc.' in v for v in picked)      # модульный docstring (1-я строка)
    assert any("@dec" in v for v in picked)                # декоратор
    assert any("def top(a," in v for v in picked)          # многострочная сигнатура — строка 1
    assert any("b):" in v for v in picked)                 # и строка 2 (до ':')
    assert any('"""Top doc."""' in v for v in picked)      # docstring функции
    assert any("class A:" in v for v in picked)
    assert any('"""Class A."""' in v for v in picked)
    assert any("def method(self):" in v for v in picked)
    assert all("return a + b" not in v for v in picked)    # тела НЕ включены
    assert all("x = 1" not in v for v in picked)
    assert all("return x" not in v for v in picked)
    assert "more." not in "\n".join(picked)                # только 1-я строка модульного docstring

def test_skeleton_empty_for_source_without_definitions():
    assert python_skeleton(b"import os\nX = 1\nprint(X)\n") == []

def test_skeleton_does_not_crash_on_syntax_error():
    assert isinstance(python_skeleton(b"def f(:\n  pass\n"), list)
