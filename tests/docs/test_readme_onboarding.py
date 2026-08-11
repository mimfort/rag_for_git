import re
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[2]
EN = ROOT / "README.md"
RU = ROOT / "README.ru.md"

QUICK_START_PAIRS = (
    ("## Start here", "## Начните здесь"),
    ("## Try reviewer", "## Попробовать reviewer"),
    ("## Deploy for a team", "## Развёртывание для команды"),
)

CONTENT_PAIRS = QUICK_START_PAIRS + (
    ("## Core workflows", "## Основные сценарии"),
    ("## How it works", "## Как это работает"),
    ("## Installation and configuration", "## Установка и конфигурация"),
    ("## CLI reference", "## Справочник CLI"),
    ("## Skills reference", "## Справочник skills"),
)

SECTION_PAIRS = CONTENT_PAIRS + (
    (
        "## Operations, troubleshooting, and limitations",
        "## Эксплуатация, диагностика и ограничения",
    ),
    ("## Development", "## Разработка"),
    ("## License", "## Лицензия"),
)

PARITY_MARKERS = (
    "uv tool install rag-reviewer",
    "docker compose -f ~/.config/rag-reviewer/docker-compose.yml up -d",
    "reviewer init",
    "reviewer install",
    "reviewer update",
    "reviewer check",
    "reviewer index",
    "reviewer status",
    "reviewer serve",
    "REVIEW_BRANCHES",
    "docs/board-providers.md",
    "provider_options",
    "sync_board",
    "get_task(key, project=...)",
    "store-first",
    "tasks:<type>:<board>",
    "sync_filter",
    "max_age_days",
    "include_archived",
    "search_codebase",
    "callers",
    "drift == 0",
)

LINK_RE = re.compile(r"(?<!!)\[[^]]*\]\(([^)]*)\)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _assert_in_order(text: str, headings: tuple[str, ...]) -> None:
    positions = [text.index(heading) for heading in headings]
    assert positions == sorted(positions)


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:] if end == -1 else text[start:end]


def _registered_skills() -> tuple[str, ...]:
    skills_root = ROOT / "plugin" / "skills"
    return tuple(
        sorted(
            path.name
            for path in skills_root.iterdir()
            if path.is_dir() and (path / "SKILL.md").is_file()
        )
    )


def _link_targets(text: str) -> tuple[str, ...]:
    targets = []
    for raw in LINK_RE.findall(text):
        raw = raw.strip()
        if raw.startswith("<") and ">" in raw:
            target = raw[1 : raw.index(">")]
        else:
            target = raw.split(maxsplit=1)[0]
        targets.append(target)
    return tuple(targets)


def _base_slug(heading: str) -> str:
    heading = re.sub(r"<[^>]+>", "", heading)
    heading = heading.replace("`", "").replace("*", "")
    lowered = heading.casefold()
    kept = "".join(ch for ch in lowered if ch.isalnum() or ch in " _-")
    return re.sub(r"\s+", "-", kept.strip())


def _heading_anchors(text: str) -> set[str]:
    counts: Counter[str] = Counter()
    anchors = set()
    for heading in HEADING_RE.findall(text):
        base = _base_slug(heading)
        suffix = counts[base]
        counts[base] += 1
        anchors.add(base if suffix == 0 else f"{base}-{suffix}")
    return anchors


def _assert_links_resolve(source: Path) -> None:
    for target in _link_targets(source.read_text(encoding="utf-8")):
        parsed = urlsplit(target)
        if parsed.scheme:
            assert parsed.scheme in {"http", "https"}, (source.name, target)
            assert parsed.netloc, (source.name, target)
            continue

        path_text = unquote(parsed.path)
        destination = source if not path_text else source.parent / path_text
        destination = destination.resolve()
        assert destination.is_relative_to(ROOT), (source.name, target)
        assert destination.exists(), (source.name, target)

        if parsed.fragment and destination.suffix.lower() == ".md":
            anchors = _heading_anchors(destination.read_text(encoding="utf-8"))
            assert unquote(parsed.fragment).casefold() in anchors, (source.name, target)


