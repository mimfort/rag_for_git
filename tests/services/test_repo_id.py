import pytest
from reviewer.services.repo_id import (
    normalize_repo,
    derive_repo_from_remote,
    derive_vcs_from_remote,
    resolve_repo_id,
)


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


# ---------------------------------------------------------------------------
# resolve_repo_id: repo + происхождение имени (PRI: issue #190)
# ---------------------------------------------------------------------------


def test_resolve_repo_id_prefers_explicit_option():
    """Явный --repo побеждает origin и env, источник — 'cli'."""
    res = resolve_repo_id("Owner/Explicit", "git@github.com:other/name.git", "env/repo")
    assert (res.repo, res.source) == ("owner/explicit", "cli")


def test_resolve_repo_id_derives_from_origin():
    """Без --repo имя выводится из origin, источник — 'git:origin'."""
    res = resolve_repo_id(None, "git@github.com:Owner/Name.git", "env/repo")
    assert (res.repo, res.source) == ("owner/name", "git:origin")


@pytest.mark.parametrize("remote", ["", None, "ssh://tunnel/blocked", "https://gitlab.com/a/b.git"])
def test_resolve_repo_id_falls_back_to_default_repo(remote):
    """Нераспознанный или отсутствующий origin → подстановка из env, источник виден."""
    res = resolve_repo_id(None, remote, "Env/Repo")
    assert (res.repo, res.source) == ("env/repo", "env:DEFAULT_REPO")


def test_resolve_repo_id_returns_none_when_nothing_resolves():
    """Нечем резолвить (нет опции, origin и env) → None, решение принимает вызывающий."""
    assert resolve_repo_id(None, None, "") is None
