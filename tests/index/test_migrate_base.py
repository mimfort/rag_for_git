from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from reviewer.config.settings import Settings
from reviewer.index.store import ChunkRow, ChunkStore


def _row(repo, ref, path, fqn):
    return ChunkRow(
        repo=repo,
        ref=ref,
        content_hash=f"{repo}:{fqn}",
        path=path,
        lang="python",
        symbol_fqn=fqn,
        kind="function",
        start_line=1,
        end_line=2,
        text="code",
        embedding=[0.0] * 1024,
    )


def _schema_dsn(dsn: str, schema_name: str) -> str:
    params = conninfo_to_dict(dsn)
    current_options = params.get("options", "")
    params["options"] = " ".join(
        option
        for option in (current_options, f"-csearch_path={schema_name},public")
        if option
    )
    return make_conninfo(**params)


def _cleanup_public_sentinel(store: ChunkStore, repo: str) -> None:
    try:
        store.clear(repo)
    finally:
        with store._connect() as conn:
            conn.execute("DELETE FROM index_meta WHERE repo=%s", (repo,))
            conn.commit()


def _assert_public_sentinel(store: ChunkStore, repo: str) -> None:
    with psycopg.connect(store.dsn) as conn:
        chunks = conn.execute(
            "SELECT ref, path, symbol_fqn FROM chunks WHERE repo=%s",
            (repo,),
        ).fetchall()
        meta = conn.execute(
            "SELECT ref, sha FROM index_meta WHERE repo=%s",
            (repo,),
        ).fetchall()
    assert chunks == [("base", "sentinel.py", "sentinel")]
    assert meta == [("base", "sentinel-sha")]


@pytest.fixture
def migration_context():
    settings = Settings()
    schema_name = f"test_migrate_{uuid4().hex}"
    repo = f"test/migrate-base-{uuid4().hex}"
    sentinel_repo = f"test/migrate-sentinel-{uuid4().hex}"
    public_store = ChunkStore(settings.pg_dsn)
    isolated_store = None
    public_schema_ready = False
    isolated_schema_created = False
    try:
        public_store.init_schema()
        public_schema_ready = True
        _cleanup_public_sentinel(public_store, sentinel_repo)
        public_store.upsert(
            [_row(sentinel_repo, "base", "sentinel.py", "sentinel")]
        )
        public_store.set_index_meta(sentinel_repo, "base", "sentinel-sha")

        with psycopg.connect(settings.pg_dsn) as conn:
            conn.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
            )
            conn.commit()
        isolated_schema_created = True

        isolated_store = ChunkStore(_schema_dsn(settings.pg_dsn, schema_name))
        isolated_store.init_schema()
        yield isolated_store, repo, public_store, sentinel_repo
    finally:
        try:
            if isolated_store is not None:
                isolated_store.close()
        finally:
            try:
                if isolated_schema_created:
                    with psycopg.connect(settings.pg_dsn) as conn:
                        conn.execute(
                            sql.SQL("DROP SCHEMA {} CASCADE").format(
                                sql.Identifier(schema_name)
                            )
                        )
                        conn.commit()
            finally:
                try:
                    if public_schema_ready:
                        _cleanup_public_sentinel(public_store, sentinel_repo)
                finally:
                    public_store.close()


@pytest.mark.integration
def test_migrate_legacy_base_to_primary(migration_context):
    store, repo, public_store, sentinel_repo = migration_context
    store.upsert([_row(repo, "base", "a.py", "f")])
    store.set_index_meta(repo, "base", "deadbeef")
    store.migrate_legacy_base("main")
    with psycopg.connect(store.dsn) as conn:
        refs = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT ref FROM chunks WHERE repo=%s",
                (repo,),
            ).fetchall()
        }
        meta = conn.execute(
            "SELECT ref, sha FROM index_meta WHERE repo=%s",
            (repo,),
        ).fetchone()
    assert refs == {"base:main"}
    assert meta == ("base:main", "deadbeef")
    _assert_public_sentinel(public_store, sentinel_repo)


@pytest.mark.integration
def test_migrate_legacy_base_conflict_resistant(migration_context):
    """Если ветка base:main уже проиндексирована, миграция legacy 'base' не падает.

    Сценарий: пользователь сделал `reviewer index --branch main` (есть base:main),
    затем `migrate-branches`. Legacy-строка с тем же (repo, path, symbol_fqn), что
    у уже существующей target-копии, не нарушает UNIQUE — она удаляется. Legacy-чанк
    без конфликта переносится.
    """
    store, repo, public_store, sentinel_repo = migration_context
    store.upsert(
        [
            # конфликтный ключ: есть и legacy 'base', и уже существующая target 'base:main'
            _row(repo, "base", "a.py", "dup"),
            _row(repo, "base:main", "a.py", "dup"),
            # неконфликтный legacy-чанк — должен перенестись в base:main
            _row(repo, "base", "b.py", "solo"),
        ]
    )
    n = store.migrate_legacy_base("main")
    with psycopg.connect(store.dsn) as conn:
        refs = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT ref FROM chunks WHERE repo=%s",
                (repo,),
            ).fetchall()
        }
        dup_count = conn.execute(
            "SELECT count(*) FROM chunks "
            "WHERE repo=%s AND path='a.py' AND symbol_fqn='dup'",
            (repo,),
        ).fetchone()[0]
        solo = conn.execute(
            "SELECT ref FROM chunks "
            "WHERE repo=%s AND path='b.py' AND symbol_fqn='solo'",
            (repo,),
        ).fetchone()
    # (а) не упало; (б) legacy 'base' исчез
    assert refs == {"base:main"}
    # (в) для конфликтного ключа осталась ровно одна строка
    assert dup_count == 1
    # (г) неконфликтный legacy-чанк перенесён
    assert solo == ("base:main",)
    # rowcount = только фактически перенесённый неконфликтный чанк
    assert n == 1
    _assert_public_sentinel(public_store, sentinel_repo)
