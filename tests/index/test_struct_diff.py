from reviewer.index.struct_diff import (
    SymbolChange,
    diff_symbols,
    extract_signature,
    format_struct_summary,
)


def test_extract_signature_reexported_and_works():
    assert extract_signature("def f(a, b):\n    return a") == "def f(a, b):"
    assert extract_signature("class A(B, C):\n    pass") == "class A(B, C):"
    assert extract_signature("x = 1") is None


def _kinds(changes):
    return {(c.kind, c.fqn) for c in changes}


def test_diff_signature_changed():
    base = b"def foo(a):\n    return a\n"
    head = b"def foo(a, b):\n    return a\n"
    changes = diff_symbols("m.py", base, head)
    assert len(changes) == 1
    c = changes[0]
    assert c.kind == "signature_changed"
    assert c.fqn == "foo"
    assert c.old_sig == "def foo(a):"
    assert c.new_sig == "def foo(a, b):"


def test_diff_added_and_removed():
    base = b"def gone():\n    pass\n"
    head = b"def fresh():\n    pass\n"
    changes = diff_symbols("m.py", base, head)
    assert _kinds(changes) == {("added", "fresh"), ("removed", "gone")}
    by_kind = {c.kind: c for c in changes}
    assert by_kind["added"].new_sig == "def fresh():"
    assert by_kind["added"].old_sig is None
    assert by_kind["removed"].old_sig == "def gone():"
    assert by_kind["removed"].new_sig is None


def test_diff_body_only_change_not_reported():
    base = b"def foo(a):\n    return a\n"
    head = b"def foo(a):\n    return a + 1\n"
    assert diff_symbols("m.py", base, head) == []


def test_diff_method_and_class_kinds():
    base = b"class A:\n    def m(self):\n        return 1\n"
    head = b"class A:\n    def m(self, x):\n        return 1\n"
    changes = diff_symbols("m.py", base, head)
    assert _kinds(changes) == {("signature_changed", "A.m")}
    assert changes[0].symbol_kind == "method"


def test_diff_base_none_means_all_added():
    head = b"def foo(a):\n    pass\n\ndef bar():\n    pass\n"
    changes = diff_symbols("m.py", None, head)
    assert _kinds(changes) == {("added", "foo"), ("added", "bar")}


def test_diff_broken_source_fail_soft():
    # битый/неполный исходник не должен бросать исключение, а вернуть список
    result = diff_symbols("m.py", b"def (:\n", b"def foo(:\n")
    assert isinstance(result, list)


def test_format_empty_returns_empty_string():
    assert format_struct_summary([]) == ""


def test_format_renders_all_kinds_in_order():
    changes = [
        SymbolChange("removed", "old_fn", "function", "def old_fn():", None, 5),
        SymbolChange("added", "A.new_m", "method", None, "def new_m(self):", 12),
        SymbolChange("signature_changed", "foo", "function",
                     "def foo(a):", "def foo(a, b):", 1),
    ]
    out = format_struct_summary(changes)
    assert out.startswith("Структурный diff:")
    # порядок: signature_changed -> added -> removed
    i_sig = out.index("foo")
    i_add = out.index("A.new_m")
    i_rem = out.index("old_fn")
    assert i_sig < i_add < i_rem
    assert "было: def foo(a):" in out
    assert "стало: def foo(a, b):" in out
    assert "(method)" in out


def test_format_added_without_signature_no_literal_none():
    changes = [SymbolChange("added", "CONST", "variable", None, None, 3)]
    out = format_struct_summary(changes)
    assert "None" not in out
    assert "CONST" in out


def test_format_caps_long_lists():
    changes = [
        SymbolChange("added", f"f{i}", "function", None, f"def f{i}():", i)
        for i in range(50)
    ]
    out = format_struct_summary(changes)
    assert "(…ещё 10)" in out
    assert out.count("def f") == 40
