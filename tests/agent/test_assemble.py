from reviewer.agent.assemble import assemble_review, ground_line
from reviewer.vcs.base import Finding

PATCH = "@@ -1,3 +1,4 @@\n line\n+x = 1\n line2\n line3"


def _f(line=2, **kw):
    d = dict(category="correctness", severity="high", file="a.py", line=line,
             side="RIGHT", message="bug", suggestion=None, confidence=0.9)
    d.update(kw)
    return Finding(**d)


def test_inline_on_commentable_line_and_cap():
    res = assemble_review(
        [_f(), _f(message="bug2"), _f(message="bug3")],
        patches={"a.py": PATCH},
        sources={"a.py": "line\nx = 1\nline2\nline3\n"},
        existing_fps=set(),
        max_comments=2,
        suggestions_mode="off",
    )
    assert len(res.inline_comments) == 2          # кап сработал
    assert "bug3" in res.summary                  # переполнение ушло в сводку


def test_existing_fingerprint_skipped():
    f = _f()
    res = assemble_review([f], patches={"a.py": PATCH},
                          sources={"a.py": "line\nx = 1\nline2\nline3\n"},
                          existing_fps={f.fingerprint()},
                          max_comments=10, suggestions_mode="off")
    assert res.inline_comments == [] and f.message not in res.summary


def test_line_outside_diff_goes_to_summary():
    res = assemble_review([_f(line=99)], patches={"a.py": PATCH},
                          sources={"a.py": "line\nx = 1\nline2\nline3\n"},
                          existing_fps=set(), max_comments=10, suggestions_mode="off")
    assert res.inline_comments == [] and "bug" in res.summary


def test_ground_line_unique_quote_wins():
    src = "a\nx = compute()\nb\n"
    assert ground_line(src, "x = compute()", 9) == 2
    assert ground_line(src, None, 9) == 9
    assert ground_line(src, "nope", 9) == 9
