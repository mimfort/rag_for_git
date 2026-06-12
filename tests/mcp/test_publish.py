"""Unit-тесты MCPReviewService.publish_review — детерминированный хвост ревью.

Проверяем: gate (отсев по severity), grounding (уточнение строки по code_quote),
dedup, assemble → inline, публикация в VCS, запись истории (fail-soft),
очистка overlay/сессии (в т.ч. при сбое VCS).

Фейки строятся по образцу tests/mcp/test_service.py: фейковый VCS записывает
publish_review в .published; фейковая history собирает прогоны в .runs.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService
from reviewer.vcs.base import PullRequest

# Исходник a.py: строка `x = 1` стоит на строке 2.
SOURCE_A = "y = 0\nx = 1\n"
# Дифф: line 1 — контекст (y = 0), line 2 — добавлена (x = 1) → RIGHT={1,2}.
PATCH_A = "@@ -1,1 +1,2 @@\n y = 0\n+x = 1"

RAW = {
    "category": "correctness", "severity": "high", "file": "a.py", "line": 2,
    "code_quote": "x = 1", "message": "bug here", "suggestion": None,
    "fix": None, "confidence": 0.9,
}


# ---------------------------------------------------------------------------
# Фейки
# ---------------------------------------------------------------------------

def _settings() -> Settings:
    s = Settings()
    s.review_history = True          # хотим проверить запись истории
    s.review_trace = False
    s.review_verdict_log = ""
    s.review_synthesis = False
    s.review_skip_drafts = True
    s.review_max_files = 50
    # Дефолты policy: severity_threshold=medium, min_confidence=0.5 — НЕ переопределяем,
    # тесты опираются на medium-порог (low отсекается).
    s.openrouter_api_key = "test"
    s.voyage_api_key = "test"
    s.github_token = "test"
    return s


def _components() -> MagicMock:
    c = MagicMock()
    c.store = MagicMock()
    c.store.deleted_refs = []
    c.store.delete_ref.side_effect = lambda ref: c.store.deleted_refs.append(ref)
    c.embedder = MagicMock()
    c.retriever = MagicMock()
    c.retriever.retrieve.return_value.as_context.return_value = "(результат поиска)"
    c.graph = MagicMock()
    c.graph.expand.return_value = set()
    c.graph.callers.return_value = set()
    c.graph.find_symbol.return_value = []
    c.llm_provider = MagicMock()
    return c


def _pr(number: int = 7) -> PullRequest:
    return PullRequest(
        number=number,
        base_sha="base123",
        head_sha="head456",
        base_ref="main",
        title="Test PR",
        body="",
        draft=False,
    )


class _FakeChangedFile:
    def __init__(self, path: str, status: str, patch: str | None) -> None:
        self.path = path
        self.status = status
        self.patch = patch


class _FakeVCS:
    """Фейковый VCS: записывает publish_review в .published, считает close."""

    def __init__(self, number: int, fails: bool = False) -> None:
        self._number = number
        self._fails = fails
        self.published: list[dict] = []
        self.close_calls = 0

    def get_pull_request(self, number: int) -> PullRequest:
        return _pr(self._number)

    def get_changed_files(self, number: int) -> list[_FakeChangedFile]:
        return [_FakeChangedFile("a.py", "modified", PATCH_A)]

    def get_file_at_ref(self, path: str, ref: str) -> str | None:
        if path == "a.py":
            return SOURCE_A
        return None  # .review.yml и прочее отсутствуют

    def list_existing_fingerprints(self, number: int) -> set[str]:
        return set()

    def publish_review(self, number, head_sha, summary, comments) -> None:
        if self._fails:
            raise RuntimeError("boom: VCS publish failed")
        self.published.append({
            "number": number, "head_sha": head_sha, "summary": summary,
            "comments": [{"path": c.path, "line": c.line, "side": c.side,
                          "body": c.body} for c in comments],
        })

    def compare_files(self, base_sha, head_sha):
        return []

    def close(self) -> None:
        self.close_calls += 1


class _FakeHistory:
    """Фейковая ReviewHistory: собирает прогоны в .runs."""

    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.findings: list[list[dict]] = []

    def record_run(self, run: dict, findings: list[dict], steps=None) -> int:
        self.runs.append(run)
        self.findings.append(findings)
        return len(self.runs)


class _FakeChunk:
    def __init__(self, node_id: str) -> None:
        self.node_id = node_id


def _fake_chunk(path, source):
    return [_FakeChunk(f"{path}#foo")]


def _make_mcp_service_with_publish(
    number: int = 7, vcs_fails: bool = False,
) -> tuple[MCPReviewService, _FakeVCS, _FakeHistory]:
    """Фабрика MCPReviewService с фейковыми компонентами + history.

    VCS отдаётся через vcs_factory (внешний — сервис его НЕ закрывает),
    history подменяется в _review_service, чтобы writes собирались в .runs.
    """
    settings = _settings()
    components = _components()
    vcs = _FakeVCS(number=number, fails=vcs_fails)
    history = _FakeHistory()
    svc = MCPReviewService(settings, components, vcs_factory=lambda o, r: vcs)
    # Подменяем хранилище истории на фейк (review_history=True → _ensure_history
    # вернул бы реальный ReviewHistory с подключением к Postgres).
    svc._review_service._history = history
    svc._review_service._history_owned = False
    return svc, vcs, history


# ---------------------------------------------------------------------------
# Тесты
#
# Каждый тест прогоняет prepare_review через реальный ReviewService.prepare —
# мокаем chunk_python/build_overlay, чтобы не дёргать tree-sitter/Voyage.
# ---------------------------------------------------------------------------

@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_posts_inline_and_records_history(_ov, _ch) -> None:
    svc, vcs, history = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    report = svc.publish_review("o/r", 7, summary="Overall fine", findings=[RAW])
    assert report["posted"] is True
    assert vcs.published[0]["comments"][0]["path"] == "a.py"
    assert history.runs[0]["pr_number"] == 7
    assert ("o/r", 7) not in svc._sessions          # сессия закрыта


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_dry_run_does_not_post_but_reports(_ov, _ch) -> None:
    svc, vcs, history = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    report = svc.publish_review("o/r", 7, summary="s", findings=[RAW], dry_run=True)
    assert report["posted"] is False and vcs.published == []
    assert report["inline"][0]["line"] == 2


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_gates_low_severity_and_grounds_line(_ov, _ch) -> None:
    svc, vcs, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    low = dict(RAW, severity="low")                       # ниже threshold=medium
    wrong_line = dict(RAW, line=99)                       # грунтовка по code_quote → 2
    report = svc.publish_review("o/r", 7, summary="s",
                                findings=[low, wrong_line], dry_run=True)
    assert report["dropped_by_gate"] == 1
    assert report["inline"][0]["line"] == 2


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_cleans_overlay_even_on_vcs_error(_ov, _ch) -> None:
    svc, vcs, _ = _make_mcp_service_with_publish(vcs_fails=True)
    svc.prepare_review("o/r", 7)
    report = svc.publish_review("o/r", 7, summary="s", findings=[RAW])
    assert report["posted"] is False and report["error"]
    assert "pr:7" in svc.components.store.deleted_refs


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_factory_vcs_not_closed(_ov, _ch) -> None:
    """Внешний (factory) VCS сервис НЕ закрывает — жизненным циклом владеет фабрика."""
    svc, vcs, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    svc.publish_review("o/r", 7, summary="s", findings=[RAW], dry_run=True)
    assert vcs.close_calls == 0


def test_publish_closes_internal_vcs() -> None:
    """Внутренний VCS (vcs_factory=None) cleanup закрывает fail-soft."""
    settings = _settings()
    components = _components()
    vcs = _FakeVCS(number=7)
    history = _FakeHistory()
    svc = MCPReviewService(settings, components, vcs_factory=None)
    svc._review_service._history = history
    svc._review_service._history_owned = False
    with patch.object(
        svc._review_service, "_create_vcs_provider", return_value=vcs,
    ), patch(
        "reviewer.services.review_service.chunk_python", side_effect=_fake_chunk,
    ), patch("reviewer.services.review_service.build_overlay"):
        svc.prepare_review("o/r", 7)
        svc.publish_review("o/r", 7, summary="s", findings=[RAW], dry_run=True)
    assert vcs.close_calls == 1
    assert ("o/r", 7) not in svc._sessions


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_history_failsoft(_ov, _ch) -> None:
    """Сбой записи истории не валит publish (run_id=None), публикация проходит."""
    svc, vcs, history = _make_mcp_service_with_publish()

    def boom(*a, **k):
        raise RuntimeError("history down")

    history.record_run = boom
    svc.prepare_review("o/r", 7)
    report = svc.publish_review("o/r", 7, summary="s", findings=[RAW])
    assert report["posted"] is True
    assert report["run_id"] is None
