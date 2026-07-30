import pytest
from reviewer.services.repo_id import normalize_repo, derive_repo_from_remote, derive_vcs_from_remote


@pytest.mark.parametrize("raw,expected", [
    ("Owner/Repo", "owner/repo"),
    ("Group/Sub/Repo", "group/sub/repo"),
])
def test_normalize_repo(raw, expected):
    assert normalize_repo(raw) == expected


@pytest.mark.parametrize(
    "bad",
    ["", "noslash", "/a/b", "a/b/", "a/../b", "a/./b", "a\\b/c", "a/\x00/b"],
)
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


@pytest.mark.parametrize("url, expected", [
    ("git@github.com:o/r.git", ("github", "")),
    ("https://github.com/o/r.git", ("github", "")),
    ("https://gitlab.com/o/r.git", ("gitlab", "https://gitlab.com")),
    ("git@gitlab.acme.com:grp/r.git", ("gitlab", "https://gitlab.acme.com")),
    ("https://gitlab.acme.com/grp/sub/r.git", ("gitlab", "https://gitlab.acme.com")),
    ("https://bitbucket.org/o/r.git", None),
    ("", None),
    ("not a url", None),
])
def test_derive_vcs_from_remote(url, expected):
    assert derive_vcs_from_remote(url) == expected
