"""Рекурсивное слияние слоёв политики с листовой диагностикой (PRI-260).

Слой, не высказавшийся о подсекции, не должен её стирать: до PRI-260 слияние
шло по верхнему ключу, и домашний per-repo слой с частичной `context_limits`
уносил коммиченные подсекции целиком. Диагностика затенения здесь той же
гранулярности — путь до листа, иначе по отчёту `config show` неотличимо
«подсекция потеряна» от «её и не было».

Модуль намеренно чистый (никаких путей, файлов и VCS): его зовут и резолвер
`resolve_policy_data`, и симуляция публикации домашнего слоя в `config migrate`.
Одна копия логики на обоих — иначе `migrate` и `show` разошлись бы в вердикте
о затенении.
"""
from __future__ import annotations

from collections.abc import Mapping

# Единственный атомарный mapping-ключ: task_board — связный контракт
# (type + project + create_target/done_target + options), а не набор
# независимых настроек. Частичный домашний `task_board: {type: jira}` поверх
# коммиченного yougile дал бы при рекурсии конфиг доски, которого не писал ни
# один слой: тип от одного, проект и целевые колонки — от другого.
ATOMIC_KEYS = frozenset({"task_board"})


def leaf_paths(value: object, prefix: str = "") -> list[str]:
    """Пути до листьев значения. Пустой mapping — сам себе лист."""
    if isinstance(value, Mapping) and value:
        paths: list[str] = []
        for key, child in value.items():
            nested = f"{prefix}.{key}" if prefix else str(key)
            paths.extend(leaf_paths(child, nested))
        return paths
    return [prefix]


def merge_layer(
    merged: dict[str, object],
    sources: dict[str, str],
    shadowed: dict[str, list[str]],
    data: Mapping[str, object],
    source: str,
) -> None:
    """Наложить слой ``data`` на ``merged``, обновив листовую диагностику."""
    _merge_mapping(merged, sources, shadowed, data, source, "")


def _merge_mapping(
    merged: dict[str, object],
    sources: dict[str, str],
    shadowed: dict[str, list[str]],
    data: Mapping[str, object],
    source: str,
    prefix: str,
) -> None:
    for raw_key, value in data.items():
        # Ключ нормализуется к строке: пути диагностики строятся по str(key), и
        # без нормализации `1:` и `"1":` из разных слоёв жили бы в `merged`
        # порознь, а в `sources`/`shadowed` — под одним путём (ложное затенение).
        key = str(raw_key)
        path = f"{prefix}.{key}" if prefix else key
        top = path.split(".", 1)[0]
        current = merged.get(key)
        if (
            isinstance(value, Mapping)
            and value
            and isinstance(current, Mapping)
            and top not in ATOMIC_KEYS
        ):
            child = dict(current)
            _merge_mapping(child, sources, shadowed, value, source, path)
            merged[key] = child
            continue
        # Пустой mapping рекурсии не даёт: тело цикла не выполнилось бы ни разу,
        # значение осталось бы прежним, и слой, снимающий секцию целиком, был бы
        # молча проигнорирован. Он идёт заменой — как и до PRI-260.
        _replace(merged, sources, shadowed, key, path, value, source, top)


def _replace(
    merged: dict[str, object],
    sources: dict[str, str],
    shadowed: dict[str, list[str]],
    key: str,
    path: str,
    value: object,
    source: str,
    top: str,
) -> None:
    """Заменить значение целиком, сняв прежние записи под этим путём.

    Снятие обязательно: mapping, заменённый скаляром, иначе оставил бы в
    отчёте источники листьев, которых в эффективной политике больше нет.
    """
    for stale in [k for k in sources if k == path or k.startswith(f"{path}.")]:
        shadowed.setdefault(stale, []).append(sources.pop(stale))
    merged[key] = value
    if top in ATOMIC_KEYS or not isinstance(value, Mapping):
        sources[path] = source
        return
    for leaf in leaf_paths(value, path):
        sources[leaf] = source
