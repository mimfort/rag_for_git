import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from reviewer.bugreport.diagnostics import diagnose
from reviewer.entrypoints.mcp_server import create_server

TRACE = (
    'Traceback (most recent call last):\n'
    '  File "/private/customer/reviewer/mcp/service.py", line 27, in call\n'
    'ValueError: internal state'
)


@pytest.mark.parametrize("result, kind", [
    ({"content": [{"type": "text", "text": '{"status":"unexpected"}'}]},
     "contract_violation"),
    ({"structured_content": {"status": "unexpected"}}, "contract_violation"),
    ({"content": [{"type": "text", "text": TRACE}], "isError": True}, "tool_exception"),
    ({"content": [{"type": "text", "text": TRACE + "\nConnection refused"}]},
     None),
    ({"structuredContent": {"status": "unexpected", "retryable": True}},
     None),
    ({"status": "ok"}, None),
])
def test_diagnostic_decodes_mcp_envelopes(result, kind):
    diagnosis = diagnose("finish_task", json.dumps(result))
    if kind is None:
        assert diagnosis == {"status": "clear"}
    else:
        assert diagnosis["status"] == "suspected_defect"
        assert diagnosis["kind"] == kind
        assert len(diagnosis["signature"]) == 16
        assert "/private/customer" not in json.dumps(diagnosis)


def test_projected_hook_uses_identical_classifier_source():
    root = Path(__file__).resolve().parents[2]
    assert (root / "plugin/hooks/_reviewer_diagnostics.py").read_bytes() == (
        root / "reviewer/bugreport/diagnostics.py"
    ).read_bytes()


def test_portable_diagnosis_mcp_tool_uses_no_service():
    service = MagicMock()
    server = create_server(service)

    async def scenario():
        result = await server.call_tool("diagnose_tool_result", {
            "tool": "finish_task", "response": '{"status": "unexpected"}',
        })
        assert json.loads(result[0].text)["kind"] == "contract_violation"
        assert not service.mock_calls

    asyncio.run(scenario())
