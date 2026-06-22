"""Сборка итогового ревью из верифицированных находок (без I/O).

Функция :func:`assemble_review` инкапсулирует всю логику превращения списка
:class:`~reviewer.vcs.base.Finding` в inline-комментарии и markdown-сводку:
ранжирование по severity/confidence, fingerprint-дедупликация, кап max_comments,
разнесение inline/summary. Не содержит сетевых вызовов — используется MCP-тулом
``publish_review``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from reviewer.agent.dedup import _SEVERITY_RANK
from reviewer.vcs.base import Finding, InlineComment
from reviewer.vcs.diff import commentable_lines


# ---------------------------------------------------------------------------
# Вспомогательные функции грунтовки строк
# ---------------------------------------------------------------------------

def _pick_nearest(matches: list[int], line: int | None) -> int | None:
    """Из строк-совпадений цитаты выбрать ближайшую к заявленной line.

    - нет совпадений → None (вызывающий пробует следующий уровень поиска);
    - одно совпадение → оно (line не нужен);
    - несколько и line задан → ближайшее (тай-брейк: меньший номер строки);
    - несколько и line is None → None (близость неопределена — не угадываем).
    """
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    if line is None:
        return None
    return min(matches, key=lambda i: (abs(i - line), i))


def ground_line(source: str | None, code_quote: str | None, line: int | None) -> int | None:
    """Уточнить номер строки по точной цитате из исходника (анти-галлюцинация).

    Алгоритм:
    1. Ищем строки с точным совпадением (после strip).
    2. Если одно совпадение — возвращаем его.
    3. Если несколько совпадений и line задан — возвращаем ближайшее к line
       (тай-брейк: меньший номер строки); если line is None — возвращаем None.
    4. Если нет ни одного точного совпадения — ищем подстроку и применяем
       ту же логику выбора ближайшего.
    5. Во всех остальных случаях возвращаем исходный ``line`` как есть.

    Parameters
    ----------
    source:
        Содержимое файла (newline-separated строки).
    code_quote:
        Дословная цитата строки кода из модели (или None).
    line:
        Исходный номер строки (fallback при неудаче).

    Returns
    -------
    int | None
        Уточнённый или исходный номер строки.
    """
    if not source or not code_quote:
        return line
    needle = code_quote.strip()
    if not needle:
        return line
    lines = source.splitlines()
    exact = [i for i, ln in enumerate(lines, 1) if ln.strip() == needle]
    picked = _pick_nearest(exact, line)
    if picked is not None:
        return picked
    if not exact:
        sub = [i for i, ln in enumerate(lines, 1) if needle in ln]
        picked = _pick_nearest(sub, line)
        if picked is not None:
            return picked
    return line


def snap_to_commentable(
    line: int,
    side: str,
    code_quote: str | None,
    commentable: dict[str, set[int]],
    source: str,
    max_distance: int = 5,
) -> int:
    """Снапнуть line к ближайшей commentable строке если текущая вне хунка.

    Верификация по code_quote: целевая строка должна содержать цитату.
    Без цитаты — принимаем ближайшего кандидата в пределах max_distance.
    Возвращает исходный line если подходящего кандидата нет.
    """
    target_set = commentable.get(side, set())
    if not target_set or line in target_set:
        return line
    src_lines = source.splitlines() if source else []
    candidates = sorted(target_set, key=lambda ln: abs(ln - line))
    for cand in candidates:
        if abs(cand - line) > max_distance:
            break
        if code_quote:
            cand_text = src_lines[cand - 1].strip() if 0 < cand <= len(src_lines) else ""
            if code_quote.strip() in cand_text:
                return cand
        else:
            return cand
    return line


# ---------------------------------------------------------------------------
# Вспомогательные функции для suggestion-блоков
# ---------------------------------------------------------------------------

def _range_in_diff(right: set[int], start: int, end: int) -> bool:
    """Все строки [start..end] должны быть в RIGHT-строках диффа (иначе GitHub 422/промах)."""
    return start <= end and all(ln in right for ln in range(start, end + 1))


def _overlaps(used: list[tuple[int, int]], start: int, end: int) -> bool:
    """Проверить, пересекается ли диапазон [start..end] с уже использованными."""
    return any(not (end < s or start > e) for (s, e) in used)


def _can_apply(f: Finding, right: set[int], used: list[tuple[int, int]], mode: str) -> bool:
    """applyable suggestion разрешён только если: режим apply, есть точная замена,
    весь диапазон в диффе (RIGHT) и не пересекается с уже выставленными правками."""
    return (mode == "apply"
            and f.replacement is not None
            and f.fix_start is not None and f.fix_end is not None
            and _range_in_diff(right, f.fix_start, f.fix_end)
            and not _overlaps(used, f.fix_start, f.fix_end))


# ---------------------------------------------------------------------------
# Результат сборки
# ---------------------------------------------------------------------------

@dataclass
class AssembledReview:
    """Результат функции :func:`assemble_review`."""

    inline_comments: list[InlineComment]
    summary: str                      # markdown-сводка (всегда с заголовком «## Авто-ревью»)
    skipped_existing: int = 0         # отфильтровано по fingerprint
    moved_to_summary: int = 0         # в сводке, т.к. inline невозможен (строка вне диффа/файла)
    capped: int = 0                   # в сводке только из-за капа max_comments
    findings_rows: list[dict] = field(default_factory=list)
    """Строки для review_findings: file, line, category, severity, confidence,
    fingerprint, message, is_real, published, inline.
    Включает все обработанные находки (is_real=True); отфильтрованные по fingerprint —
    с inline=False, published=False."""


# ---------------------------------------------------------------------------
# Форматирование комментария и строки истории
# ---------------------------------------------------------------------------

def _loc(f: Finding) -> str:
    """Локация для сводки: ``file:line`` или просто ``file``, если строка неизвестна."""
    return f"{f.file}:{f.line}" if f.line is not None else f.file


def _format_body(f: Finding, fp: str, *, suggestion_block: str = "") -> str:
    """Тело комментария: заголовок [категория/severity], текст, опциональный
    suggestion-блок и скрытый fingerprint-маркер для идемпотентности."""
    body = f"**[{f.category}/{f.severity}]** {f.message}"
    if f.suggestion:
        body += f"\n\n💡 _Предложение:_ {f.suggestion}"
    body += suggestion_block
    body += f"\n<!-- ai-review:{fp} -->"
    return body


def _row(f: Finding, fp: str, *, published: bool, inline: bool) -> dict:
    """Строка для таблицы review_findings (формат _record_history в review_service)."""
    return {
        "file": f.file,
        "line": f.line,
        "category": f.category,
        "severity": f.severity,
        "confidence": f.confidence,
        "fingerprint": fp,
        "message": (f.message or "")[:500],
        "is_real": True,
        "published": published,
        "inline": inline,
    }


# ---------------------------------------------------------------------------
# Валидация координат находки
# ---------------------------------------------------------------------------

def _sane_line(fnd: Finding, commentable: dict, sources: dict[str, str] | None) -> int | None:
    """Реальная строка находки или None, если модель её выдумала.

    Валидируем по факту: файл должен быть среди изменённых в PR, а номер
    строки — существовать в новой версии файла. Иначе inline-гейт и сводка
    показали бы несуществующую строку (модели иногда галлюцинируют file:line,
    напр. для класса из другого модуля).
    """
    if fnd.line is None:
        return None
    if fnd.file not in commentable:
        return None
    src = (sources or {}).get(fnd.file)
    if src is not None and not (1 <= fnd.line <= len(src.splitlines())):
        return None
    return fnd.line


# ---------------------------------------------------------------------------
# Основная функция сборки
# ---------------------------------------------------------------------------

def assemble_review(
    verified: list[Finding],
    *,
    patches: dict[str, str | None],
    sources: dict[str, str],
    existing_fps: set[str],
    max_comments: int,
    suggestions_mode: str,
) -> AssembledReview:
    """Собрать inline-комментарии и markdown-сводку из верифицированных находок.

    Логика (та же, что в ``make_assemble_node``):

    1. Вычисляем commentable-строки для каждого изменённого файла.
    2. Сортируем находки по рангу: critical > high > medium > low, при равенстве
       — убывающий confidence.
    3. Для каждой находки:

       - Грунтуем ``f.line`` через ``_sane_line`` (выбрасываем галлюцинированные
         координаты), затем считаем fingerprint.
       - Пропускаем, если fingerprint уже есть в ``existing_fps``.
       - Если ``_can_apply`` — формируем ```suggestion```-блок (многострочный →
         start_line/start_side), отмечаем диапазон как использованный.
       - Иначе, если строка в диффе — обычный inline текстовый комментарий.
       - Иначе — строка в markdown-сводку.
       - Останавливаемся при достижении ``max_comments`` inline-замечаний;
         оставшиеся уходят в сводку.

    .. warning::
       Функция МУТИРУЕТ входные находки: ``f.line`` перезаписывается результатом
       ``_sane_line`` (галлюцинированные координаты обнуляются). Вызывающий может
       пересчитывать ``f.fingerprint()`` после вызова — он совпадёт с fingerprint
       в ``findings_rows`` и в маркерах комментариев.

    Parameters
    ----------
    verified:
        Список находок, прошедших policy.gate + verifier.
    patches:
        Словарь path → unified diff (или None для слишком больших файлов).
    sources:
        Словарь path → содержимое файла (новая версия).
    existing_fps:
        Fingerprint'ы уже опубликованных комментариев — пропускаем дубли.
    max_comments:
        Максимальное количество inline-комментариев.
    suggestions_mode:
        Режим suggestion'ов: ``"apply"`` — вставлять ```suggestion```-блоки,
        ``"off"`` — только текстовые советы.

    Returns
    -------
    AssembledReview
        Контейнер с inline_comments, summary и вспомогательной статистикой.
    """
    commentable = {p: commentable_lines(patches.get(p)) for p in patches}

    used: dict[str, list[tuple[int, int]]] = {}
    inline: list[InlineComment] = []
    summary_lines: list[str] = ["## Авто-ревью\n"]
    findings_rows: list[dict] = []

    skipped_existing = 0
    moved_to_summary = 0
    capped = 0

    # Составной ранг: сначала severity (critical > high > medium > low),
    # при равном severity — большая центральность символа (хаб важнее, PRI-129),
    # при равной центральности — большая уверенность идёт раньше.
    ranked = sorted(
        verified,
        key=lambda f: (-_SEVERITY_RANK.get(f.severity, 0), -f.centrality, -f.confidence),
    )

    for f in ranked:
        # Грунтуем номер строки ДО fingerprint: fp включает line и должен совпадать
        # с маркером в теле комментария и с записью в review_findings.
        f.line = _sane_line(f, commentable, sources)
        fp = f.fingerprint()

        if fp in existing_fps:
            # Дубликат прошлого прогона — не логируем, добавляем в findings_rows как неопубликованный.
            skipped_existing += 1
            findings_rows.append(_row(f, fp, published=False, inline=False))
            continue

        allowed = commentable.get(f.file, {"RIGHT": set(), "LEFT": set()})

        if len(inline) >= max_comments:
            # Кап достигнут — излишек уходит в сводку.
            summary_lines.append(f"- `{_loc(f)}` {_format_body(f, fp)}")
            if f.line is not None and f.line in allowed.get(f.side, set()):
                capped += 1            # в сводке только из-за капа
            else:
                moved_to_summary += 1  # ушла бы в сводку и без капа
            findings_rows.append(_row(f, fp, published=True, inline=False))
            continue

        right = allowed.get("RIGHT", set())

        # 1) applyable ```suggestion — только при безопасных инвариантах
        if _can_apply(f, right, used.get(f.file, []), suggestions_mode):
            repl = f.replacement.rstrip("\n")
            body = _format_body(f, fp, suggestion_block=f"\n\n```suggestion\n{repl}\n```")
            used.setdefault(f.file, []).append((f.fix_start, f.fix_end))
            if f.fix_start < f.fix_end:   # многострочный диапазон
                inline.append(InlineComment(f.file, f.fix_end, "RIGHT", body,
                                            start_line=f.fix_start, start_side="RIGHT"))
            else:                          # одна строка
                inline.append(InlineComment(f.file, f.fix_end, "RIGHT", body))
            findings_rows.append(_row(f, fp, published=True, inline=True))
            continue

        # 2) Обычный inline (текстовый совет) на строке диффа, иначе — в сводку.
        body = _format_body(f, fp)
        if f.line is not None and f.line in allowed.get(f.side, set()):
            inline.append(InlineComment(f.file, f.line, f.side, body))
            findings_rows.append(_row(f, fp, published=True, inline=True))
        else:
            summary_lines.append(f"- `{_loc(f)}` {body}")
            moved_to_summary += 1
            findings_rows.append(_row(f, fp, published=True, inline=False))

    if inline:
        summary_lines.insert(
            1, f"Выставлено inline-замечаний на строки диффа: {len(inline)}.\n"
        )
    if len(summary_lines) == 1:
        summary_lines.append("Замечаний не найдено.")

    return AssembledReview(
        inline_comments=inline,
        summary="\n".join(summary_lines),
        skipped_existing=skipped_existing,
        moved_to_summary=moved_to_summary,
        capped=capped,
        findings_rows=findings_rows,
    )
