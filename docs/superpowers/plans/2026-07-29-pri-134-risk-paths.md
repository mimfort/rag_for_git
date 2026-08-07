# PRI-134 Risk Paths Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Доставить в global `review-pr` bounded и grounded проверку опасных non-Python изменений, не расширяя Python-only index/overlay/graph.

**Architecture:** Чистый сервисный классификатор превращает полный список `ChangedFile` в типизированные `RiskPath` и deterministic overflow. `ReviewService` добавляет их в `PreparedReview`, MCP сериализует отдельный diff payload, а plugin skill запускает один условный safety dimension. Сигнал маршрутизирует анализ, но finding создаётся только по конкретному diff evidence и проходит существующие verify/gate/publish.

**Tech Stack:** Python 3.11+, dataclasses, FastMCP payloads, pytest, Markdown skill prompts.

## Global Constraints

- `units`, `changed_paths`, overlay, chunker, graph и structural diff остаются Python-only.
- Классификатор читает только `path`, `status` и patch metadata; embeddings для risk files запрещены.
- Обычные `.yaml`, `.json` и `.toml` без risk-якоря не запускают дополнительный анализ.
- Причины риска: `credential_like`, `migration`, `ci_deploy_infra`, `dependency`.
- Порядок причин и файлов детерминирован; hard cap первой версии равен 10.
- Path/reason не являются finding evidence; публикация требует конкретной проблемы в diff.
- Старые persisted session payloads без risk-полей восстанавливаются с пустыми списками.
- Новых dependencies, category и policy keys не добавлять.

---

## File Structure

- Create `reviewer/services/risk_paths.py`: тип `RiskPath`, path-only классификация, ranking и cap.
- Create `tests/services/test_risk_paths.py`: исчерпывающий чистый контракт классификатора.
- Modify `reviewer/services/review_service.py`: построение risk signals и grounding sources без overlay.
- Modify `reviewer/mcp/session_serde.py`: JSON round-trip и backward-compatible read.
- Modify `reviewer/mcp/service.py`: публичный MCP diff payload.
- Modify `tests/services/test_review_service.py`: интеграция классификатора в prepare.
- Modify `tests/mcp/test_session_serde.py`: round-trip и legacy payload.
- Modify `tests/mcp/test_service.py`: публичная shape risk payload.
- Create `plugin/skills/review-pr/references/risk-changes-prompt.md`: evidence-first safety dimension.
- Modify `plugin/skills/review-pr/SKILL.md`: условный dispatch и summary reporting.
- Modify `tests/skills/test_assembled_prompts.py`: assembled prompt guard.
- Create `tests/skills/test_review_pr_risk_paths.py`: orchestration contract guard.

### Task 1: Чистый классификатор risk paths

**Files:**

- Create: `reviewer/services/risk_paths.py`
- Create: `tests/services/test_risk_paths.py`

**Interfaces:**

- Consumes: `reviewer.vcs.base.ChangedFile`.
- Produces:
  `RiskReason = Literal["credential_like", "migration", "ci_deploy_infra", "dependency"]`;
  `RiskPath(path: str, status: str, reasons: tuple[RiskReason, ...])`;
  `select_risk_paths(files: Sequence[ChangedFile], limit: int = 10) -> tuple[list[RiskPath], list[str]]`.

- [ ] **Step 1: Написать failing tests для семейств и anti-noise**

```python
@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("db/migrations/001_drop.sql", "migration"),
        (".github/workflows/release.yml", "ci_deploy_infra"),
        ("infra/main.tf", "ci_deploy_infra"),
        ("uv.lock", "dependency"),
        ("pyproject.toml", "dependency"),
        (".env.production", "credential_like"),
        ("deploy/secrets.yaml", "credential_like"),
    ],
)
def test_classifies_supported_risk_families(path, reason):
    selected, skipped = select_risk_paths([_changed(path)])
    assert skipped == []
    assert len(selected) == 1
    assert reason in selected[0].reasons


@pytest.mark.parametrize(
    "path",
    ["config/app.yaml", "fixtures/data.json", "ruff.toml", "reviewer/app.py"],
)
def test_ordinary_config_and_python_are_quiet(path):
    assert select_risk_paths([_changed(path)]) == ([], [])
```

