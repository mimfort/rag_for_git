from reviewer.tasks.store import build_task_text, task_content_hash


def test_build_task_text_joins_title_description_criteria():
    text = build_task_text("Login", "Add logout", ["clears session", "redirects"])
    assert "Login" in text and "Add logout" in text
    assert "clears session" in text and "redirects" in text


def test_build_task_text_skips_empty_parts():
    assert build_task_text("Только заголовок", "", None) == "Только заголовок"


def test_build_task_text_empty_criteria_like_none():
    assert build_task_text("T", "D", []) == build_task_text("T", "D", None)


def test_content_hash_stable_and_normalized():
    # хвостовые пробелы не должны менять хэш (как Chunk.content_hash)
    a = task_content_hash("line one  \nline two")
    b = task_content_hash("line one\nline two")
    assert a == b


def test_content_hash_changes_on_real_change():
    assert task_content_hash("a") != task_content_hash("b")
