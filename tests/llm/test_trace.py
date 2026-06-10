"""Тесты для reviewer.llm.trace.TraceLog."""
from __future__ import annotations

import threading
from types import SimpleNamespace

from reviewer.llm.trace import TraceLog, _TEXT_CAP, _TEXT_SUFFIX


# ---------------------------------------------------------------------------
# Вспомогательные фабрики фейковых AIMessage
# ---------------------------------------------------------------------------

def _ai_msg(
    text: str = "Рассуждение модели",
    tool_calls: list | None = None,
    input_tokens: int = 100,
    output_tokens: int = 20,
    cost: float = 0.0,
):
    """Фейковый AIMessage (SimpleNamespace) с usage_metadata."""
    meta = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    rmeta: dict = {"token_usage": {"cost": cost}} if cost else {}
    return SimpleNamespace(
        content=text,
        tool_calls=tool_calls or [],
        usage_metadata=meta,
        response_metadata=rmeta,
    )


def _ai_msg_no_meta(text: str = "Без метаданных"):
    """Фейковый AIMessage без usage_metadata."""
    return SimpleNamespace(content=text, tool_calls=[])


# ---------------------------------------------------------------------------
# Тесты record_prompt
# ---------------------------------------------------------------------------

def test_record_prompt_adds_step():
    log = TraceLog()
    log.record_prompt("analyze", "reviewer/app.py", "Текст промпта")

    steps = log.snapshot()
    assert len(steps) == 1
    s = steps[0]
    assert s["kind"] == "prompt"
    assert s["stage"] == "analyze"
    assert s["unit"] == "reviewer/app.py"
    assert s["text"] == "Текст промпта"
    assert s["tool_calls"] is None
    assert s["tokens"] == 0
    assert s["cost"] == 0.0
    assert s["seq"] == 1


def test_record_prompt_truncates_long_text():
    long_text = "A" * (_TEXT_CAP + 100)
    log = TraceLog()
    log.record_prompt("analyze", "file.py", long_text)

    steps = log.snapshot()
    assert len(steps[0]["text"]) == _TEXT_CAP + len(_TEXT_SUFFIX)
    assert steps[0]["text"].endswith(_TEXT_SUFFIX)


def test_record_prompt_exact_cap_not_truncated():
    exact_text = "B" * _TEXT_CAP
    log = TraceLog()
    log.record_prompt("analyze", "file.py", exact_text)

    steps = log.snapshot()
    assert steps[0]["text"] == exact_text
    assert not steps[0]["text"].endswith(_TEXT_SUFFIX)


# ---------------------------------------------------------------------------
# Тесты record_llm_call
# ---------------------------------------------------------------------------

def test_record_llm_call_adds_step():
    log = TraceLog()
    msg = _ai_msg("Думаю...", input_tokens=200, output_tokens=50, cost=0.01)
    log.record_llm_call("analyze", "reviewer/app.py", msg)

    steps = log.snapshot()
    assert len(steps) == 1
    s = steps[0]
    assert s["kind"] == "llm_call"
    assert s["stage"] == "analyze"
    assert s["unit"] == "reviewer/app.py"
    assert s["text"] == "Думаю..."
    assert s["tokens"] == 250   # 200 + 50
    assert abs(s["cost"] - 0.01) < 1e-9
    assert s["tool_calls"] is None


def test_record_llm_call_with_tool_calls():
    log = TraceLog()
    tool_calls = [
        {"name": "search_code", "args": {"query": "def connect"}, "id": "tc1"},
        {"name": "get_related_symbols", "args": {"node_id": "app#MyClass"}, "id": "tc2"},
    ]
    msg = _ai_msg("Инструменты!", tool_calls=tool_calls)
    log.record_llm_call("analyze", "file.py", msg)

    steps = log.snapshot()
    s = steps[0]
    assert s["tool_calls"] is not None
    assert len(s["tool_calls"]) == 2
    assert s["tool_calls"][0]["name"] == "search_code"
    assert s["tool_calls"][0]["args"] == {"query": "def connect"}
    assert s["tool_calls"][1]["name"] == "get_related_symbols"


def test_record_llm_call_no_tool_calls_is_none():
    log = TraceLog()
    msg = _ai_msg("Без тулов")
    log.record_llm_call("analyze", "file.py", msg)

    steps = log.snapshot()
    assert steps[0]["tool_calls"] is None


def test_record_llm_call_no_metadata_does_not_raise():
    log = TraceLog()
    msg = _ai_msg_no_meta("Нет метаданных")
    log.record_llm_call("verify", "a.py:10", msg)

    steps = log.snapshot()
    assert len(steps) == 1
    assert steps[0]["tokens"] == 0
    assert steps[0]["cost"] == 0.0


def test_record_llm_call_truncates_long_text():
    long_text = "X" * (_TEXT_CAP + 500)
    msg = _ai_msg(long_text)
    log = TraceLog()
    log.record_llm_call("analyze", "file.py", msg)

    steps = log.snapshot()
    assert len(steps[0]["text"]) == _TEXT_CAP + len(_TEXT_SUFFIX)
    assert steps[0]["text"].endswith(_TEXT_SUFFIX)


