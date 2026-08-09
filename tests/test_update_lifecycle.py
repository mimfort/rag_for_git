import hashlib
import json
import threading
from contextlib import nullcontext
from contextlib import contextmanager
from types import SimpleNamespace

import reviewer.update_lifecycle as lifecycle
from reviewer.update_lifecycle import (
    download_compose,
    find_uv_tool_python,
    run_fresh_artifact_refresh,
    sync_compose_file,
)


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def test_sync_compose_creates_missing_target_and_state(tmp_path):
    content = b"services:\n  db: {}\n"

    result = sync_compose_file(content, config_dir=tmp_path)

    assert result.action == "created"
    assert result.path.read_bytes() == content
    state = json.loads((tmp_path / ".reviewer-update.json").read_text())
    assert state == {"docker_compose_sha256": _digest(content)}


def test_sync_compose_adopts_exact_unmanaged_download(tmp_path):
    content = b"services:\n  db: {}\n"
    (tmp_path / "docker-compose.yml").write_bytes(content)

    result = sync_compose_file(content, config_dir=tmp_path)

    assert result.action == "adopted"
    assert json.loads((tmp_path / ".reviewer-update.json").read_text()) == {
        "docker_compose_sha256": _digest(content)
    }


def test_sync_compose_reports_current_managed_file(tmp_path):
    content = b"services:\n  db: {}\n"
    sync_compose_file(content, config_dir=tmp_path)

    result = sync_compose_file(content, config_dir=tmp_path)

    assert result.action == "current"


def test_sync_compose_updates_only_file_matching_recorded_hash(tmp_path):
    old = b"services:\n  db: {image: old}\n"
    new = b"services:\n  db: {image: new}\n"
    sync_compose_file(old, config_dir=tmp_path)

    result = sync_compose_file(new, config_dir=tmp_path)

    assert result.action == "updated"
    assert result.path.read_bytes() == new


def test_sync_compose_preserves_modified_managed_file_and_old_state(tmp_path):
    old = b"services:\n  db: {image: old}\n"
    new = b"services:\n  db: {image: new}\n"
    sync_compose_file(old, config_dir=tmp_path)
    target = tmp_path / "docker-compose.yml"
    target.write_bytes(b"services:\n  db: {ports: [custom]}\n")
    state_before = (tmp_path / ".reviewer-update.json").read_bytes()

    result = sync_compose_file(new, config_dir=tmp_path)

    assert result.action == "preserved"
    assert target.read_bytes() == b"services:\n  db: {ports: [custom]}\n"
    assert (tmp_path / ".reviewer-update.json").read_bytes() == state_before


def test_sync_compose_preserves_unmanaged_nonmatching_file(tmp_path):
    target = tmp_path / "docker-compose.yml"
    target.write_bytes(b"custom\n")

    result = sync_compose_file(b"canonical\n", config_dir=tmp_path)

    assert result.action == "preserved"
    assert target.read_bytes() == b"custom\n"
    assert not (tmp_path / ".reviewer-update.json").exists()


def test_download_compose_uses_canonical_url_and_no_cache_headers():
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["cache"] = request.headers["Cache-control"]
        seen["timeout"] = timeout
        return nullcontext(SimpleNamespace(read=lambda: b"services: {}\n"))

    assert download_compose(opener=opener, timeout=7) == b"services: {}\n"
    assert seen == {
        "url": (
            "https://raw.githubusercontent.com/mimfort/rag_for_git/"
            "main/docker-compose.yml"
        ),
        "cache": "no-cache, no-store",
        "timeout": 7,
    }


def test_run_fresh_artifact_refresh_uses_same_python_environment():
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="compose current\n", stderr="")

    result = run_fresh_artifact_refresh(
        python_executable="/tools/rag-reviewer/bin/python",
        run=run,
    )

    assert calls == [
        (
            [
                "/tools/rag-reviewer/bin/python",
                "-I",
                "-c",
                "from reviewer.entrypoints.launcher import main; main()",
                "update",
                "--refresh-artifacts",
            ],
            {"capture_output": True, "text": True},
        )
    ]
    assert result.returncode == 0
    assert result.stdout == "compose current\n"


def test_run_fresh_artifact_refresh_reports_missing_executable():
    result = run_fresh_artifact_refresh(python_executable="")

    assert result.returncode == 127
    assert "Python executable не найден" in result.stderr


def test_sync_compose_preserves_symlink_even_when_content_matches(tmp_path):
    source = tmp_path / "custom-compose.yml"
    content = b"services:\n  db: {}\n"
    source.write_bytes(content)
    target = tmp_path / "docker-compose.yml"
    target.symlink_to(source)

    result = sync_compose_file(content, config_dir=tmp_path)

    assert result.action == "preserved"
    assert target.is_symlink()
    assert source.read_bytes() == content
    assert not (tmp_path / ".reviewer-update.json").exists()


def test_sync_compose_preserves_edit_that_races_managed_update(monkeypatch, tmp_path):
    old = b"services:\n  db: {image: old}\n"
    new = b"services:\n  db: {image: new}\n"
    custom = b"services:\n  db: {ports: [custom]}\n"
    sync_compose_file(old, config_dir=tmp_path)
    target = tmp_path / "docker-compose.yml"
    original_read = lifecycle._read_recorded_hash

    def read_then_edit(path):
        recorded = original_read(path)
        target.write_bytes(custom)
        return recorded

    monkeypatch.setattr(lifecycle, "_read_recorded_hash", read_then_edit)

    result = sync_compose_file(new, config_dir=tmp_path)

    assert result.action == "preserved"
    assert target.read_bytes() == custom


def test_find_uv_tool_python_resolves_persistent_environment(tmp_path):
    tools = tmp_path / "uv-tools"
    python = tools / "rag-reviewer" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=f"{tools}\n")

    result = find_uv_tool_python("/opt/uv", run=run, system="Linux")

    assert result == str(python)
    assert calls == [
        (["/opt/uv", "tool", "dir"], {"capture_output": True, "text": True})
    ]


def test_find_uv_tool_python_returns_none_for_missing_environment(tmp_path):
    result = find_uv_tool_python(
        "/opt/uv",
        run=lambda argv, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=f"{tmp_path / 'uv-tools'}\n",
        ),
        system="Windows",
    )

    assert result is None


def test_update_lock_serializes_parallel_syncs(tmp_path):
    acquired = threading.Event()

    def contender():
        with lifecycle._update_lock(tmp_path):
            acquired.set()

    with lifecycle._update_lock(tmp_path):
        thread = threading.Thread(target=contender)
        thread.start()
        assert acquired.wait(0.1) is False

    assert acquired.wait(1) is True
    thread.join()


def test_sync_compose_holds_update_lock(monkeypatch, tmp_path):
    entered = []

    @contextmanager
    def lock(directory):
        entered.append(directory)
        yield

    monkeypatch.setattr(lifecycle, "_update_lock", lock)

    sync_compose_file(b"services: {}\n", config_dir=tmp_path)

    assert entered == [tmp_path]


def test_sync_compose_preserves_file_created_during_missing_target_race(monkeypatch, tmp_path):
    target = tmp_path / "docker-compose.yml"
    custom = b"custom\n"

    def collide(path, content):
        path.write_bytes(custom)
        return False

    monkeypatch.setattr(lifecycle, "_atomic_create", collide, raising=False)

    result = sync_compose_file(b"canonical\n", config_dir=tmp_path)

    assert result.action == "preserved"
    assert target.read_bytes() == custom
    assert not (tmp_path / ".reviewer-update.json").exists()
