from pathlib import Path


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