- [ ] **Step 2: Запустить тесты и подтвердить ожидаемый import failure**

Run:
`/Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/pytest -q tests/services/test_risk_paths.py`

Expected: FAIL during collection with `ModuleNotFoundError: reviewer.services.risk_paths`.

- [ ] **Step 3: Реализовать типы и conservative path classification**

```python
RiskReason = Literal[
    "credential_like", "migration", "ci_deploy_infra", "dependency"
]
RISK_PATH_LIMIT = 10


@dataclass(frozen=True)
class RiskPath:
    path: str
    status: str
    reasons: tuple[RiskReason, ...]


def classify_risk_path(path: str) -> tuple[RiskReason, ...]:
    normalized = path.replace("\\", "/").strip("/")
    lowered = normalized.lower()
    basename = lowered.rsplit("/", 1)[-1]
    segments = tuple(part for part in lowered.split("/") if part)
    if basename.endswith(".py"):
        return ()

    stem = basename.rsplit(".", 1)[0]
    reasons: set[RiskReason] = set()
    if (
        basename.startswith(".env")
        or basename in {".npmrc", ".pypirc", ".netrc"}
        or stem in {"secret", "secrets", "credential", "credentials"}
        or {"secret", "secrets", "credential", "credentials"} & set(segments)
    ):
        reasons.add("credential_like")
    if (
        basename.endswith(".sql")
        or "migrations" in segments
        or _contains_pair(segments, "alembic", "versions")
        or _contains_pair(segments, "db", "migrate")
    ):
        reasons.add("migration")
    if (
        basename in _CI_FILENAMES
        or basename.startswith("dockerfile")
        or _is_compose_file(basename)
        or _contains_pair(segments, ".github", "workflows")
        or bool(_INFRA_SEGMENTS & set(segments))
        or basename.endswith((".tf", ".tfvars"))
    ):
        reasons.add("ci_deploy_infra")
    if (
        basename in _DEPENDENCY_NAMES
        or (basename.startswith("requirements") and basename.endswith(".txt"))
    ):
        reasons.add("dependency")
    return tuple(reason for reason in _REASON_ORDER if reason in reasons)
```

Exact conservative sets:

```python
_DEPENDENCY_NAMES = {
    "uv.lock", "poetry.lock", "pipfile", "pipfile.lock",
    "pyproject.toml", "package.json", "package-lock.json",
    "yarn.lock", "pnpm-lock.yaml", "cargo.toml", "cargo.lock",
    "go.mod", "go.sum", "composer.json", "composer.lock",
    "gemfile", "gemfile.lock",
}
_CI_FILENAMES = {".gitlab-ci.yml", ".gitlab-ci.yaml", "jenkinsfile"}
_INFRA_SEGMENTS = {
    "deploy", "deployment", "helm", "charts", "k8s", "kubernetes",
    "terraform", "infra",
}


def _contains_pair(segments: tuple[str, ...], first: str, second: str) -> bool:
    return any(
        left == first and right == second
        for left, right in zip(segments, segments[1:])
    )


def _is_compose_file(basename: str) -> bool:
    return basename in {
        "compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml",
    }


def _rank(candidate: tuple[ChangedFile, RiskPath]) -> tuple:
    file, signal = candidate
    patch = file.patch or ""
    reason_priority = min(_REASON_ORDER.index(reason) for reason in signal.reasons)
    return (
        reason_priority,
        -sum(line.startswith("@@") for line in patch.splitlines()),
        -len(patch),
        signal.path.replace("\\", "/").lower(),
        signal.path,
        signal.status,
    )


def select_risk_paths(
    files: Sequence[ChangedFile],
    limit: int = RISK_PATH_LIMIT,
) -> tuple[list[RiskPath], list[str]]:
    ranked = sorted(
        (
            (file, RiskPath(file.path, file.status, reasons))
            for file in files
            if (reasons := classify_risk_path(file.path))
        ),
        key=_rank,
    )
    unique: dict[str, RiskPath] = {}
    for _, signal in ranked:
        unique.setdefault(signal.path.replace("\\", "/").lower(), signal)
    signals = list(unique.values())
    selected = signals[:max(limit, 0)]
    skipped = [signal.path for signal in signals[max(limit, 0):]]
    return selected, skipped
```

