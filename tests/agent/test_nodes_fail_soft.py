import logging

from reviewer.agent.graph import build_graph
from reviewer.agent.nodes import make_verify_node, make_assemble_node, make_publish_node
from reviewer.agent.state import Deps, ReviewUnit
from reviewer.policy.policy import ReviewPolicy
from reviewer.vcs.base import Finding


def _make_finding(category="correctness", severity="high", confidence=0.9, file="a.py"):
    return Finding(category, severity, file, 1, "RIGHT", "msg", None, confidence)


def _node_deps(policy, verifier, vcs=None):
    return Deps(
        vcs=vcs,
        retriever=None,
        graph=None,
        policy=policy,
        analyzer=None,
        verifier=verifier,
        pr_number=1,
        head_sha="s",
        overlay_ref="pr:1",
        changed_paths=[],
        patches={},
    )


def _assemble_deps(vcs):
    return Deps(
        vcs=vcs,
        retriever=None,
        graph=None,
        policy=ReviewPolicy(),
        analyzer=None,
        verifier=None,
        pr_number=1,
        head_sha="s",
        overlay_ref="pr:1",
        changed_paths=["a.py"],
        patches={"a.py": "@@ -1,2 +1,2 @@\n x\n+y\n"},
    )


class _FailVerifier:
    def verify(self, findings, deps):
        raise RuntimeError("verify channel down")


def test_verify_node_fail_soft_passes_findings_and_logs_failed_unit():
    """При исключении верификатора находки не теряются (fail-open),
    а причина сбоя дописывается в failed_units."""
    policy = ReviewPolicy()  # пропускает всё
    f = _make_finding()
    deps = _node_deps(policy, _FailVerifier())
    node = make_verify_node(deps)

    result = node({"findings": [f], "failed_units": []})

    assert result["verified"] == [f]
    assert any("verify channel down" in entry for entry in result["failed_units"])


class _FailListVCS:
    def list_existing_fingerprints(self, n):
        raise ConnectionError("GitHub API timeout")

    def publish_review(self, n, sha, summary, comments):
        pass


def test_assemble_node_fail_soft_on_existing_fingerprints():
    """Сбой при получении существующих fingerprint не блокирует публикацию:
    existing считается пустым, inline-замечания формируются, в failed_units — предупреждение."""
    vcs = _FailListVCS()
    deps = _assemble_deps(vcs)
    f = Finding("correctness", "high", "a.py", 2, "RIGHT", "bug", None, 0.9)
    node = make_assemble_node(deps)

    result = node({"verified": [f], "failed_units": []})

    assert len(result["inline_comments"]) == 1
    assert result["inline_comments"][0].path == "a.py"
    assert any("GitHub API timeout" in entry for entry in result["failed_units"])


class _FailPublishVCS:
    def publish_review(self, n, sha, summary, comments):
        raise ConnectionError("GitHub API timeout")


def test_publish_node_fail_soft_returns_published_false_and_failed_unit(caplog):
    """При исключении publish_review узел не падает: возвращает published=False,
    дописывает причину в failed_units и логирует человекочитаемое сообщение."""
    vcs = _FailPublishVCS()
    deps = Deps(
        vcs=vcs,
        retriever=None,
        graph=None,
        policy=ReviewPolicy(),
        analyzer=None,
        verifier=None,
        pr_number=1,
        head_sha="s",
        overlay_ref="pr:1",
        changed_paths=[],
        patches={},
    )
    node = make_publish_node(deps)

    with caplog.at_level(logging.ERROR):
        result = node({"summary": "## Авто-ревью\n", "inline_comments": [], "failed_units": []})

    assert result.get("published") is False
    assert any("publish: ConnectionError: GitHub API timeout" in entry
               for entry in result["failed_units"])
    assert any("Не удалось опубликовать ревью" in rec.message for rec in caplog.records)


def test_publish_node_success_returns_published_true():
    """При успешной публикации узел возвращает published=True."""
    class _OkVCS:
        def publish_review(self, n, sha, summary, comments):
            pass

    deps = Deps(
        vcs=_OkVCS(),
        retriever=None,
        graph=None,
        policy=ReviewPolicy(),
        analyzer=None,
        verifier=None,
        pr_number=1,
        head_sha="s",
        overlay_ref="pr:1",
        changed_paths=[],
        patches={},
    )
    node = make_publish_node(deps)
    result = node({"summary": "", "inline_comments": [], "failed_units": []})
    assert result.get("published") is True


class _GraphFailPublishVCS:
    def __init__(self):
        self.published = None

    def list_existing_fingerprints(self, n):
        return set()

    def publish_review(self, n, sha, summary, comments):
        raise RuntimeError("publish rejected")


class _GraphAnalyzer:
    def analyze(self, unit, deps):
        return [Finding("correctness", "high", "a.py", 2, "RIGHT", "bug", None, 0.9)]


def test_graph_does_not_crash_when_publish_fails():
    """Ошибка публикации не обрушивает граф: состояние доходит до END с published=False."""
    vcs = _GraphFailPublishVCS()
    deps = Deps(
        vcs=vcs,
        retriever=None,
        graph=None,
        policy=ReviewPolicy(),
        analyzer=_GraphAnalyzer(),
        verifier=_PassVerifier(),
        pr_number=1,
        head_sha="s",
        overlay_ref="pr:1",
        changed_paths=["a.py"],
        patches={"a.py": "@@ -1,2 +1,2 @@\n x\n+y\n"},
    )
    g = build_graph(deps, publish=True)
    state = g.invoke({
        "review_units": [ReviewUnit("a.py", ["a.py#f"], "y")],
        "findings": [], "failed_units": [], "verified": [],
        "summary": "", "inline_comments": [],
    })

    assert state.get("published") is False
    assert any("publish: RuntimeError: publish rejected" in entry
               for entry in state["failed_units"])
    assert len(state["inline_comments"]) == 1


class _PassVerifier:
    def verify(self, findings, deps):
        return findings
