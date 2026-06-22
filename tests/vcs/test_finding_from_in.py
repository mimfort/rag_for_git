"""Finding.from_in: построение внутреннего Finding из валидированного FindingIn."""
from reviewer.mcp.schemas import FindingIn
from reviewer.vcs.base import Finding


def test_from_in_maps_fields_and_fix():
    fi = FindingIn.model_validate({
        "file": "a.py", "severity": "high", "line": 2, "code_quote": "x = 1",
        "message": "bug", "suggestion": "fix it", "confidence": 0.9,
        "fix": {"start_line": 2, "end_line": 2, "replacement": "x = 2"},
    })
    f = Finding.from_in(fi)
    assert isinstance(f, Finding)
    assert (f.file, f.line, f.side, f.severity) == ("a.py", 2, "RIGHT", "high")
    assert f.message == "bug" and f.suggestion == "fix it" and f.confidence == 0.9
    assert (f.fix_start, f.fix_end, f.replacement) == (2, 2, "x = 2")
    assert f.code_quote == "x = 1"
    assert f.centrality == 0.0


def test_from_in_without_fix():
    fi = FindingIn.model_validate({"file": "b.py", "message": "m"})
    f = Finding.from_in(fi)
    assert (f.fix_start, f.fix_end, f.replacement) == (None, None, None)
    assert f.category == "correctness" and f.confidence == 0.1
