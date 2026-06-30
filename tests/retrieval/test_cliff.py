from reviewer.retrieval.cliff import select_by_cliff, format_tail_note


class _It:
    def __init__(self, path):
        self.path = path

    def __repr__(self):
        return f"It({self.path})"


def _scored(pairs):
    return [(_It(p), s) for p, s in pairs]


def test_relative_cliff_cuts_below_ratio_but_floor_lifts_minimum():
    scored = _scored([("a/1.py", 0.91), ("a/2.py", 0.34), ("a/3.py", 0.12), ("a/4.py", 0.05)])
    kept, meta = select_by_cliff(scored, floor_n=2, ceiling_n=15, ratio=0.5, abs_floor=0.3)
    # 0.34 < 0.91*0.5 → обрыв после 1-го, но floor_n=2 поднимает до 2
    assert [it.path for it in kept] == ["a/1.py", "a/2.py"]
    assert meta.top_score == 0.91 and meta.cut_score == 0.34


def test_long_high_run_capped_by_ceiling():
    scored = _scored([(f"a/{i}.py", 0.9 - i * 0.01) for i in range(20)])
    kept, meta = select_by_cliff(scored, floor_n=4, ceiling_n=10, ratio=0.5, abs_floor=0.3)
    assert len(kept) == 10
    assert meta.beyond_relevant > 0          # хвост ≥ abs_floor существует


def test_abs_floor_cuts_noise_when_top_is_low():
    scored = _scored([("a/1.py", 0.41), ("a/2.py", 0.38), ("a/3.py", 0.20)])
    kept, _ = select_by_cliff(scored, floor_n=1, ceiling_n=15, ratio=0.5, abs_floor=0.3)
    # 0.20 < abs_floor 0.3 → отсечён, хотя 0.20 >= 0.41*0.5=0.205? нет (0.20<0.205) — оба правила режут
    assert [it.path for it in kept] == ["a/1.py", "a/2.py"]


def test_tail_meta_groups_by_path_prefix_and_note_built():
    scored = _scored([
        ("reviewer/retrieval/x.py", 0.88), ("reviewer/retrieval/y.py", 0.71),
        ("reviewer/retrieval/z.py", 0.69), ("reviewer/index/a.py", 0.55),
        ("tests/t.py", 0.20),
    ])
    kept, meta = select_by_cliff(scored, floor_n=1, ceiling_n=2, ratio=0.5, abs_floor=0.3)
    assert len(kept) == 2
    assert meta.beyond_relevant == 2          # z.py(0.69)+a.py(0.55) ≥ abs_floor; tests(0.20) нет
    prefixes = {g[0] for g in meta.groups}
    assert "reviewer" in prefixes
    note = format_tail_note(meta)
    assert note and "ceiling" in note


def test_empty_and_single():
    assert select_by_cliff([], floor_n=4, ceiling_n=15, ratio=0.5, abs_floor=0.3)[0] == []
    kept, meta = select_by_cliff(_scored([("a/1.py", 0.5)]), floor_n=4, ceiling_n=15,
                                 ratio=0.5, abs_floor=0.3)
    assert len(kept) == 1 and format_tail_note(meta) is None
