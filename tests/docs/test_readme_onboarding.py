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
    "uv tool install --from rag-reviewer reviewer",
    "docker compose up -d",
    "reviewer init",
    "reviewer install",
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
        marker = f"reviewer_{skill}"
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


def test_all_readme_links_and_local_anchors_resolve():
    _assert_links_resolve(EN)
    _assert_links_resolve(RU)