`config/app.yaml` не должен матчиться только по extension. `secrets`/`credentials`
считаются якорями только как basename stem или отдельный path segment, чтобы
`docs/credentials-guide.md` не создавал сигнал.

- [ ] **Step 4: Добавить deterministic dedup/ranking/cap tests**

```python
def test_deduplicates_multi_reason_path_and_orders_reasons():
    selected, _ = select_risk_paths([_changed("infra/secrets.env")])
    assert selected == [
        RiskPath(
            "infra/secrets.env",
            "modified",
            ("credential_like", "ci_deploy_infra"),
        )
    ]


def test_orders_by_reason_then_diff_importance_then_path_and_caps():
    files = [_changed(f"migrations/{name}.sql") for name in ("z", "a", "m")]
    selected, skipped = select_risk_paths(files, limit=2)
    assert [item.path for item in selected] == [
        "migrations/a.sql",
        "migrations/m.sql",
    ]
    assert skipped == ["migrations/z.sql"]


def test_removed_risk_file_is_preserved():
    selected, _ = select_risk_paths([_changed("Dockerfile", status="removed")])
    assert selected[0].status == "removed"
```

Use key `(minimum reason priority, -hunk_count, -patch_length, normalized_path)`;
preserve the original `ChangedFile.path` in `RiskPath` and skipped output.

- [ ] **Step 5: Запустить classifier tests**

Run:
`/Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/pytest -q tests/services/test_risk_paths.py`

Expected: PASS.

- [ ] **Step 6: Запустить Ruff для новых файлов**

Run:
`/Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/ruff check reviewer/services/risk_paths.py tests/services/test_risk_paths.py`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add reviewer/services/risk_paths.py tests/services/test_risk_paths.py
git commit -m "feat(services): классифицировать risk paths (PRI-134)"
```

### Task 2: PreparedReview, persisted session и MCP payload

**Files:**

- Modify: `reviewer/services/review_service.py`
- Modify: `reviewer/mcp/session_serde.py`
- Modify: `reviewer/mcp/service.py`
- Modify: `tests/services/test_review_service.py`
- Modify: `tests/mcp/test_session_serde.py`
- Modify: `tests/mcp/test_service.py`

**Interfaces:**

- Consumes:
  `select_risk_paths(files, limit=RISK_PATH_LIMIT)` and `RiskPath` from Task 1.
- Produces:
  `PreparedReview.risk_paths: list[RiskPath]`;
  `PreparedReview.risk_skipped_paths: list[str]`;
  MCP payload items
  `{path, status, reasons, patch, commentable_right, commentable_left}`.

- [ ] **Step 1: Написать failing prepare integration test**

Add a VCS mock whose `get_file_at_ref` returns source by path:

```python
def test_prepare_routes_risk_files_without_indexing(settings, components):
    files = [
        _changed("reviewer/app.py"),
        _changed("migrations/001.sql"),
        _changed("config/app.yaml"),
    ]
    vcs = _vcs_with_files(files)
    vcs.get_file_at_ref.side_effect = lambda path, ref: {
        ".review.yml": "",
        "reviewer/app.py": "def foo(): pass\n",
        "migrations/001.sql": "DROP TABLE old_data;\n",
        "config/app.yaml": "debug: false\n",
    }.get(path, "")

    prepared = ReviewService(settings, components).prepare(
        "owner", "repo", 1, vcs_provider=vcs
    )

    assert [u.path for u in prepared.units] == ["reviewer/app.py"]
    assert prepared.changed_paths == ["reviewer/app.py"]
    assert [item.path for item in prepared.risk_paths] == ["migrations/001.sql"]
    assert prepared.sources["migrations/001.sql"] == "DROP TABLE old_data;\n"
    overlay_paths = build_overlay_mock.call_args.args[4]
    assert overlay_paths == ["reviewer/app.py"]
