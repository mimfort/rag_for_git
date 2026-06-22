from reviewer.index.struct_diff import extract_signature


def test_extract_signature_reexported_and_works():
    assert extract_signature("def f(a, b):\n    return a") == "def f(a, b):"
    assert extract_signature("class A(B, C):\n    pass") == "class A(B, C):"
    assert extract_signature("x = 1") is None
