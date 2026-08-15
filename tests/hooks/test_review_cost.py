import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "plugin" / "hooks"))

import review_cost  # noqa: E402


def _assistant(model, usage, sidechain=False, extra=None):
    line = {"type": "assistant", "isSidechain": sidechain,
            "message": {"model": model, "usage": usage}}
    line.update(extra or {})
    return line


def _prepare_call(repo, pr):
    return {"type": "assistant", "message": {"model": "opus", "usage": {}, "content": [
        {"type": "tool_use", "name": "mcp__reviewer__prepare_review",
         "input": {"repo": repo, "pr": pr}}]}}


def _sidechain_prompt(text):
    return {"type": "user", "isSidechain": True, "message": {"content": text}}


def _payload(tmp_path, lines, repo="owner/name", pr=7):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return {"transcript_path": str(transcript), "session_id": "s1",
            "tool_name": "mcp__reviewer__publish_review",
            "tool_input": {"repo": repo, "pr": pr, "summary": "s"}}


def test_sidecar_path_is_deterministic_from_repo_and_pr():
    first = review_cost.sidecar_path("owner/name", 7)
    assert first == review_cost.sidecar_path("owner/name", 7)
    assert first != review_cost.sidecar_path("owner/name", 8)
    assert Path(first).name == "owner_name-7.json"


def test_window_starts_at_prepare_review_of_same_pr(tmp_path, monkeypatch):
    monkeypatch.setattr(review_cost.tempfile, "gettempdir", lambda: str(tmp_path))
    lines = [
        _assistant("opus", {"output_tokens": 1000}),          # до окна — не считается
        _prepare_call("owner/name", 7),
        _assistant("opus", {"output_tokens": 4}),             # в окне
    ]
    review_cost.run(_payload(tmp_path, lines))
    data = json.loads(Path(review_cost.sidecar_path("owner/name", 7)).read_text(encoding="utf-8"))
    assert data["usage"]["by_model"]["opus"]["output"] == 4


def test_window_ignores_prepare_of_another_pr(tmp_path, monkeypatch):
    monkeypatch.setattr(review_cost.tempfile, "gettempdir", lambda: str(tmp_path))
    lines = [
        _prepare_call("owner/name", 99),
        _assistant("opus", {"output_tokens": 3}),
        _prepare_call("owner/name", 7),
        _assistant("opus", {"output_tokens": 5}),
    ]
    review_cost.run(_payload(tmp_path, lines))
    data = json.loads(Path(review_cost.sidecar_path("owner/name", 7)).read_text(encoding="utf-8"))
    assert data["usage"]["by_model"]["opus"]["output"] == 5


def test_falls_back_to_skill_marker_when_no_prepare(tmp_path, monkeypatch):
    monkeypatch.setattr(review_cost.tempfile, "gettempdir", lambda: str(tmp_path))
    lines = [
        {"type": "user", "message": {"content":
            "Base directory for this skill: /x/skills/review-pr"}},
        _assistant("opus", {"output_tokens": 6}),
    ]
    review_cost.run(_payload(tmp_path, lines))
    data = json.loads(Path(review_cost.sidecar_path("owner/name", 7)).read_text(encoding="utf-8"))
    assert data["usage"]["by_model"]["opus"]["output"] == 6


def test_no_window_writes_no_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(review_cost.tempfile, "gettempdir", lambda: str(tmp_path))
    review_cost.run(_payload(tmp_path, [_assistant("opus", {"output_tokens": 6})]))
    assert not Path(review_cost.sidecar_path("owner/name", 7)).exists()


def test_sidechain_attributed_to_stage_by_reference_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(review_cost.tempfile, "gettempdir", lambda: str(tmp_path))
    lines = [
        _prepare_call("owner/name", 7),
        _sidechain_prompt("follow references/verify-prompt.md exactly"),
        _assistant("sonnet", {"output_tokens": 10}, sidechain=True),
        _sidechain_prompt("follow references/analyze-prompt.md exactly"),
        _assistant("sonnet", {"output_tokens": 20}, sidechain=True),
        _assistant("opus", {"output_tokens": 1}),
    ]
    review_cost.run(_payload(tmp_path, lines))
    data = json.loads(Path(review_cost.sidecar_path("owner/name", 7)).read_text(encoding="utf-8"))
    by_stage = data["usage"]["by_stage"]
    assert by_stage["verify"]["output"] == 10
    assert by_stage["analyze"]["output"] == 20
    assert by_stage["orchestrator"]["output"] == 1


def test_unrecognised_sidechain_goes_to_subagent_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(review_cost.tempfile, "gettempdir", lambda: str(tmp_path))
    lines = [
        _prepare_call("owner/name", 7),
        _sidechain_prompt("do something unlabelled"),
        _assistant("sonnet", {"output_tokens": 9}, sidechain=True),
    ]
    review_cost.run(_payload(tmp_path, lines))
    data = json.loads(Path(review_cost.sidecar_path("owner/name", 7)).read_text(encoding="utf-8"))
    assert data["usage"]["by_stage"]["subagent"]["output"] == 9


