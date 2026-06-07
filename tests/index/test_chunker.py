from reviewer.index.chunker import chunk_python

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
