import pytest
from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore

pytestmark = pytest.mark.integration


@pytest.fixture
def store():
    s = Settings()
    st = ChunkStore(s.pg_dsn)
    st.init_schema()
    return st


def test_get_repo_vcs_absent_returns_none(store):
    assert store.get_repo_vcs("nobody/none-xyz") is None


def test_set_then_get_repo_vcs(store):
    store.set_repo_vcs("o/r-test133", "gitlab", "https://gitlab.acme.com")
    assert store.get_repo_vcs("o/r-test133") == ("gitlab", "https://gitlab.acme.com")


def test_set_repo_vcs_upserts(store):
    store.set_repo_vcs("o/r-test133", "github", "")
    assert store.get_repo_vcs("o/r-test133") == ("github", "")
