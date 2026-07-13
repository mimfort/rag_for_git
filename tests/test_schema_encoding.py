import importlib
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "module_name,schema_name",
    (
        ("reviewer.index.store", "schema.sql"),
        ("reviewer.mcp.session_store", "session_store.sql"),
        ("reviewer.web.history", "schema.sql"),
    ),
)
def test_schema_loader_uses_utf8_under_non_utf8_locale(monkeypatch, module_name, schema_name):
    original_read_text = Path.read_text
    encodings = []

    def read_text(path, encoding=None, errors=None):
        if path.name == schema_name:
            encodings.append(encoding)
            if encoding is None:
                raise UnicodeDecodeError("cp1252", b"\x81", 0, 1, "undefined character")
        return original_read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_text)

    module = importlib.import_module(module_name)
    importlib.reload(module)

    assert encodings
    assert all(encoding == "utf-8" for encoding in encodings)
