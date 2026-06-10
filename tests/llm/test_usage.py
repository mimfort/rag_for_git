"""Тесты для reviewer.llm.usage.UsageLog."""
from types import SimpleNamespace

from reviewer.llm.usage import UsageLog


# ---------------------------------------------------------------------------
# Вспомогательные фабрики фейковых сообщений
# ---------------------------------------------------------------------------

def _msg(input_tokens: int, output_tokens: int, cache_read: int = 0, cost: float = 0.0):
    """Сообщение с usage_metadata (и опц. response_metadata.cost), как у langchain AIMessage."""
    meta: dict = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    if cache_read:
        meta["input_token_details"] = {"cache_read": cache_read}
    rmeta: dict = {"token_usage": {"cost": cost}} if cost else {}
    return SimpleNamespace(usage_metadata=meta, response_metadata=rmeta)


def _msg_no_meta():
    """Сообщение без атрибута usage_metadata."""
    return SimpleNamespace()


# ---------------------------------------------------------------------------
# Тесты агрегации
# ---------------------------------------------------------------------------

def test_add_aggregates_by_stage():
    log = UsageLog()
    log.add("analyze", _msg(100, 10))
    log.add("analyze", _msg(200, 20))
    log.add("verify", _msg(50, 5))

    report = log.report()
    assert "analyze" in report
    assert "verify" in report
    # суммарные токены по этапу analyze
    assert "300" in report  # input 100+200
    assert "30" in report   # output 10+20


def test_add_without_usage_metadata_does_not_raise_and_counts_calls():
    log = UsageLog()
    log.add("analyze", _msg_no_meta())
    log.add("analyze", _msg_no_meta())

    report = log.report()
    assert "analyze" in report
    # два вызова — счётчик должен отразиться в отчёте
    assert "2 вызовов" in report


def test_report_empty_when_no_calls():
    log = UsageLog()
    assert log.report() == ""


def test_report_contains_stages_and_itogo():
    log = UsageLog()
    log.add("analyze", _msg(1000, 100))
    log.add("verify", _msg(500, 50))
    log.add("synthesize", _msg(200, 20))

    report = log.report()
    assert "analyze" in report
    assert "verify" in report
    assert "synthesize" in report
    assert "итого" in report


def test_cache_read_counted_from_input_token_details():
    log = UsageLog()
    log.add("analyze", _msg(5000, 200, cache_read=3000))

    report = log.report()
    assert "кэш 3000" in report


def test_cache_read_omitted_when_zero():
    log = UsageLog()
    log.add("analyze", _msg(100, 10, cache_read=0))

    report = log.report()
    assert "кэш" not in report


def test_itogo_sums_all_stages():
    log = UsageLog()
    log.add("analyze", _msg(1000, 100))
    log.add("verify", _msg(500, 50))

    report = log.report()
    lines = report.splitlines()
    itogo_line = next((line for line in lines if line.startswith("итого")), None)
    assert itogo_line is not None
    assert "1500" in itogo_line   # суммарный input
    assert "150" in itogo_line    # суммарный output


def test_single_stage_no_itogo():
    """При одном этапе строка «итого» не нужна."""
    log = UsageLog()
    log.add("analyze", _msg(100, 10))

    report = log.report()
    assert "итого" not in report


def test_cost_aggregated_and_shown_in_report():
    log = UsageLog()
    log.add("analyze", _msg(1000, 100, cost=0.01))
    log.add("analyze", _msg(2000, 200, cost=0.02))

    report = log.report()
    # 0.01 + 0.02 = 0.0300
    assert "$0.0300" in report


def test_cost_summed_into_itogo():
    log = UsageLog()
    log.add("analyze", _msg(1000, 100, cost=0.1))
    log.add("verify", _msg(500, 50, cost=0.05))

    lines = log.report().splitlines()
    itogo_line = next((line for line in lines if line.startswith("итого")), None)
    assert itogo_line is not None
    assert "$0.1500" in itogo_line   # суммарная стоимость 0.1 + 0.05


def test_cost_omitted_when_absent():
    log = UsageLog()
    log.add("analyze", _msg(100, 10))   # без cost

    assert "$" not in log.report()


def test_snapshot_returns_per_stage_dict():
    """snapshot() возвращает словарь с правильными полями для каждого этапа."""
    log = UsageLog()
    log.add("analyze", _msg(1000, 100, cache_read=200, cost=0.01))
    log.add("analyze", _msg(500, 50))
    log.add("verify", _msg(300, 30, cost=0.005))

    snap = log.snapshot()
    assert set(snap.keys()) == {"analyze", "verify"}

    a = snap["analyze"]
    assert a["calls"] == 2
    assert a["input_tokens"] == 1500
    assert a["output_tokens"] == 150
    assert a["cache_read_tokens"] == 200
    assert abs(a["cost"] - 0.01) < 1e-9

    v = snap["verify"]
    assert v["calls"] == 1
    assert v["input_tokens"] == 300
    assert v["output_tokens"] == 30
    assert v["cache_read_tokens"] == 0
    assert abs(v["cost"] - 0.005) < 1e-9


def test_snapshot_empty_when_no_calls():
    """snapshot() возвращает пустой словарь, если вызовов не было."""
    log = UsageLog()
    assert log.snapshot() == {}


def test_snapshot_does_not_modify_state():
    """Изменение возвращённого словаря не влияет на внутреннее состояние."""
    log = UsageLog()
    log.add("analyze", _msg(100, 10))

    snap = log.snapshot()
    snap["analyze"]["calls"] = 999  # мутируем снимок

    # Оригинальные данные не тронуты
    assert log.snapshot()["analyze"]["calls"] == 1


def test_snapshot_is_thread_safe():
    """snapshot() под параллельными add() не ломает счётчики."""
    import threading

    log = UsageLog()
    n = 30

    def worker():
        for _ in range(n):
            log.add("analyze", _msg(10, 1))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = log.snapshot()
    assert snap["analyze"]["calls"] == 4 * n
    assert snap["analyze"]["input_tokens"] == 4 * n * 10


def test_add_never_raises_on_garbage_message():
    """add() не должен бросать исключения даже при мусорном вводе."""
    log = UsageLog()
    log.add("analyze", None)
    log.add("analyze", 42)
    log.add("analyze", object())
    # не упали — хорошо


def test_thread_safety():
    """Параллельные add() из разных потоков не портят счётчики."""
    import threading

    log = UsageLog()
    n = 50

    def worker():
        for _ in range(n):
            log.add("analyze", _msg(10, 1))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    report = log.report()
    # 4 потока × 50 вызовов × 10 токенов = 2000 входных токенов
    assert f"{4 * n} вызовов" in report
    assert str(4 * n * 10) in report   # input_tokens
