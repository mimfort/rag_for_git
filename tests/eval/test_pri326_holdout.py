import copy
import hashlib
import json
import subprocess

import pytest

from eval.pri278.briefs import EFFORT, MODEL, _classify_attempt
from eval.pri326.replay import freeze_experiment, score_experiment
from reviewer.mcp.brief_evidence import BriefEvidence


def _git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-q")
    for index in range(10):
        (clone / f"f{index}.py").write_text(f"value = {index}\n")
    _git(clone, "add", ".")
    _git(clone, "-c", "user.name=Replay Test", "-c", "user.email=replay@example.org", "commit", "-qm", "base")
    for index in range(10):
        (clone / f"f{index}.py").write_text("value = 999\n")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    context = "// f1.py#value (f1.py:1-1)\n  1 | value = 1"
    case = {
        "key": "TEST-42",
        "repo": "test/project",
        "base_sha": _git(clone, "rev-parse", "HEAD"),
        "input": "Use value from the appropriate file.",
        "contexts": {"base": context, "reframe": context},
        "obligatory": [{"path": f"f{idx}.py"} for idx in range(10)],
        "read_only": [],
    }
    return clone, case


def _responses(case):
    responses = {}
    for repeat in (1, 2):
        for arm in ("base", "reframe"):
            request = [{"key": case["key"], "task": case["input"], "context": case["contexts"][arm]}]
            records = [{"key": case["key"], "brief": "- f1.py:1 — shows value"}]
            if (repeat, arm) == (2, "reframe"):
                records = []
            responses[f"{case['key']}-r{repeat}-{arm}"] = {
                "identity": {"key": case["key"], "repeat": repeat, "arm": arm},
                "request_sha256": hashlib.sha256(json.dumps(request, ensure_ascii=False).encode()).hexdigest(),
                "state": "completed",
                "model": MODEL,
                "effort": EFFORT,
                "elapsed_s": 1,
                "usage": {"input_tokens": 42, "output_tokens": 10},
                **_classify_attempt(records, request),
            }
    return responses


def test_bulk_only_replay_reports_null_empty_stratum_and_symmetric_pairs(experiment):
    clone, case = experiment
    archive = freeze_experiment([case], _responses(case))
    result = score_experiment(archive, {case["repo"]: clone}, BriefEvidence())
    scheduled = result["scheduled"]["all"]
    paired = result["complete_pairs"]["all"]
    assert scheduled["base"]["ordinary"]["task_median_core"] is None
    assert scheduled["base"]["large"]["core"] == {"hit": 2, "all": 20}
    assert scheduled["reframe"]["large"]["core"] == {"hit": 1, "all": 20}
    assert scheduled["reframe"]["large"]["missing_briefs"] == 1
    assert paired["reframe"]["large"]["core"] == paired["base"]["large"]["core"] == {"hit": 1, "all": 10}
    assert paired["reframe"]["large"]["task_median_core"] == 0.1


def test_replay_rejects_incorrect_historical_rows(experiment):
    clone, case = experiment
    case["contexts"]["reframe"] = case["contexts"]["reframe"].replace("value = 1", "value = 9")
    archive = freeze_experiment([case], _responses(case))
    with pytest.raises(ValueError, match="Git"):
        score_experiment(archive, {case["repo"]: clone}, BriefEvidence())


def test_replay_binds_each_response_to_case_repeat_and_arm(experiment):
    clone, case = experiment
    responses = _responses(case)
    archive = freeze_experiment([case], responses)
    changed = copy.deepcopy(archive)
    changed["responses"]["TEST-42-r2-base"] = responses["TEST-42-r1-base"]
    with pytest.raises(ValueError, match="контрольн"):
        score_experiment(changed, {case["repo"]: clone}, BriefEvidence())
    responses["TEST-42-r2-base"] = responses["TEST-42-r1-base"]
    with pytest.raises(ValueError, match="идентичность"):
        freeze_experiment([case], responses)
