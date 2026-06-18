from pathlib import Path

DOC = Path(__file__).resolve().parents[2] / ".kimi-code" / "INSTALL.md"


def test_install_doc_mentions_install_command_for_skills():
    text = DOC.read_text(encoding="utf-8")
    # быстрый путь ставит и скилы; обновление = повторный запуск
    assert "reviewer install kimi" in text
    assert "обновлен" in text.lower()  # есть явная пометка про обновление
