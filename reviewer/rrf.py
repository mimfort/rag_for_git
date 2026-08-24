"""Константа RRF — единственная в системе (PRI-267).

Живёт в корне пакета, а не в retrieval рядом с ``rrf_merge``: её читают оба
store (``reviewer/index/store.py``, ``reviewer/tasks/store.py``), которые лежат
НИЖЕ retrieval. Импорт retrieval→index развернул бы направление зависимости —
``reviewer/retrieval/multiquery.py`` уже импортирует ``reviewer.index.refs``.

Модуль намеренно ничего не импортирует: тогда его может взять любой слой, не
рискуя циклом.
"""
from __future__ import annotations

RRF_K = 60
"""Знаменатель RRF: score = Σ 1/(RRF_K + rank). Одно объявление на систему —
питоновское слияние подзапросов и оба SQL берут значение отсюда."""
