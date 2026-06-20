from reviewer.tools.impact import extract_signature


def test_extract_signature_single_line():
    assert extract_signature("def f(a, b):\n    return a") == "def f(a, b):"


def test_extract_signature_with_annotations():
    text = "def f(a: int, b: str = 'x') -> bool:\n    ..."
    assert extract_signature(text) == "def f(a: int, b: str = 'x') -> bool:"


def test_extract_signature_multiline():
    text = "def f(\n    a,\n    b,\n):\n    return a"
    assert extract_signature(text) == "def f( a, b, ):"


def test_extract_signature_async_and_decorator():
    text = "@cache\nasync def f(x):\n    return x"
    assert extract_signature(text) == "async def f(x):"


def test_extract_signature_class():
    assert extract_signature("class A(B, C):\n    pass") == "class A(B, C):"


def test_extract_signature_none_when_absent():
    assert extract_signature("x = 1\ny = 2") is None
