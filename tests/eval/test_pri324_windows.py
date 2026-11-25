import gzip
import json
from pathlib import Path

import pytest

from eval.pri324.replay import _metrics, _validate_archive, _validate_envelope
from eval.pri324.selector import choose_windows
from reviewer.index.store import Retrieved
from reviewer.mcp.brief_anchors import check_brief_anchors
from reviewer.mcp.brief_evidence import BriefEvidence, _visible_lines
from reviewer.retrieval.retriever import _render_block


def _fragment(path, name, first, text):
    return Retrieved(
        f"{path}#{name}",
        path,
        name,
        "function",
        first,
        first + len(text.splitlines()) - 1,
        text,
        0.0,
    )


def test_selection_keeps_paths_order_and_character_budget():
    first = _fragment(
        "search.py", "old_handler", 1, "def old_handler():\n    return 'legacy handler'"
    )
    relevant = _fragment("search.py", "bm25", 4, "def bm25():\n    return 1")
    other = _fragment("paths.py", "collect", 1, "def collect():\n    return ()")
    baseline = "\n\n".join(_render_block(item, line_numbers=True) for item in (first, other))
    sources = {"search.py": f"{first.text}\n\n{relevant.text}", "paths.py": other.text}

    changed = choose_windows(
        baseline, "Изменить bm25", sources, {"search.py": [first, relevant], "paths.py": [other]}
    )
    assert "search.py#bm25" in changed
    assert list(_visible_lines(changed)) == list(_visible_lines(baseline))
    assert len(changed.split("\n\n")[0]) <= len(baseline.split("\n\n")[0])
    assert changed.split("\n\n")[1] == baseline.split("\n\n")[1]
    eligible, diagnostics = check_brief_anchors(
        {"code": changed}, lambda paths: {p: sources[p] for p in paths}
    )
    assert eligible == {"code": {"search.py": {4, 5}, "paths.py": {1, 2}}}
    assert diagnostics["code"] == {"checked": 4, "mismatched": 0, "unavailable": 0}


def test_no_signal_preserves_baseline_bytes():
    item = _fragment("file.py", "old", 1, "def old():\n    return None")
    baseline = _render_block(item, line_numbers=True)
    assert (
        choose_windows(
            baseline, "Только кириллица", {"file.py": item.text}, {"file.py": [item]}
        ).encode()
        == baseline.encode()
    )


def test_scoring_counts_only_git_verified_references(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    case = {"key": "PRI-1", "obligatory": [{"path": "a.py"}], "read_only": [{"path": "b.py"}]}
    answer = {
        "brief": "## Relevant code\n- a.py:3 — править\n- b.py:4 — прочитать",
        "verified_paths": ["a.py"],
    }
    evidence = {"code": {"a.py": {3}, "b.py": {5}}}
    score = _metrics(case, answer, evidence, BriefEvidence())
    assert score["core"] == {"hit": 1, "all": 1}
    assert score["dependencies"] == {"hit": 0, "all": 1}
    assert score["rejected_references"] == 1
    with pytest.raises(ValueError, match="результаты проверки"):
        _metrics(case, {**answer, "verified_paths": ["a.py", "b.py"]}, evidence, BriefEvidence())


def _archive():
    with gzip.open(Path("eval/pri324/responses.json.gz"), "rt", encoding="utf-8") as file:
        return json.load(file)


def test_archive_rejects_transplant_between_identical_inputs_across_repeats():
    archive = _archive()
    _validate_archive(archive)
    batches = archive["batches"]
    first, second = "main-r1-b1-base", "main-r2-b1-base"
    assert batches[first]["input_sha256"] == batches[second]["input_sha256"]
    batches[first] = batches[second]
    with pytest.raises(ValueError, match="контрольные суммы"):
        _validate_archive(archive)


def test_envelope_checks_model_attempt_and_token_counts():
    archive = _archive()
    control = archive["controls"]["initial_batch_with_singleton_retries"]
    response = control["reframe"]
    protocol = archive["protocol"]
    _validate_envelope(response, protocol, control["keys"])
    with pytest.raises(ValueError, match="Модель"):
        _validate_envelope({**response, "model": "other"}, protocol, control["keys"])
    with pytest.raises(ValueError, match="пропусках"):
        _validate_envelope({**response, "retry_attempts": {}}, protocol, control["keys"])
    with pytest.raises(ValueError, match="токенов"):
        _validate_envelope({**response, "usage": {}}, protocol, control["keys"])
