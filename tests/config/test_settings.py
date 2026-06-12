from reviewer.config.settings import Settings

def test_review_categories_list_parsed_from_csv(monkeypatch):
    monkeypatch.setenv("REVIEW_CATEGORIES", "security, correctness")
    s = Settings(_env_file=None)
    assert s.review_categories_list() == ["security", "correctness"]
