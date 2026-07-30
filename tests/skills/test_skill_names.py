from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = ROOT / "plugin" / "skills"


def registered_skill_files() -> tuple[Path, ...]:
    return tuple(sorted(SKILLS_ROOT.glob("*/SKILL.md")))


def frontmatter_name(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path}: нет начального YAML frontmatter"
    header = text.split("---", 2)[1]
    values = [
        line.removeprefix("name:").strip()
        for line in header.splitlines()
        if line.startswith("name:")
    ]
    assert len(values) == 1, f"{path}: ожидался ровно один frontmatter name"
    return values[0]


def registered_skill_names() -> tuple[str, ...]:
    return tuple(frontmatter_name(path) for path in registered_skill_files())


def test_frontmatter_names_match_skill_directories():
    mismatches = [
        (path.relative_to(ROOT).as_posix(), path.parent.name, frontmatter_name(path))
        for path in registered_skill_files()
        if frontmatter_name(path) != path.parent.name
    ]
    assert mismatches == [], f"path, expected, actual: {mismatches}"


def test_frontmatter_names_are_unique_and_have_no_reviewer_prefix():
    names = registered_skill_names()
    assert len(names) == len(set(names)), names
    assert [name for name in names if name.startswith("reviewer_")] == []
