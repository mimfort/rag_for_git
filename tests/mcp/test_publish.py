"""Unit-тесты MCPReviewService.publish_review — детерминированный хвост ревью.

Проверяем: gate (отсев по severity), grounding (уточнение строки по code_quote),
dedup, assemble → inline, публикация в VCS, запись истории (fail-soft),
очистка overlay/сессии (в т.ч. при сбое VCS).

Фейки строятся по образцу tests/mcp/test_service.py: фейковый VCS записывает
publish_review в .published; фейковая history собирает прогоны в .runs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from reviewer.config.settings import Settings
from reviewer.mcp.service import _MAX_SESSION_STEPS, MCPReviewService
from reviewer.vcs.base import Finding, PullRequest

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
    s.review_skip_drafts = True
    s.review_max_files = 50
    # Дефолты policy: severity_threshold=medium, min_confidence=0.5 — НЕ переопределяем,
    # тесты опираются на medium-порог (low отсекается).
    s.voyage_api_key = "test"
    s.github_token = "test"
    s.review_session_persist = False     # unit-тесты не трогают Postgres-таблицу сессий
    return s


def _components() -> MagicMock:
    c = MagicMock()
    c.store = MagicMock()
    c.store.deleted_refs = []
    c.store.delete_ref.side_effect = lambda repo, ref: c.store.deleted_refs.append(ref)
    c.embedder = MagicMock()
    c.retriever = MagicMock()
    c.retriever.retrieve.return_value.as_context.return_value = "(результат поиска)"
    c.graph = MagicMock()
    c.graph.expand.return_value = set()
    c.graph.callers.return_value = set()
    c.graph.find_symbol.return_value = []
    c.graph.in_degree.return_value = {}
    c.retriever.store.fetch_nodes_at.return_value = []
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

    def __init__(
        self,
        number: int,
        fails: bool = False,
        existing_fps: set[str] | None = None,
    ) -> None:
        self._number = number
        self._fails = fails
        self._existing_fps = existing_fps or set()
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
        return set(self._existing_fps)

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
        self.steps: list[list[dict] | None] = []

    def record_run(self, run: dict, findings: list[dict], steps=None) -> int:
        self.runs.append(run)
        self.findings.append(findings)
        self.steps.append(steps)
        return len(self.runs)


class _FakeChunk:
    def __init__(self, node_id: str) -> None:
        self.node_id = node_id


def _fake_chunk(path, source):
    return [_FakeChunk(f"{path}#foo")]


def _make_mcp_service_with_publish(
    number: int = 7,
    vcs_fails: bool = False,
    existing_fps: set[str] | None = None,
) -> tuple[MCPReviewService, _FakeVCS, _FakeHistory]:
    """Фабрика MCPReviewService с фейковыми компонентами + history.

    VCS отдаётся через vcs_factory (внешний — сервис его НЕ закрывает),
    history подменяется в _review_service, чтобы writes собирались в .runs.
    """
    settings = _settings()
    components = _components()
    vcs = _FakeVCS(number=number, fails=vcs_fails, existing_fps=existing_fps)
    history = _FakeHistory()
    svc = MCPReviewService(settings, components, vcs_factory=lambda o, r: vcs)
    # Подменяем хранилище истории на фейк (review_history=True → _ensure_history
    # вернул бы реальный ReviewHistory с подключением к Postgres).
    svc._review_service._history = history
    svc._review_service._history_owned = False
    return svc, vcs, history


def _submit_then_publish(svc, repo, pr, findings, *, summary="s", dry_run=False,
                         verdicts=None, task_key=None, **publish_kwargs):
    """PRI-156: вместо publish_review(findings=...) — submit + publish из сессии."""
    if findings:
        svc.submit_findings(repo, pr, findings)
    if verdicts:
        svc.submit_verdicts(repo, pr, verdicts)
    return svc.publish_review(repo, pr, summary=summary, dry_run=dry_run,
                              task_key=task_key, **publish_kwargs)


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
    report = _submit_then_publish(svc, "o/r", 7, [RAW], summary="Overall fine")
    assert report["posted"] is True
    assert vcs.published[0]["comments"][0]["path"] == "a.py"
    assert history.runs[0]["pr_number"] == 7
    assert ("o/r", 7) not in svc._sessions          # сессия закрыта


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_dry_run_does_not_post_but_reports(_ov, _ch) -> None:
    svc, vcs, history = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    report = _submit_then_publish(svc, "o/r", 7, [RAW], dry_run=True)
    assert report["posted"] is False and vcs.published == []
    assert report["inline"][0]["line"] == 2
    assert report["capped"] == 0
    # История пишется и в dry_run — с фактическими счётчиками
    run = history.runs[0]
    assert run["dry_run"] is True
    assert run["model"] == "claude-code"
    assert run["findings_analyzed"] == 1
    assert run["findings_kept"] == 1
    assert run["status"] == "ok"


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_gates_low_severity_and_grounds_line(_ov, _ch) -> None:
    svc, vcs, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    low = dict(RAW, severity="low")                       # ниже threshold=medium
    wrong_line = dict(RAW, line=99)                       # грунтовка по code_quote → 2
    report = _submit_then_publish(svc, "o/r", 7, [low, wrong_line], dry_run=True)
    assert report["dropped_by_gate"] == 1
    assert report["inline"][0]["line"] == 2


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_cleans_overlay_even_on_vcs_error(_ov, _ch) -> None:
    svc, vcs, history = _make_mcp_service_with_publish(vcs_fails=True)
    svc.prepare_review("o/r", 7)
    report = _submit_then_publish(svc, "o/r", 7, [RAW])
    assert report["posted"] is False and report["error"]
    # prepare сам чистит overlay один раз (self-healing) + cleanup после publish
    assert svc.components.store.deleted_refs.count("pr:7") == 2
    # История: status=error, непустой error_text, находки помечены published=False
    run = history.runs[0]
    assert run["status"] == "error"
    assert run["error_text"]
    assert history.findings[0], "ожидали записанные находки"
    assert all(row["published"] is False for row in history.findings[0])


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_factory_vcs_not_closed(_ov, _ch) -> None:
    """Внешний (factory) VCS сервис НЕ закрывает — жизненным циклом владеет фабрика."""
    svc, vcs, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    _submit_then_publish(svc, "o/r", 7, [RAW], dry_run=True)
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
        _submit_then_publish(svc, "o/r", 7, [RAW], dry_run=True)
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
    report = _submit_then_publish(svc, "o/r", 7, [RAW])
    assert report["posted"] is True
    assert report["run_id"] is None


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_invalid_always_zero_with_enforced_schema(_ov, _ch) -> None:
    """Все candidates валидны (validated на submit) → invalid всегда 0."""
    svc, _, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    report = _submit_then_publish(svc, "o/r", 7, [RAW], dry_run=True)
    assert report["invalid"] == 0


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_skips_already_posted_fingerprints(_ov, _ch) -> None:
    """Идемпотентность: fingerprint прошлого прогона → inline отфильтрован."""
    fp = Finding(
        category="correctness", severity="high", file="a.py", line=2,
        side="RIGHT", message="bug here", suggestion=None, confidence=0.9,
    ).fingerprint()
    svc, vcs, _ = _make_mcp_service_with_publish(existing_fps={fp})
    svc.prepare_review("o/r", 7)
    report = _submit_then_publish(svc, "o/r", 7, [RAW], dry_run=True)
    assert report["already_posted"] == 1
    assert report["inline"] == []


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_empty_findings_posts_summary(_ov, _ch) -> None:
    """Пустой список находок: ревью публикуется со сводкой «Замечаний не найдено»."""
    svc, vcs, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    report = _submit_then_publish(svc, "o/r", 7, [], summary="s")
    assert report["posted"] is True
    assert report["inline"] == []
    assert "Замечаний не найдено" in report["summary"]
    assert "Замечаний не найдено" in vcs.published[0]["summary"]


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_dedups_near_identical_findings(_ov, _ch) -> None:
    """Две одинаковые находки схлопываются в одну: deduped=1, один inline."""
    svc, vcs, history = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    report = _submit_then_publish(svc, "o/r", 7, [RAW, dict(RAW)], dry_run=True)
    assert report["deduped"] == 1
    assert len(report["inline"]) == 1
    # findings_analyzed — по уникальным fingerprint (точный дубль не раздувает счётчик)
    assert history.runs[0]["findings_analyzed"] == 1
    assert history.runs[0]["findings_kept"] == 1


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_skipped_existing_not_counted_in_comments_summary(_ov, _ch) -> None:
    """skipped_existing (повторная находка с тем же fingerprint) НЕ учитывается
    в comments_summary — она published=False, не является summary-комментарием."""
    fp = Finding(
        category="correctness", severity="high", file="a.py", line=2,
        side="RIGHT", message="bug here", suggestion=None, confidence=0.9,
    ).fingerprint()
    # Передаём две находки: одна — уже опубликована (existing_fps), другая новая
    # и попадёт в summary (line=None → не в дифф).
    summary_raw = dict(RAW, line=None, code_quote=None, message="summary finding")
    svc, vcs, history = _make_mcp_service_with_publish(existing_fps={fp})
    svc.prepare_review("o/r", 7)
    report = _submit_then_publish(
        svc, "o/r", 7, [RAW, summary_raw], dry_run=True,
    )
    # RAW пропущена (already_posted), summary_raw уходит в summary
    assert report["already_posted"] == 1
    # comments_summary должен быть 1 (только summary_raw, без skipped_existing)
    run = history.runs[0]
    assert run["comments_summary"] == 1, (
        f"comments_summary должен быть 1 (только реально опубликованная), "
        f"получили {run['comments_summary']}"
    )


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_annotates_centrality_from_graph(_ov, _ch) -> None:
    from reviewer.index.store import Retrieved
    svc, vcs, _ = _make_mcp_service_with_publish()
    # Находка RAW на a.py:2 ложится в символ a.py#foo (диапазон 1..10); граф даёт degree=4.
    svc.components.retriever.store.fetch_nodes_at.return_value = [
        Retrieved("a.py#foo", "a.py", "foo", "function", 1, 10, "", 0.0)
    ]
    svc.components.graph.in_degree.return_value = {"a.py#foo": 4}
    svc.prepare_review("o/r", 7)
    _submit_then_publish(svc, "o/r", 7, [RAW], dry_run=True)
    # Центральность была запрошена ровно для пойманного символа.
    svc.components.graph.in_degree.assert_called_once()
    assert svc.components.graph.in_degree.call_args.args[1] == ["a.py#foo"]


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_coerced_findings_publish(_ov, _ch) -> None:
    """Коэрция severity/suggestion на submit → medium проходит гейт, suggestion=None не в теле."""
    svc, vcs, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    pack = [dict(RAW, severity=["high"], message="unhashable severity"),
            dict(RAW, suggestion=42, message="non-string suggestion")]
    report = _submit_then_publish(svc, "o/r", 7, pack, dry_run=True)
    assert report["dropped_by_gate"] == 0
    assert len(report["inline"]) == 2
    inline_bodies = [c["body"] for c in report["inline"]]
    assert not any("42" in b and "suggestion" in b.lower() for b in inline_bodies)


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_drops_findings_with_is_real_false(_ov, _ch) -> None:
    """Явный is_real=false → находка отсеяна; verify_rejected=1."""
    svc, _, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    report = _submit_then_publish(svc, "o/r", 7, [RAW], dry_run=True,
                                  verdicts=[{"id": "f1", "is_real": False}])
    assert report["verify_rejected"] == 1
    assert report["inline"] == []


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_keeps_finding_without_verdict(_ov, _ch) -> None:
    """Нет вердикта (verify умер/частичный) → находка остаётся (recall-safe)."""
    svc, _, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    report = _submit_then_publish(svc, "o/r", 7, [RAW], dry_run=True)  # без verdicts
    assert report["verify_rejected"] == 0
    assert report["inline"][0]["line"] == 2


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_records_real_metadata(_ov, _ch) -> None:
    """PRI-209: без override метаданные берутся из сессии/дефолтов."""
    svc, _, history = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    started = datetime.now(timezone.utc) - timedelta(seconds=2)
    report = _submit_then_publish(svc, "o/r", 7, [RAW], dry_run=True,
                                  started_at=started)
    assert report["posted"] is False
    run = history.runs[0]
    assert run["model"] == "claude-code"
    assert run["duration_ms"] >= 0
    assert run["usage"] is None
    assert run["total_cost"] is None


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_records_server_steps(_ov, _ch) -> None:
    svc, vcs, history = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    svc.search_code("o/r", 7, "token check")
    _submit_then_publish(svc, "o/r", 7, [RAW], dry_run=True)
    assert len(history.steps[0]) >= 1
    assert any(step["name"] == "search_code" for step in history.steps[0])


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_accepts_metadata_override(_ov, _ch) -> None:
    """PRI-209: клиент может передать метаданные LLM-прохода в publish_review."""
    svc, _, history = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    svc.submit_findings("o/r", 7, [RAW])
    started_iso = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    report = svc.publish_review(
        "o/r", 7, summary="s", dry_run=True,
        model="custom-model",
        model_verify="verify-model",
        usage={"input_tokens": 100, "output_tokens": 50},
        total_cost=0.00123,
        started_at=started_iso,
        steps=[{"tool": "step1"}],
    )
    assert report["posted"] is False
    run = history.runs[0]
    assert run["model"] == "custom-model"
    assert run["model_verify"] == "verify-model"
    assert run["usage"] == {"input_tokens": 100, "output_tokens": 50}
    assert run["total_cost"] == 0.00123
    assert run["duration_ms"] >= 0
    assert history.steps[0] == [{"tool": "step1", "seq": 0}]


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_caps_client_steps_when_session_full(_ov, _ch) -> None:
    """PRI-209: при заполненной сессии (session.steps == _MAX_SESSION_STEPS)
    клиентские шаги отбрасываются целиком.

    Регрессия на баг «отрицательного нуля»: при max_client == 0 срез
    client_steps[-0:] раньше возвращал ВЕСЬ список клиента, и кэп не срабатывал.
    """
    svc, _, history = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    s = svc._session("o/r", 7)
    s.steps = [{"stage": "analyze", "seq": i} for i in range(_MAX_SESSION_STEPS)]
    svc.submit_findings("o/r", 7, [RAW])
    svc.publish_review(
        "o/r", 7, summary="s", dry_run=True,
        steps=[{"tool": "client"} for _ in range(5)],
    )
    # Клиентские шаги отброшены целиком: в истории только серверные _MAX_SESSION_STEPS.
    assert len(history.steps[0]) == _MAX_SESSION_STEPS
    assert all(step.get("tool") != "client" for step in history.steps[0])


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_ignores_malformed_started_at(_ov, _ch) -> None:
    svc, vcs, history = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    report = svc.publish_review(
        "o/r", 7, summary="s", dry_run=True, started_at="not-a-timestamp",
    )
    assert report is not None
    run = history.runs[0]
    assert run["duration_ms"] >= 0
    assert run["started_at"] is not None


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_merges_server_and_client_steps(_ov, _ch) -> None:
    svc, vcs, history = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    svc.search_code("o/r", 7, "token check")
    svc.get_related_symbols("o/r", 7, "a.py#f")
    client_steps = [
        {"stage": "synthesize", "unit": "(summary)", "seq": 0, "kind": "llm_call", "name": "summary", "text": "ok", "tool_calls": None, "tokens": 10, "cost": 0.01},
        {"stage": "synthesize", "unit": "(summary)", "seq": 1, "kind": "llm_call", "name": "summary2", "text": "ok2", "tool_calls": None, "tokens": 5, "cost": 0.005},
    ]
    svc.publish_review("o/r", 7, summary="s", dry_run=True, steps=client_steps)
    recorded = history.steps[0]
    assert any(step["name"] == "search_code" for step in recorded)
    assert any(step["name"] == "get_related_symbols" for step in recorded)
    assert any(step["name"] == "summary" for step in recorded)
    seqs = [step["seq"] for step in recorded]
    assert seqs == list(range(len(recorded)))
