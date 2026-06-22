"""submit_findings/get_candidate_findings/submit_verdicts — сессионное накопление."""
from __future__ import annotations

import json
from unittest.mock import patch

from tests.mcp.test_publish import (
    RAW, _make_mcp_service_with_publish, _fake_chunk,
)


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_submit_findings_accumulates_with_ids(_ov, _ch):
    svc, _, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    r1 = svc.submit_findings("o/r", 7, [RAW])
    r2 = svc.submit_findings("o/r", 7, [dict(RAW, message="bug B")])
    assert r1 == {"accepted": 1, "ids": ["f1"]}
    assert r2 == {"accepted": 1, "ids": ["f2"]}
    sess = svc._sessions[("o/r", 7)]
    assert set(sess.candidates) == {"f1", "f2"}
    assert sess.candidates["f1"].message == "bug here"


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_get_candidate_findings_returns_ids(_ov, _ch):
    svc, _, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    svc.submit_findings("o/r", 7, [RAW])
    payload = json.loads(svc.get_candidate_findings("o/r", 7))
    assert payload["candidates"][0]["id"] == "f1"
    assert payload["candidates"][0]["file"] == "a.py"
    assert payload["candidates"][0]["code_quote"] == "x = 1"


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_submit_verdicts_records_and_flags_unknown(_ov, _ch):
    svc, _, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    svc.submit_findings("o/r", 7, [RAW])              # → f1
    r = svc.submit_verdicts("o/r", 7, [{"id": "f1", "is_real": False},
                                       {"id": "f9", "is_real": True}])
    assert r == {"recorded": 1, "unknown_ids": ["f9"]}
    assert svc._sessions[("o/r", 7)].verdicts == {"f1": False}
