"""Тесты для reviewer.llm.usage.UsageLog."""
from types import SimpleNamespace

from reviewer.llm.usage import UsageLog


# ---------------------------------------------------------------------------
# Вспомогательные фабрики фейковых сообщений
# ---------------------------------------------------------------------------

def _msg(input_tokens: int, output_tokens: int, cache_read: int = 0):
    """Сообщение с usage_metadata, как у langchain AIMessage."""
    meta: dict = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    if cache_read:
        meta["input_token_details"] = {"cache_read": cache_read}
    return SimpleNamespace(usage_metadata=meta)


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
