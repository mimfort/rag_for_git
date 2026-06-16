from reviewer.agent.assemble import assemble_review, ground_line
from reviewer.vcs.base import Finding

PATCH = "@@ -1,3 +1,4 @@\n line\n+x = 1\n line2\n line3"
SOURCE = "line\nx = 1\nline2\nline3\n"


def _f(line=2, **kw):
    d = dict(category="correctness", severity="high", file="a.py", line=line,
             side="RIGHT", message="bug", suggestion=None, confidence=0.9)
    d.update(kw)
    return Finding(**d)


def test_inline_on_commentable_line_and_cap():
    res = assemble_review(
        [_f(), _f(message="bug2"), _f(message="bug3")],
        patches={"a.py": PATCH},
        sources={"a.py": SOURCE},
        existing_fps=set(),
        max_comments=2,
        suggestions_mode="off",
    )
    assert len(res.inline_comments) == 2          # кап сработал
    assert "bug3" in res.summary                  # переполнение ушло в сводку
    # счётчики: bug3 в сводке только из-за капа (строка 2 — в диффе)
    assert res.capped == 1
    assert res.moved_to_summary == 0
    assert res.skipped_existing == 0
    # findings_rows: все три опубликованы, две inline, одна — в сводке
    assert [r["inline"] for r in res.findings_rows] == [True, True, False]
    assert all(r["published"] for r in res.findings_rows)


def test_existing_fingerprint_skipped():
    f = _f()
    res = assemble_review([f], patches={"a.py": PATCH},
                          sources={"a.py": SOURCE},
                          existing_fps={f.fingerprint()},
                          max_comments=10, suggestions_mode="off")
    assert res.inline_comments == [] and f.message not in res.summary
    assert res.skipped_existing == 1
    # отфильтрованная находка остаётся в findings_rows как неопубликованная
    assert len(res.findings_rows) == 1
    row = res.findings_rows[0]
    assert row["published"] is False and row["inline"] is False
    assert row["fingerprint"] == f.fingerprint()


def test_line_outside_diff_goes_to_summary():
    res = assemble_review([_f(line=99)], patches={"a.py": PATCH},
                          sources={"a.py": SOURCE},
                          existing_fps=set(), max_comments=10, suggestions_mode="off")
    assert res.inline_comments == [] and "bug" in res.summary
    assert res.moved_to_summary == 1
    assert res.capped == 0
    row = res.findings_rows[0]
    assert row["published"] is True and row["inline"] is False
    assert row["line"] is None      # строка 99 вне файла — обнулена грунтовкой


def test_cap_branch_distinguishes_capped_from_unanchorable():
    """В cap-ветке: находка с валидной строкой диффа → capped, находка, которая
    и без капа ушла бы в сводку (строка вне диффа/файла) → moved_to_summary."""
    res = assemble_review(
        [_f(), _f(message="bug2"), _f(message="capfill"), _f(line=99, message="nodiff")],
        patches={"a.py": PATCH},
        sources={"a.py": SOURCE},
        existing_fps=set(), max_comments=2, suggestions_mode="off")
    assert len(res.inline_comments) == 2
    assert res.capped == 1            # capfill: строка 2 в диффе, не попала только из-за капа
    assert res.moved_to_summary == 1  # nodiff: строка вне файла — в сводку в любом случае


def test_suggestion_apply_single_and_multiline():
    """Режим apply: одно-строчный fix даёт ```suggestion```-блок без start_line,
    многострочный (fix_start<fix_end) — InlineComment с start_line/start_side="RIGHT"."""
    f1 = _f(fix_start=2, fix_end=2, replacement="x = 2\n")
    res = assemble_review([f1], patches={"a.py": PATCH}, sources={"a.py": SOURCE},
                          existing_fps=set(), max_comments=10, suggestions_mode="apply")
    assert len(res.inline_comments) == 1
    ic = res.inline_comments[0]
    assert "```suggestion\nx = 2\n```" in ic.body
    assert ic.line == 2 and ic.side == "RIGHT" and ic.start_line is None
    assert res.findings_rows[0]["inline"] is True

    f2 = _f(fix_start=2, fix_end=3, replacement="a\nb\n")
    res2 = assemble_review([f2], patches={"a.py": PATCH}, sources={"a.py": SOURCE},
                           existing_fps=set(), max_comments=10, suggestions_mode="apply")
    assert len(res2.inline_comments) == 1
    ic2 = res2.inline_comments[0]
    assert "```suggestion\na\nb\n```" in ic2.body
    assert ic2.line == 3 and ic2.start_line == 2 and ic2.start_side == "RIGHT"


def test_ground_line_unique_quote_wins():
    src = "a\nx = compute()\nb\n"
    assert ground_line(src, "x = compute()", 9) == 2
    assert ground_line(src, None, 9) == 9
    assert ground_line(src, "nope", 9) == 9


from reviewer.agent.assemble import snap_to_commentable

_SNAP_COMMENTABLE = {"RIGHT": {10, 11, 12, 20}, "LEFT": {8, 9, 10}}
# Строки 1..24 вида "line 1", "line 2", ...
_SNAP_SOURCE = "\n".join(f"line {i}" for i in range(1, 25))


def test_snap_line_already_commentable():
    """Строка уже в commentable — без изменений."""
    assert snap_to_commentable(10, "RIGHT", None, _SNAP_COMMENTABLE, _SNAP_SOURCE) == 10


def test_snap_with_matching_code_quote():
    """code_quote совпадает с кандидатом — снапаем."""
    src = "\n".join([""] * 11 + ["match_me"] + [""] * 5)  # line 12 = "match_me"
    commentable = {"RIGHT": {12}, "LEFT": set()}
    assert snap_to_commentable(13, "RIGHT", "match_me", commentable, src) == 12


def test_snap_code_quote_no_match_returns_original():
    """code_quote не совпадает ни с одним кандидатом — возвращаем оригинал."""
    src = "\n".join([""] * 11 + ["other_content"] + [""] * 5)
    commentable = {"RIGHT": {12}, "LEFT": set()}
    assert snap_to_commentable(13, "RIGHT", "no_match", commentable, src) == 13


def test_snap_without_code_quote_snaps_nearest():
    """Без code_quote снапаем на ближайшего кандидата в пределах max_distance."""
    commentable = {"RIGHT": {10, 12}, "LEFT": set()}
    # line=14, ближайший в RIGHT — 12 (расстояние 2 ≤ 5)
    assert snap_to_commentable(14, "RIGHT", None, commentable, _SNAP_SOURCE) == 12


def test_snap_too_far_returns_original():
    """Ближайший кандидат дальше max_distance=5 — без изменений."""
    # line=1, ближайший в RIGHT — 10 (расстояние 9 > 5)
    assert snap_to_commentable(1, "RIGHT", None, _SNAP_COMMENTABLE, _SNAP_SOURCE) == 1


def test_snap_empty_commentable_returns_original():
    """Нет commentable строк — без изменений."""
    assert snap_to_commentable(5, "RIGHT", "x", {}, _SNAP_SOURCE) == 5


def test_snap_wrong_side_no_candidates():
    """Кандидаты есть в RIGHT, но сторона LEFT пустая — без изменений."""
    commentable = {"RIGHT": {10}, "LEFT": set()}
    assert snap_to_commentable(11, "LEFT", None, commentable, _SNAP_SOURCE) == 11
