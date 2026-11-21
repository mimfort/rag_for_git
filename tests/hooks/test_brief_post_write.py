"""Unit-тесты последовательного PostToolUse-хука brief_post_write."""

import importlib.util
import io
import pathlib
import sys

import pytest

from .test_brief_guard import (
    _SOLVE_USER,
    _brief,
    _payload,
    _retrieval_rows,
    _write_call,
    _write_transcript,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOOKS_DIR = ROOT / "plugin" / "hooks"
HOOK_PATH = HOOKS_DIR / "brief_post_write.py"


def _load():
    if not HOOK_PATH.is_file():
        pytest.fail(f"отсутствует wrapper hook: {HOOK_PATH}")
    spec = importlib.util.spec_from_file_location("brief_post_write", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    previous_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(HOOKS_DIR))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
        sys.dont_write_bytecode = previous_bytecode
    return mod


@pytest.fixture
def hook_module():
    return _load()


def test_run_calls_cost_then_guard_with_same_payload(hook_module, monkeypatch):
    payload = {"tool_name": "Write"}
    calls = []

    monkeypatch.setattr(
        hook_module.brief_cost,
        "run",
        lambda value: calls.append(("cost", value)),
    )
    monkeypatch.setattr(
        hook_module.brief_guard,
        "run",
        lambda value: calls.append(("guard", value)),
    )

    assert hook_module.run(payload) == 0
    assert calls == [("cost", payload), ("guard", payload)]


def test_run_calls_guard_when_cost_raises(hook_module, monkeypatch):
    payload = {"tool_name": "Write"}
    calls = []

    def raise_cost(value):
        calls.append(("cost", value))
        raise RuntimeError("cost failed")

    monkeypatch.setattr(hook_module.brief_cost, "run", raise_cost)
    monkeypatch.setattr(
        hook_module.brief_guard,
        "run",
        lambda value: calls.append(("guard", value)),
    )

    assert hook_module.run(payload) == 0
    assert calls == [("cost", payload), ("guard", payload)]


def test_run_returns_zero_when_guard_raises(hook_module, monkeypatch):
    payload = {"tool_name": "Write"}

    monkeypatch.setattr(hook_module.brief_cost, "run", lambda _payload: 0)
    monkeypatch.setattr(
        hook_module.brief_guard,
        "run",
        lambda _payload: (_ for _ in ()).throw(RuntimeError("guard failed")),
    )

    assert hook_module.run(payload) == 0


def test_main_returns_zero_for_malformed_stdin(hook_module, monkeypatch):
    monkeypatch.setattr(hook_module.sys, "stdin", io.StringIO("not-json"))

    assert hook_module.main() == 0


def test_run_preserves_verified_bytes_when_appending_token_block(hook_module, tmp_path):
    (tmp_path / ".review.yml").write_text(
        "solve_task:\n  brief_token_cost: true\n", encoding="utf-8"
    )
    canonical = (
        "## Relevant code\r\n"
        "<!-- reviewer:verified-citations -->\r\n\r\n"
        "- `source.py:3` — reason mentions other.py:9\r\n"
    )
    legacy = "\n## Test exemplars\n- missing.py:2"
    brief = _brief(tmp_path, canonical + legacy + "\n\n\n")
    transcript = _write_transcript(
        tmp_path,
        [
            _SOLVE_USER,
            {"type": "assistant", "message": {"model": "m", "usage": {"input_tokens": 5}}},
            *_retrieval_rows("// known.py#known (known.py:1-3)"),
            _write_call(),
        ],
    )
    payload = _payload(brief, transcript)

    assert hook_module.run(payload) == 0

    token_block = (
        "## Токены (этап solve-task)\n"
        "Модель: m\n"
        "fresh-in 5 · out 0 · cache-write 0 · cache-read 0\n"
        "Всего: 5 токенов\n"
    )
    warning = "⚠️ [файл не в результатах поиска]"
    expected = f"{canonical}{legacy} {warning}\n\n\n{token_block}".encode()
    assert brief.read_bytes() == expected

    assert hook_module.run(payload) == 0
    assert brief.read_bytes() == expected