def test_readmes_link_to_each_other_near_the_top():
    english = _read("README.md")[:600]
    russian = _read("README.ru.md")[:600]

    assert "[Русский](README.ru.md)" in english
    assert "[English](README.md)" in russian


def test_readmes_start_with_matching_dual_track_routes():
    english = _read("README.md")
    russian = _read("README.ru.md")

    _assert_in_order(english, tuple(pair[0] for pair in QUICK_START_PAIRS))
    _assert_in_order(russian, tuple(pair[1] for pair in QUICK_START_PAIRS))


def test_readmes_share_content_section_order():
    english = _read("README.md")
    russian = _read("README.ru.md")

    _assert_in_order(english, tuple(pair[0] for pair in CONTENT_PAIRS))
    _assert_in_order(russian, tuple(pair[1] for pair in CONTENT_PAIRS))


def test_each_registered_skill_has_its_own_heading_in_both_readmes():
    english_headings = {
        line for line in _read("README.md").splitlines() if line.startswith("### ")
    }
    russian_headings = {
        line for line in _read("README.ru.md").splitlines() if line.startswith("### ")
    }

    for skill in _registered_skills():
        marker = f"### `{skill}`"
        assert any(marker in heading for heading in english_headings), marker
        assert any(marker in heading for heading in russian_headings), marker


def test_readmes_share_the_complete_section_order():
    english = _read("README.md")
    russian = _read("README.ru.md")

    _assert_in_order(english, tuple(pair[0] for pair in SECTION_PAIRS))
    _assert_in_order(russian, tuple(pair[1] for pair in SECTION_PAIRS))


def test_readmes_share_critical_commands_and_contract_markers():
    english = _read("README.md")
    russian = _read("README.ru.md")

    for marker in PARITY_MARKERS:
        assert marker in english, ("README.md", marker)
        assert marker in russian, ("README.ru.md", marker)


def test_quick_start_uses_unified_update_and_indexes_before_checking():
    for filename, heading in (
        ("README.md", "## Try reviewer"),
        ("README.ru.md", "## Попробовать reviewer"),
    ):
        section = _section(_read(filename), heading)
        assert "curl -o ~/.config/rag-reviewer/docker-compose.yml" not in section
        _assert_in_order(
            section,
            (
                "uv tool install rag-reviewer",
                "reviewer update",
                "docker compose -f ~/.config/rag-reviewer/docker-compose.yml up -d",
                "reviewer init",
                "reviewer index /path/to/repo --ref main",
                "reviewer check",
                "reviewer status /path/to/repo --branch main --json",
            ),
        )


def test_readmes_document_unified_update_contract():
    markers = (
        "uvx --refresh --from rag-reviewer@latest reviewer update --upgrade-tool",
        ".reviewer-update.json",
        "New Chat",
        "Reload Window",
    )
    for filename in ("README.md", "README.ru.md"):
        text = _read(filename)
        for marker in markers:
            assert marker in text, (filename, marker)


def test_team_route_describes_per_client_stdio_processes():
    for filename, heading in (
        ("README.md", "## Deploy for a team"),
        ("README.ru.md", "## Развёртывание для команды"),
    ):
        section = _section(_read(filename), heading)
        assert "stdio" in section
        assert "reviewer-mcp" in section
        assert "PostgreSQL/ParadeDB" in section
        assert "shared host" in section
        assert "service account" in section
        assert "127.0.0.1" in section


def test_security_discloses_both_external_code_data_paths():
    for filename in ("README.md", "README.ru.md"):
        section = _section(_read(filename), "### Security" if filename == "README.md" else "### Безопасность")
        assert "Voyage" in section
        assert "AI model provider" in section


def test_readmes_document_web_container_runtime_ports() -> None:
    for filename, heading in (
        ("README.md", "### Web admin"),
        ("README.ru.md", "### Web admin"),
    ):
        section = _section(_read(filename), heading)
        for marker in (
            "docker build -f web/Dockerfile -t rag-reviewer-web .",
            "docker run --rm",
            "REVIEWER_WEB_PORT=8080",
            "127.0.0.1:18000:8080",
            "docker compose --profile web up -d web",
            "REVIEWER_WEB_PUBLISH_PORT=18000",
        ):
            assert marker in section, (filename, marker)


