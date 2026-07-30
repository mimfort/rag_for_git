from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = ROOT / "plugin" / "skills"
TEXT_SUFFIXES = {".md", ".py", ".json", ".toml", ".yml", ".yaml"}
ACTIVE_DOCS = (
    ROOT / "README.md",
    ROOT / "README.ru.md",
    ROOT / "CLAUDE.md",
    ROOT / "AGENTS.md",
    ROOT / "plugin" / "README.md",
)


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


def _legacy_skill_names() -> tuple[str, ...]:
    return tuple(f"reviewer_{path.parent.name}" for path in registered_skill_files())


def _text_files(root: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix in TEXT_SUFFIXES
            and "__pycache__" not in path.parts
        )
    )


def _legacy_offenders(paths: tuple[Path, ...]) -> list[str]:
    offenders: list[str] = []
    legacy = _legacy_skill_names()
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for name in legacy:
            if name in text:
                offenders.append(f"{path.relative_to(ROOT)}: {name}")
    return offenders


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


def test_active_code_and_plugin_surfaces_have_no_legacy_skill_names():
    paths = (
        *(path for path in _text_files(ROOT / "plugin") if path != ROOT / "plugin" / "README.md"),
        *_text_files(ROOT / "reviewer"),
        *_text_files(ROOT / "tests"),
    )
    assert _legacy_offenders(paths) == []


def test_active_docs_have_no_legacy_skill_names():
    assert _legacy_offenders(ACTIVE_DOCS) == []


def test_bilingual_readmes_list_every_canonical_skill():
    for readme in (ROOT / "README.md", ROOT / "README.ru.md"):
        text = readme.read_text(encoding="utf-8")
        missing = [
            name
            for name in registered_skill_names()
            if f"/rag-reviewer:{name}" not in text
        ]
        assert missing == [], f"{readme.name}: {missing}"


def test_migration_docs_require_fresh_sessions():
    for readme in ACTIVE_DOCS:
        text = readme.read_text(encoding="utf-8")
        assert "rag-reviewer:" in text
    combined = "\n".join(path.read_text(encoding="utf-8") for path in ACTIVE_DOCS)
    assert "New Chat" in combined
    assert "new CLI session" in combined
    assert "Reload Window" in combined