```

Add overflow coverage by passing 11 `.env.<n>` files through `prepare()` and asserting
that 10 land in `risk_paths` and the last deterministic path lands in
`risk_skipped_paths`.

- [ ] **Step 2: Запустить prepare test и подтвердить missing fields**

Run:
`/Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/pytest -q tests/services/test_review_service.py -k risk`

Expected: FAIL because `PreparedReview` has no `risk_paths`.

- [ ] **Step 3: Wire risk selection and grounding sources into ReviewService**

```python
from dataclasses import dataclass, field
from reviewer.services.risk_paths import RiskPath, select_risk_paths


@dataclass
class PreparedReview:
    risk_paths: list[RiskPath] = field(default_factory=list)
    risk_skipped_paths: list[str] = field(default_factory=list)
```

Immediately after `files = vcs.get_changed_files(pr_number)`:

```python
risk_paths, risk_skipped_paths = select_risk_paths(files)
```

Keep the existing Python `selected_files`/`changed`/`build_overlay` flow unchanged.
After Python head sources are loaded, fetch only selected non-removed risk sources:

```python
risk_sources: dict[str, str] = {}
for item in risk_paths:
    if item.status == "removed":
        continue
    src = vcs.get_file_at_ref(item.path, prq.head_sha)
    if src:
        risk_sources[item.path] = src
```

After `sources = {u.path: u.new_source for u in units}`, call
`sources.update(risk_sources)`, and pass both new fields to `PreparedReview`.
Do not pass risk paths to `build_overlay`, `chunk_python`, `changed_node_ids` or
`skipped_paths`.

- [ ] **Step 4: Добавить failing session serde tests**

Extend `_prepared()` with:

```python
risk_paths=[
    RiskPath("migrations/001.sql", "modified", ("migration",)),
],
risk_skipped_paths=["infra/overflow.tf"],
```

Assert both fields survive JSON round-trip. Add a legacy compatibility test:

```python
def test_from_payload_defaults_missing_risk_fields():
    payload = json.loads(json.dumps(to_payload(_prepared(_DummyVCS()))))
    payload.pop("risk_paths")
    payload.pop("risk_skipped_paths")
    restored = from_payload(payload, _DummyVCS())
    assert restored.risk_paths == []
    assert restored.risk_skipped_paths == []
```

- [ ] **Step 5: Реализовать explicit JSON serde**

In `to_payload`:

```python
"risk_paths": [asdict(item) for item in prepared.risk_paths],
"risk_skipped_paths": prepared.risk_skipped_paths,
```

In `from_payload`:

```python
risk_paths=[
    RiskPath(
        path=item["path"],
        status=item["status"],
        reasons=tuple(item["reasons"]),
    )
    for item in d.get("risk_paths", [])
],
risk_skipped_paths=d.get("risk_skipped_paths", []),
```

Keep `test_to_payload_covers_all_prepared_fields` passing.

- [ ] **Step 6: Написать failing MCP payload shape test**

Prepare a service with `migrations/001.sql` and assert:

```python
risk = out["risk_paths"][0]
assert risk["path"] == "migrations/001.sql"
assert risk["status"] == "modified"
assert risk["reasons"] == ["migration"]
assert risk["patch"].startswith("@@")
assert isinstance(risk["commentable_right"], list)
assert isinstance(risk["commentable_left"], list)
assert out["risk_skipped_paths"] == []
```

- [ ] **Step 7: Реализовать MCP payload formatting**

In `_prepared_payload`, build risk items with the same `commentable_lines()` helper as
ordinary units:

```python
risk_paths = []
for item in p.risk_paths:
    patch = p.patches.get(item.path)
    lines = commentable_lines(patch)
    risk_paths.append({
        "path": item.path,
        "status": item.status,
        "reasons": list(item.reasons),
        "patch": patch,
        "commentable_right": sorted(lines["RIGHT"]),
        "commentable_left": sorted(lines["LEFT"]),
    })