# ---------------------------------------------------------------------------
# Тесты record_tool_call
# ---------------------------------------------------------------------------

def test_record_tool_call_adds_step():
    log = TraceLog()
    log.record_tool_call(
        "analyze", "reviewer/app.py",
        "search_code", {"query": "def connect"},
        "Результат поиска: node_id=app#connect, path:10-20",
    )

    steps = log.snapshot()
    assert len(steps) == 1
    s = steps[0]
    assert s["kind"] == "tool_call"
    assert s["name"] == "search_code"
    assert s["text"] == "Результат поиска: node_id=app#connect, path:10-20"
    assert s["tool_calls"] is not None
    assert s["tool_calls"][0]["name"] == "search_code"
    assert s["tool_calls"][0]["args"] == {"query": "def connect"}
    assert s["tokens"] == 0
    assert s["cost"] == 0.0


def test_record_tool_call_result_as_str():
    """result преобразуется через str() — принимаем любой объект."""
    log = TraceLog()
    log.record_tool_call("analyze", "file.py", "search_code", {}, [1, 2, 3])

    steps = log.snapshot()
    assert steps[0]["text"] == "[1, 2, 3]"


def test_record_tool_call_truncates_long_result():
    long_result = "R" * (_TEXT_CAP + 200)
    log = TraceLog()
    log.record_tool_call("analyze", "file.py", "search_code", {}, long_result)

    steps = log.snapshot()
    assert steps[0]["text"].endswith(_TEXT_SUFFIX)


# ---------------------------------------------------------------------------
# Тесты порядка seq и snapshot
# ---------------------------------------------------------------------------

def test_seq_monotonically_increases():
    log = TraceLog()
    log.record_prompt("analyze", "a.py", "p1")
    log.record_llm_call("analyze", "a.py", _ai_msg("r1"))
    log.record_tool_call("analyze", "a.py", "search_code", {}, "res")
    log.record_llm_call("analyze", "a.py", _ai_msg("r2"))

    steps = log.snapshot()
    seqs = [s["seq"] for s in steps]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)   # уникальны


def test_snapshot_returns_correct_order():
    log = TraceLog()
    log.record_prompt("analyze", "a.py", "промпт")
    log.record_llm_call("analyze", "a.py", _ai_msg("ответ 1"))
    log.record_tool_call("analyze", "a.py", "search_code", {}, "результат")
    log.record_llm_call("analyze", "a.py", _ai_msg("ответ 2"))

    steps = log.snapshot()
    assert [s["kind"] for s in steps] == ["prompt", "llm_call", "tool_call", "llm_call"]


def test_snapshot_does_not_modify_internal_state():
    log = TraceLog()
    log.record_prompt("analyze", "a.py", "текст")

    snap = log.snapshot()
    snap[0]["kind"] = "hacked"   # мутируем снимок

    fresh = log.snapshot()
    assert fresh[0]["kind"] == "prompt"   # не изменился


def test_snapshot_shape_keys():
    """Каждый шаг содержит все обязательные поля."""
    log = TraceLog()
    log.record_llm_call("analyze", "file.py", _ai_msg("text"))

    s = log.snapshot()[0]
    for key in ("seq", "stage", "unit", "kind", "name", "text", "tool_calls", "tokens", "cost"):
        assert key in s, f"Отсутствует поле: {key}"


# ---------------------------------------------------------------------------
# Потокобезопасность
# ---------------------------------------------------------------------------

def test_thread_safety_seq_unique():
    """Параллельные записи дают уникальные и монотонные seq."""
    log = TraceLog()
    n = 30

    def worker():
        for i in range(n):
            log.record_prompt("analyze", f"file_{i}.py", "текст")

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    steps = log.snapshot()
    assert len(steps) == 5 * n
    seqs = [s["seq"] for s in steps]
    assert len(set(seqs)) == 5 * n   # все seq уникальны


def test_thread_safety_no_exceptions():
    """Параллельные вызовы всех методов не бросают исключений."""
    log = TraceLog()

    def worker_prompt():
        for _ in range(20):
            log.record_prompt("analyze", "a.py", "текст")

    def worker_llm():
        for _ in range(20):
            log.record_llm_call("verify", "b.py:1", _ai_msg("r"))

    def worker_tool():
        for _ in range(20):
            log.record_tool_call("analyze", "a.py", "search_code", {}, "res")

    threads = (
        [threading.Thread(target=worker_prompt) for _ in range(2)]
        + [threading.Thread(target=worker_llm) for _ in range(2)]
        + [threading.Thread(target=worker_tool) for _ in range(2)]
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    steps = log.snapshot()
    assert len(steps) == 6 * 20


# ---------------------------------------------------------------------------
# Fail-soft: никогда не бросает
# ---------------------------------------------------------------------------

def test_does_not_raise_on_none_message():
    log = TraceLog()
    log.record_llm_call("analyze", "file.py", None)
    log.record_prompt("analyze", "file.py", None)  # type: ignore[arg-type]
    # не упали — хорошо


def test_does_not_raise_on_garbage_args():
    log = TraceLog()
    log.record_tool_call("analyze", "file.py", "tool", {}, object())
    log.record_llm_call("verify", "a.py:1", 42)
    # не упали — хорошо
