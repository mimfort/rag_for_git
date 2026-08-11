"""Путь к локальному клону репо в индексе (PRI-235)."""
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


def test_get_repo_clone_absent_returns_none(store):
    assert store.get_repo_clone("nobody/none-xyz") is None


def test_set_then_get_repo_clone(store):
    store.set_repo_clone("o/r-test235", "/srv/clones/o/r")
    assert store.get_repo_clone("o/r-test235") == "/srv/clones/o/r"


def test_set_repo_clone_upserts(store):
    store.set_repo_clone("o/r-test235", "/srv/clones/o/r")
    store.set_repo_clone("o/r-test235", "/home/dev/r")
    assert store.get_repo_clone("o/r-test235") == "/home/dev/r"