```

Return `risk_paths` and a copy of `risk_skipped_paths` as top-level fields.

- [ ] **Step 8: Запустить service/MCP/session tests**

Run:

```bash
/Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/pytest -q \
  tests/services/test_review_service.py \
  tests/mcp/test_session_serde.py \
  tests/mcp/test_service.py
```

Expected: PASS.

- [ ] **Step 9: Ruff и commit**

```bash
/Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/ruff check \
  reviewer/services/review_service.py reviewer/mcp/session_serde.py \
  reviewer/mcp/service.py tests/services/test_review_service.py \
  tests/mcp/test_session_serde.py tests/mcp/test_service.py
git add reviewer/services/review_service.py reviewer/mcp/session_serde.py \
  reviewer/mcp/service.py tests/services/test_review_service.py \
  tests/mcp/test_session_serde.py tests/mcp/test_service.py
git commit -m "feat(mcp): передавать bounded risk diffs (PRI-134)"
```

### Task 3: Whole-diff safety dimension в global plugin

**Files:**

- Create: `plugin/skills/review-pr/references/risk-changes-prompt.md`
- Modify: `plugin/skills/review-pr/SKILL.md`
- Modify: `tests/skills/test_assembled_prompts.py`
- Create: `tests/skills/test_review_pr_risk_paths.py`

**Interfaces:**

- Consumes top-level prepare payload:
  `risk_paths[]` and `risk_skipped_paths[]` from Task 2.
- Produces candidate findings via
  `submit_findings(repo, pr, findings=[finding])`, category `correctness|security`.

- [ ] **Step 1: Написать failing assembled-prompt guard**

```python
def test_risk_changes_assembled_has_schema_and_evidence_guards():
    prompt = assemble("review-pr/references/risk-changes-prompt.md")
    assert '"severity": "low|medium|high|critical"' in prompt
    assert "submit_findings" in prompt
    assert "path or reason is not evidence" in prompt
    assert "ordinary configuration" in prompt
    assert "do not repeat a credential value" in prompt
    assert "commentable_right" in prompt
