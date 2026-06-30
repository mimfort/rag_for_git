from reviewer.index.store import Retrieved


def test_retrieved_new_fields_default():
    r = Retrieved(node_id="a.py#f", path="a.py", symbol_fqn="f", kind="function",
                  start_line=1, end_line=2, text="x", score=0.1)
    assert r.ann_distance is None
    assert r.bm25_hit is False


def test_retrieved_accepts_ann_fields():
    r = Retrieved(node_id="a.py#f", path="a.py", symbol_fqn="f", kind="function",
                  start_line=1, end_line=2, text="x", score=0.1,
                  ann_distance=0.42, bm25_hit=True)
    assert r.ann_distance == 0.42 and r.bm25_hit is True