def _tool_result(uuid=None, parent=None):
    """Результат тула: type=user, sidechain, текста на верхнем уровне нет."""
    line = {"type": "user", "isSidechain": True, "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}
    if uuid is not None:
        line["uuid"] = uuid
    if parent is not None:
        line["parentUuid"] = parent
    return line


def test_tool_result_does_not_reset_stage(tmp_path, monkeypatch):
    """Реальная форма транскрипта: между ходами субагента лежит tool_result."""
    monkeypatch.setattr(review_cost.tempfile, "gettempdir", lambda: str(tmp_path))
    lines = [
        _prepare_call("owner/name", 7),
        _sidechain_prompt("follow references/analyze-prompt.md exactly"),
        _assistant("sonnet", {"output_tokens": 10}, sidechain=True),
        _tool_result(),
        _assistant("sonnet", {"output_tokens": 20}, sidechain=True),
    ]
    review_cost.run(_payload(tmp_path, lines))
    data = json.loads(Path(review_cost.sidecar_path("owner/name", 7)).read_text(encoding="utf-8"))
    by_stage = data["usage"]["by_stage"]
    assert by_stage["analyze"]["output"] == 30
    assert "subagent" not in by_stage


def test_parallel_chains_do_not_mix_stages(tmp_path, monkeypatch):
    """Две параллельные цепочки чередуются в транскрипте — стадии не смешиваются."""
    monkeypatch.setattr(review_cost.tempfile, "gettempdir", lambda: str(tmp_path))
    root_a = dict(_sidechain_prompt("follow references/analyze-prompt.md exactly"),
                  uuid="a0", parentUuid="main")
    root_v = dict(_sidechain_prompt("follow references/verify-prompt.md exactly"),
                  uuid="v0", parentUuid="main")
    lines = [
        _prepare_call("owner/name", 7),
        root_a,
        root_v,
        _assistant("sonnet", {"output_tokens": 10}, sidechain=True,
                   extra={"uuid": "a1", "parentUuid": "a0"}),
        _assistant("sonnet", {"output_tokens": 100}, sidechain=True,
                   extra={"uuid": "v1", "parentUuid": "v0"}),
        _tool_result(uuid="a2", parent="a1"),
        _tool_result(uuid="v2", parent="v1"),
        _assistant("sonnet", {"output_tokens": 5}, sidechain=True,
                   extra={"uuid": "a3", "parentUuid": "a2"}),
        _assistant("sonnet", {"output_tokens": 50}, sidechain=True,
                   extra={"uuid": "v3", "parentUuid": "v2"}),
    ]
    review_cost.run(_payload(tmp_path, lines))
    data = json.loads(Path(review_cost.sidecar_path("owner/name", 7)).read_text(encoding="utf-8"))
    by_stage = data["usage"]["by_stage"]
    assert by_stage["analyze"]["output"] == 15
    assert by_stage["verify"]["output"] == 150
    assert "subagent" not in by_stage


def test_total_cost_is_weighted_not_summed(tmp_path, monkeypatch):
    monkeypatch.setattr(review_cost.tempfile, "gettempdir", lambda: str(tmp_path))
    lines = [
        _prepare_call("owner/name", 7),
        _assistant("opus", {"input_tokens": 100, "output_tokens": 100,
                            "cache_creation_input_tokens": 100,
                            "cache_read_input_tokens": 100}),
    ]
    review_cost.run(_payload(tmp_path, lines))
    data = json.loads(Path(review_cost.sidecar_path("owner/name", 7)).read_text(encoding="utf-8"))
    assert data["total_cost"] == 735.0        # не 400 — веса, а не сумма
    assert data["model"] == "opus"
    assert data["transcript_source"] == "payload"
    assert data["version"] == 1


def test_broken_payload_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(review_cost.tempfile, "gettempdir", lambda: str(tmp_path))
    assert review_cost.run({}) == 0
    assert review_cost.run({"tool_input": {"repo": "owner/name"}}) == 0


def test_sidecar_path_matches_server_side_formula(tmp_path, monkeypatch):
    """Формула пути продублирована в хуке и на сервере — они обязаны совпадать."""
    import tempfile as _tempfile

    from reviewer.services import cost_sidecar

    monkeypatch.setattr(review_cost.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(_tempfile, "gettempdir", lambda: str(tmp_path))
    for repo, pr in (("owner/name", 7), ("o/r", 1), ("a/b-c", 42)):
        assert review_cost.sidecar_path(repo, pr) == cost_sidecar.sidecar_path(repo, pr)
    assert review_cost.SIDECAR_VERSION == cost_sidecar.SIDECAR_VERSION


def test_weight_formula_matches_every_copy():
    """Веса бакетов продублированы в трёх местах — они обязаны совпадать.

    Хук не может импортировать пакет ``reviewer`` (системный python3), поэтому
    единственный источник правды невозможен; расхождение ловится тестом.
    """
    import _transcript  # noqa: E402  (plugin/hooks на sys.path)

    from eval.solve_task_metrics.cost import WEIGHTS as EVAL_WEIGHTS
    from reviewer.web.history import _STAGE_COST_WEIGHTS

    assert _transcript.WEIGHTS == _STAGE_COST_WEIGHTS
    assert _transcript.WEIGHTS == EVAL_WEIGHTS


def test_stage_markers_cover_every_reference_prompt():
    """Guard: новый reference-промпт обязан получить строку в STAGE_MARKERS."""
    refs = Path(__file__).resolve().parents[2] / "plugin" / "skills" / "review-pr" / "references"
    for prompt in sorted(refs.glob("*-prompt.md")):
        assert any(prompt.name in marker for marker in review_cost.STAGE_MARKERS), (
            f"{prompt.name} не описан в review_cost.STAGE_MARKERS"
        )
