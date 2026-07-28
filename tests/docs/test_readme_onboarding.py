from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EN = ROOT / "README.md"
RU = ROOT / "README.ru.md"

QUICK_START_PAIRS = (
    ("## Start here", "## Начните здесь"),
    ("## Try reviewer", "## Попробовать reviewer"),
    ("## Deploy for a team", "## Развёртывание для команды"),
)


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _assert_in_order(text: str, headings: tuple[str, ...]) -> None:
    positions = [text.index(heading) for heading in headings]
    assert positions == sorted(positions)


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
