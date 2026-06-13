import pytest
from reviewer.services.repo_id import normalize_repo, derive_repo_from_remote


@pytest.mark.parametrize("raw,expected", [
    ("Owner/Repo", "owner/repo"),
    ("  OWNER/Name  ", "owner/name"),
    ("owner/name", "owner/name"),
])
def test_normalize_repo(raw, expected):
    assert normalize_repo(raw) == expected


@pytest.mark.parametrize("bad", ["", "noslash", "a/b/c"])
def test_normalize_repo_rejects_bad(bad):
    with pytest.raises(ValueError):
        normalize_repo(bad)


@pytest.mark.parametrize("url,expected", [
    ("git@github.com:Owner/Repo.git", "owner/repo"),
    ("https://github.com/Owner/Repo.git", "owner/repo"),
    ("https://github.com/owner/name", "owner/name"),
    ("ssh://git@github.com/owner/name.git", "owner/name"),
])
def test_derive_from_remote(url, expected):
    assert derive_repo_from_remote(url) == expected


@pytest.mark.parametrize("url", ["", "https://gitlab.com/a/b.git", "not a url"])
def test_derive_from_remote_none(url):
    assert derive_repo_from_remote(url) is None
