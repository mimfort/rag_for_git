"""Round-trip сериализации PreparedReview через session_serde."""
from __future__ import annotations

import json

import pytest

from reviewer.agent.state import ReviewUnit
from reviewer.mcp.session_serde import from_payload, to_payload
from reviewer.policy.policy import ReviewPolicy
from reviewer.services.review_service import PreparedReview
from reviewer.vcs.base import PullRequest


class _DummyVCS:
    """Маркерный объект вместо живого VCSProvider."""


def _prepared(vcs) -> PreparedReview:
    prq = PullRequest(
        number=7, base_sha="b1", head_sha="h2", base_ref="main",
        title="T", body="body", draft=False, head_ref="feature/x",
    )
    return PreparedReview(
        repo="o/r",
        branch="main",
        prq=prq,
        units=[ReviewUnit("a.py", ["a.py#foo"], "@@ -1 +1 @@\n x", new_source="x = 1\n")],
        policy=ReviewPolicy(min_confidence=0.5, max_comments=10, task_board={"type": "yougile"}),
        patches={"a.py": "@@ -1 +1 @@\n x", "b.py": None},
        sources={"a.py": "x = 1\n"},
        changed_paths=["a.py"],
        changed_node_ids=["a.py#foo"],
        skipped_paths=["c.py"],
        overlay_ref="pr:7",
        vcs=vcs,
        changed_status={"a.py": "modified", "b.py": "added"},
        task_board={"type": "yougile"},
        task_keys={"primary": "PRI-7", "others": []},
    )


def test_payload_roundtrip_preserves_fields() -> None:
    original = _prepared(_DummyVCS())
    # Имитируем хранение в JSONB: payload должен пережить json-сериализацию.
    payload = json.loads(json.dumps(to_payload(original)))

    new_vcs = _DummyVCS()
    restored = from_payload(payload, new_vcs)

    assert restored.repo == original.repo
    assert restored.branch == original.branch
    assert restored.prq == original.prq                 # dataclass __eq__
    assert restored.units == original.units             # list[ReviewUnit] __eq__
    assert restored.policy == original.policy            # ReviewPolicy __eq__
    assert restored.patches == original.patches
    assert restored.sources == original.sources
    assert restored.changed_paths == original.changed_paths
    assert restored.changed_node_ids == original.changed_node_ids
    assert restored.skipped_paths == original.skipped_paths
    assert restored.overlay_ref == original.overlay_ref
    assert restored.changed_status == original.changed_status
    assert restored.task_board == original.task_board
    assert restored.task_keys == original.task_keys
    assert restored.vcs is new_vcs                       # vcs не сериализуется, подставлен заново


def test_to_payload_excludes_vcs() -> None:
    payload = to_payload(_prepared(_DummyVCS()))
    assert "vcs" not in payload


def test_from_payload_missing_key_raises() -> None:
    """Пустой payload бросает KeyError — контракт для _rehydrate_session."""
    with pytest.raises(KeyError):
        from_payload({}, _DummyVCS())


def test_from_payload_bad_prq_raises_type_error() -> None:
    """Испорченный prq бросает TypeError — контракт для _rehydrate_session."""
    payload = json.loads(json.dumps(to_payload(_prepared(_DummyVCS()))))
    payload["prq"] = {"unexpected_field": 1}  # PullRequest(**...) → TypeError
    with pytest.raises(TypeError):
        from_payload(payload, _DummyVCS())
