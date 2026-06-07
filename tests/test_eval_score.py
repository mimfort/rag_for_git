from eval.run_eval import score


def test_score_precision_recall():
    expected = [{"file": "a.py", "line": 1, "category": "bug"}, {"file": "a.py", "line": 2, "category": "bug"}]
    produced = [{"file": "a.py", "line": 1, "category": "bug"}, {"file": "a.py", "line": 9, "category": "bug"}]
    r = score(expected, produced)
    assert r["tp"] == 1 and r["precision"] == 0.5 and r["recall"] == 0.5
