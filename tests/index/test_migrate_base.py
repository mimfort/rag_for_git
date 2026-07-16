import psycopg
import pytest

from reviewer.config.settings import Settings
from reviewer.index.store import ChunkRow, ChunkStore


_REPO = "test/migrate-base"


def _row(ref, path, fqn):
    return ChunkRow(
        repo=_REPO,
        ref=ref,
        content_hash=fqn,
        path=path,
        lang="python",
        symbol_fqn=fqn,
        kind="function",
        start_line=1,
        end_line=2,
        text="code",
        embedding=[0.0] * 1024,
    )


def _cleanup(store):
    store.clear(_REPO)
    with store._connect() as conn:
        conn.execute("DELETE FROM index_meta WHERE repo=%s", (_REPO,))
        conn.commit()


@pytest.fixture
def store():
    settings = Settings()
    chunk_store = ChunkStore(settings.pg_dsn)
    try:
        chunk_store.init_schema()
        _cleanup(chunk_store)
        yield chunk_store
    finally:
        try:
            _cleanup(chunk_store)
        finally:
            chunk_store.close()


@pytest.mark.integration
def test_migrate_legacy_base_to_primary(store):
    store.upsert([_row("base", "a.py", "f")])
    store.set_index_meta(_REPO, "base", "deadbeef")
    store.migrate_legacy_base("main")
    with psycopg.connect(store.dsn) as conn:
        refs = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT ref FROM chunks WHERE repo=%s",
                (_REPO,),
            ).fetchall()
        }
        meta = conn.execute(
            "SELECT ref, sha FROM index_meta WHERE repo=%s",
            (_REPO,),
        ).fetchone()
    assert refs == {"base:main"}
    assert meta == ("base:main", "deadbeef")


@pytest.mark.integration
def test_migrate_legacy_base_conflict_resistant(store):
    """Если ветка base:main уже проиндексирована, миграция legacy 'base' не падает.

    Сценарий: пользователь сделал `reviewer index --branch main` (есть base:main),
    затем `migrate-branches`. Legacy-строка с тем же (repo, path, symbol_fqn), что
    у уже существующей target-копии, не нарушает UNIQUE — она удаляется. Legacy-чанк
    без конфликта переносится.
    """
    store.upsert(
        [
            # конфликтный ключ: есть и legacy 'base', и уже существующая target 'base:main'
            _row("base", "a.py", "dup"),
            _row("base:main", "a.py", "dup"),
            # неконфликтный legacy-чанк — должен перенестись в base:main
            _row("base", "b.py", "solo"),
        ]
    )
    n = store.migrate_legacy_base("main")
    with psycopg.connect(store.dsn) as conn:
        refs = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT ref FROM chunks WHERE repo=%s",
                (_REPO,),
            ).fetchall()
        }
        dup_count = conn.execute(
            "SELECT count(*) FROM chunks "
            "WHERE repo=%s AND path='a.py' AND symbol_fqn='dup'",
            (_REPO,),
        ).fetchone()[0]
        solo = conn.execute(
            "SELECT ref FROM chunks "
            "WHERE repo=%s AND path='b.py' AND symbol_fqn='solo'",
            (_REPO,),
        ).fetchone()
    # (а) не упало; (б) legacy 'base' исчез
    assert refs == {"base:main"}
    # (в) для конфликтного ключа осталась ровно одна строка
    assert dup_count == 1
    # (г) неконфликтный legacy-чанк перенесён
    assert solo == ("base:main",)
    # rowcount = только фактически перенесённый неконфликтный чанк
    assert n == 1
