from reviewer.config.settings import Settings


def test_review_branches_default_is_main(monkeypatch):
    monkeypatch.delenv("REVIEW_BRANCHES", raising=False)
    s = Settings(_env_file=None)
    assert s.review_branches_list() == ["main"]
    assert s.primary_branch() == "main"


def test_review_branches_parsed_from_csv(monkeypatch):
    monkeypatch.setenv("REVIEW_BRANCHES", "main, master , release/v1")
    s = Settings(_env_file=None)
    assert s.review_branches_list() == ["main", "master", "release/v1"]
    assert s.primary_branch() == "main"


def test_review_branches_empty_falls_back_to_main(monkeypatch):
    monkeypatch.setenv("REVIEW_BRANCHES", "   ")
    s = Settings(_env_file=None)
    assert s.review_branches_list() == ["main"]
