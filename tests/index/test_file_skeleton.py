"""PRI-245: скелет файла как объединение скелетов его символов-чанков."""
from reviewer.index.chunker import chunk_python, file_skeleton_lines

SRC = '''"""Модульный докстринг."""
import os


class A:
    """Класс A."""

    def m(self, x):
        """Метод m."""
        y = x + 1
        return y


def f(a, b):
    """Функция f."""
    return a + b
'''


def _chunks(src: str) -> list[tuple[int, str]]:
    return [(c.start_line, c.text) for c in chunk_python("x.py", src.encode("utf-8"))]


def test_skeleton_carries_signatures_and_first_docstring_lines():
    rendered = {n: text for n, text in file_skeleton_lines(_chunks(SRC))}
    assert "class A:" in rendered[5]
    assert '"""Класс A."""' in rendered[6]
    assert "def m(self, x):" in rendered[8]
    assert "def f(a, b):" in rendered[14]


def test_skeleton_omits_bodies():
    rendered = "\n".join(text for _, text in file_skeleton_lines(_chunks(SRC)))
    assert "y = x + 1" not in rendered
    assert "return a + b" not in rendered


def test_nested_symbols_are_deduplicated():
    """Скелет класса уже содержит сигнатуры методов — строки не задваиваются."""
    numbers = [n for n, _ in file_skeleton_lines(_chunks(SRC))]
    assert numbers == sorted(numbers)
    assert len(numbers) == len(set(numbers))


def test_line_numbers_are_absolute():
    """Номера строк — абсолютные в файле, а не относительные внутри чанка."""
    for number, text in file_skeleton_lines(_chunks(SRC)):
        assert SRC.splitlines()[number - 1] == text


def test_empty_input_is_empty():
    assert file_skeleton_lines([]) == []