def test_readmes_document_storage_publish_ports() -> None:
    for filename, heading in (
        ("README.md", "### Required services and credentials"),
        ("README.ru.md", "### Сервисы и credentials"),
    ):
        section = _section(_read(filename), heading)
        for marker in (
            "PARADEDB_PUBLISH_PORT",
            "NEO4J_BOLT_PUBLISH_PORT",
            "NEO4J_HTTP_PUBLISH_PORT",
            "preserved",
        ):
            assert marker in section, (filename, marker)


def test_readmes_document_configuration_ownership_and_scenarios():
    cases = (
        (
            "README.md",
            "### Configuration ownership",
            ("Single repository", "Second repository", "CI / server"),
        ),
        (
            "README.ru.md",
            "### Владение конфигурацией",
            ("Один репозиторий", "Второй репозиторий", "CI / server"),
        ),
    )
    for filename, heading, scenarios in cases:
        section = _section(_read(filename), heading)
        for marker in (
            "global `.env`",
            "home global YAML",
            "home per-repo YAML",
            "committed `.review.yml`",
            "git remote / CLI",
            "Postgres / Neo4j",
            "reviewer init --scope repo",
            "reviewer config show --repo",
        ):
            assert marker in section, (filename, marker)
        for scenario in scenarios:
            assert scenario in section, (filename, scenario)


def test_readmes_document_vcs_token_matrix_and_check_semantics():
    for filename in ("README.md", "README.ru.md"):
        text = _read(filename)
        section = _section(text, "### VCS credentials")
        for marker in (
            "`GITHUB_TOKEN`",
            "Pull requests: Read and write",
            "Contents: Read",
            "`GITLAB_TOKEN`",
            "`api` scope",
            "`/user`",
            "`/api/v4/user`",
            "identity",
            "repository permissions",
        ):
            assert marker in section, (filename, marker)
        assert "GitLab-only" not in text
        assert "dry-run `/rag-reviewer:review-pr`" not in text


def test_team_route_validates_the_selected_board_project():
    for filename, heading in (
        ("README.md", "## Deploy for a team"),
        ("README.ru.md", "## Развёртывание для команды"),
    ):
        section = _section(_read(filename), heading)
        assert "reviewer check --board-project TYPE=PROJECT" in section


def test_readmes_document_task_retention_policy_and_repo_mode():
    cases = (
        (
            "README.md",
            "### Task boards",
            "### `sync-tasks`",
            (
                "inclusive cutoff",
                "unknown age is not filtered by age",
                "only while `include_archived: false`",
                "unknown archive does not itself exclude the row",
                "archive warning is emitted only then",
                "age filtering runs first and may still exclude the row",
                "same `task_board.project` share one task corpus",
                "purge is explicit",
                "next successful full sync",
            ),
        ),
        (
            "README.ru.md",
            "### Доски задач",
            "### `sync-tasks`",
            (
                "inclusive cutoff",
                "unknown age не фильтруется по возрасту",
                "только при `include_archived: false`",
                "unknown archive сам по себе не исключает строку",
                "archive warning появляется только тогда",
                "age filtering выполняется первым и всё ещё может исключить строку",
                "одинаковым `task_board.project` используют один task corpus",
                "purge включается явно",
                "следующем успешном полном sync",
            ),
        ),
    )

    for filename, board_heading, sync_heading, prose in cases:
        text = _read(filename)
        board = _section(text, board_heading)
        sync = _section(text, sync_heading)
        normalized_board = " ".join(board.split()).casefold()
        for marker in (
            "sync_filter:",
            "max_age_days: 180",
            "include_archived: false",
            "include_archived: true",
        ):
            assert marker in normalized_board, (filename, marker)
        for marker in prose:
            assert marker in normalized_board, (filename, marker)
        assert "repo mode" in sync
        for field in (
            "eligible",
            "filtered_by_age",
            "filtered_archived",
            "age_unknown",
            "archive_unknown",
            "filter_applied",
            "filter_fingerprint",
            "filter_source",
            "by_board",
            "purge",
            "warnings",
        ):
            assert field in sync, (filename, field)


def test_all_readme_links_and_local_anchors_resolve():
    _assert_links_resolve(EN)
    _assert_links_resolve(RU)
