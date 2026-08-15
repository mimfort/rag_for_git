"""Офлайн-харнесс метрик этапа solve-task (PRI-250).

НЕ продакшн-путь reviewer: пакет живёт в eval/, использует только stdlib и
никогда не импортируется из reviewer/**. Расчётные модули (cost, classify,
recall, history, forecast) — чистые функции без ввода-вывода.

Расчётные модули (classify, recall, briefs) ПЕРЕНЕСЕНЫ в reviewer/metrics/
brief_quality (PRI-249) и здесь только реэкспортируются: онлайн-съём метрики и
этот харнесс обязаны мерить одной линейкой. Остальные модули (cost, ground_truth,
history, snapshot, report, forecast, endtoend) офлайн-специфичны и живут здесь.
"""