```

- [ ] **Step 2: Написать failing orchestration guard**

```python
def test_review_pr_dispatches_risk_dimension_conditionally():
    text = SKILL.read_text("utf-8")
    block = re.search(
        r"risk changes.*?(?=\\n\\s*- blast-radius:)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    assert block
    assert "ONLY if `risk_paths` is non-empty" in block.group()
    assert "risk-changes-prompt.md" in block.group()
    assert "risk_skipped_paths" in text
```

- [ ] **Step 3: Запустить skill tests и подтвердить отсутствие prompt/contract**

Run:

```bash
/Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/pytest -q \
  tests/skills/test_assembled_prompts.py \
  tests/skills/test_review_pr_risk_paths.py
```

Expected: FAIL because reference prompt and orchestration block do not exist.

- [ ] **Step 4: Создать evidence-first risk prompt**

The prompt must:

```markdown
You are the bounded risk-changes dimension for one pull request.

Inputs are `risk_paths` items with path/status/reasons/patch/commentable lines.
A path or reason is not evidence. Report only a concrete defect visible in the
diff and its direct consequences. Ordinary configuration changes are not findings.

Check only:
- migration ordering, irreversible/destructive operations and code/schema mismatch;
- CI/deploy/infra behavior, privilege exposure and rollback breakage;
- changed dependency manifest/lock consistency;
- credential-like additions that contain an actual credential value.

For credentials, use the exact changed line only as `code_quote`; do not repeat a
credential value in message, suggestion, fix or summary.
If patch is missing/binary, or evidence is ambiguous, submit no finding.
```

Include:

```markdown
<!-- include: _common/anti-hallucination.md -->
<!-- include: _common/findings-schema.md -->
```

Require a line from `commentable_right`/`commentable_left`, exact `code_quote`,
categories `correctness` or `security`, and submission through `submit_findings`.
Removed-file evidence may use LEFT coordinates; when exact grounding is impossible,
use `line: null` so the deterministic tail moves it to summary.

- [ ] **Step 5: Update review-pr orchestration contract**

In Prepare payload docs add:

```markdown
- `risk_paths`: bounded non-Python items with
  `{path, status, reasons, patch, commentable_right, commentable_left}`
- `risk_skipped_paths`: classified paths omitted by the deterministic cap
```

In Dimensions add one parallel branch before blast-radius:

```markdown
- risk changes (ONLY if `risk_paths` is non-empty): dispatch one subagent with
  `references/risk-changes-prompt.md`, every risk item, PR title/body,
  repo/pr identifiers and output language. It submits only grounded
  `correctness`/`security` findings via `submit_findings`.
```

In Analyze/Failure handling/Publish summary require:

- Python per-unit fan-out remains based only on `units`;
- failed risk subagent is fail-open and named in summary;
- every `risk_skipped_paths` entry is reported as not inspected.

- [ ] **Step 6: Запустить skill tests**

Run:

```bash
/Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/pytest -q \
  tests/skills/test_assembled_prompts.py \
  tests/skills/test_review_pr_risk_paths.py \
  tests/skills/test_review_pr_store_first.py
```

Expected: PASS.

- [ ] **Step 7: Запустить целевой regression suite**

Run:

```bash
/Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/pytest -q \
  tests/services tests/mcp tests/skills tests/entrypoints/test_cli.py
/Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/ruff check \
  reviewer/services reviewer/mcp tests/services tests/mcp tests/skills
```

Expected: PASS. Если collection снова требует отсутствующий `fastapi`, не расширять
scope кода: установить project extra `.[web]` в worktree environment либо зафиксировать
environment-only blocker и сохранить успешные целевые прогоны.

- [ ] **Step 8: Commit**

```bash
git add plugin/skills/review-pr/SKILL.md \
  plugin/skills/review-pr/references/risk-changes-prompt.md \
  tests/skills/test_assembled_prompts.py tests/skills/test_review_pr_risk_paths.py
git commit -m "feat(skills): проверять опасные non-Python diff (PRI-134)"
```

### Task 4: Финальная проверка и документационный контракт

**Files:**

- Verify: all files from Tasks 1–3
- Modify only if required by a failing contract test.

**Interfaces:**

- Consumes committed feature branch.
- Produces verified diff ready for code review/PR.

- [ ] **Step 1: Проверить spec coverage и repository invariants**

```bash
git diff origin/dev...HEAD --check
git status --short
rg -n "risk_paths|risk_skipped_paths|risk-changes-prompt" \
  reviewer plugin/skills tests docs/superpowers
```

Expected: every new field has service, serde, public payload, prompt and test coverage;
working tree is clean before any review-only report files.

- [ ] **Step 2: Запустить full available unit suite**

```bash
/Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/pytest -q
/Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/ruff check .
```

Expected: PASS. Baseline environment currently lacks optional `fastapi`; if still
missing, install the declared `web` extra rather than changing production/test code.

- [ ] **Step 3: Провести two-stage code review**

First review the diff against this plan/spec for requirement coverage. Then review
maintainability, security/noise behavior and backward compatibility. Any finding gets
a failing regression test before a fix.

- [ ] **Step 4: Commit only review fixes**

```bash
git add reviewer/services/risk_paths.py reviewer/services/review_service.py \
  reviewer/mcp/session_serde.py reviewer/mcp/service.py \
  plugin/skills/review-pr/SKILL.md \
  plugin/skills/review-pr/references/risk-changes-prompt.md \
  tests/services/test_risk_paths.py tests/services/test_review_service.py \
  tests/mcp/test_session_serde.py tests/mcp/test_service.py \
  tests/skills/test_assembled_prompts.py tests/skills/test_review_pr_risk_paths.py
git commit -m "fix(review): уточнить risk-path pipeline (PRI-134)"
```

Skip this commit when review finds nothing.
