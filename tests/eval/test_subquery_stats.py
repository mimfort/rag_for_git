"""Распределение числа подзапросов по размеру задачи (PRI-255, критерий 1)."""
from eval.solve_task_metrics import subquery_stats


def _task(lines: int, items: int = 0) -> dict:
    body = "\n".join(f"строка {i}" for i in range(lines))
    todo = "\n".join(f"{i}. пункт {i}" for i in range(1, items + 1))
    description = body + (f"\n\n## Что сделать\n\n{todo}\n" if items else "")
    return {"title": "T", "description": description}


def test_size_buckets_split_small_medium_bulk():
    assert "мелкая" in subquery_stats.size_bucket(_task(3))
    assert "средняя" in subquery_stats.size_bucket(_task(20))
    assert "развёртка" in subquery_stats.size_bucket(_task(50))


def test_distribution_is_not_a_constant_across_buckets():
    rows = subquery_stats.distribution([
        ("PRI-1", _task(3), "q"),
        ("PRI-2", _task(50, items=10), "q"),
    ])
    counts = {row["bucket"]: row["median"] for row in rows}
    assert len(set(counts.values())) > 1, "число подзапросов производно от размера задачи"


def test_render_lists_every_bucket_and_task_count():
    text = subquery_stats.render([("PRI-1", _task(3), "q"), ("PRI-2", _task(50, 10), "q")])
    assert "задач" in text.splitlines()[0], "заголовок таблицы называет колонку количества задач"
    assert "мелкая" in text and "развёртка" in text, "оба класса размера присутствуют в выводе"


def test_missing_task_counts_as_single_subquery():
    rows = subquery_stats.distribution([("PRI-9", None, "формулировка")])
    assert rows[0]["median"] == 1
