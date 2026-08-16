"""Офлайн-харнесс метрик этапа solve-task (PRI-250).

НЕ продакшн-путь reviewer: пакет живёт в eval/ и никогда не импортируется из
reviewer/**. Расчётные модули (cost, classify, recall, history, forecast) —
чистые функции без ввода-вывода и на stdlib.

Единственное исключение из stdlib-инварианта — live.py (PRI-254): режиму
replay нужен живой ретрив (Postgres, Neo4j, Voyage). Он импортируется лениво,
внутри тела команды, поэтому snapshot|stats|compare|forecast продолжают
работать без инфраструктуры. Границу стережёт tests/eval/test_live_boundary.py.

Расчётные модули (classify, recall, briefs) ПЕРЕНЕСЕНЫ в reviewer/metrics/
brief_quality (PRI-249) и здесь только реэкспортируются: онлайн-съём метрики и
этот харнесс обязаны мерить одной линейкой. Остальные модули (cost, ground_truth,
history, snapshot, report, forecast, endtoend) офлайн-специфичны и живут здесь.
"""
