from reviewer.mcp.service import TOOL_STAGES, build_step


def test_stage_map_covers_pr_session_tools():
    assert TOOL_STAGES["search_code"] == "analyze"
    assert TOOL_STAGES["read_file"] == "analyze"
    assert TOOL_STAGES["get_impact"] == "analyze"
    assert TOOL_STAGES["get_candidate_findings"] == "verify"
    assert TOOL_STAGES["submit_verdicts"] == "verify"
    assert TOOL_STAGES["submit_findings"] == "synthesize"


def test_build_step_records_stage_and_payload_sizes():
    step = build_step(seq=3, name="search_code", args={"query": "abc"}, result="x" * 10)
    assert step["stage"] == "analyze"
    assert step["kind"] == "tool_call"
    assert step["seq"] == 3
    assert step["name"] == "search_code"
    tc = step["tool_calls"][0]
    assert tc["result_bytes"] == 10
    assert tc["args_bytes"] > 0


def test_build_step_truncates_text_but_not_byte_count():
    step = build_step(seq=0, name="read_file", args={"path": "a.py"}, result="y" * 5000)
    assert len(step["text"]) == 500
    assert step["tool_calls"][0]["result_bytes"] == 5000


def test_unknown_tool_falls_back_to_analyze():
    assert build_step(seq=0, name="mystery", args={}, result="")["stage"] == "analyze"


def test_non_string_result_has_zero_bytes_and_no_text():
    step = build_step(seq=0, name="submit_findings", args={}, result={"recorded": 2})
    assert step["text"] is None
    assert step["tool_calls"][0]["result_bytes"] == 0


def test_client_steps_get_default_stage_and_kind():
    from reviewer.mcp.service import normalize_client_step
    assert normalize_client_step({"name": "prepare"}) == {
        "name": "prepare", "stage": "client", "kind": "client_step",
    }
    # заполненные клиентом значения не перетираются
    kept = normalize_client_step({"name": "x", "stage": "analyze", "kind": "llm_call"})
    assert kept["stage"] == "analyze" and kept["kind"] == "llm_call"
