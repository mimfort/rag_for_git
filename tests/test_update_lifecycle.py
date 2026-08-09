import hashlib
import json
from contextlib import nullcontext
from types import SimpleNamespace

from reviewer.update_lifecycle import (
    download_compose,
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


def test_run_fresh_artifact_refresh_invokes_reviewer_hidden_phase():
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="compose current\n", stderr="")

    result = run_fresh_artifact_refresh(
        which=lambda name: "/tools/reviewer" if name == "reviewer" else None,
        run=run,
    )

    assert calls == [
        (
            ["/tools/reviewer", "update", "--refresh-artifacts"],
            {"capture_output": True, "text": True},
        )
    ]
    assert result.returncode == 0
    assert result.stdout == "compose current\n"


def test_run_fresh_artifact_refresh_reports_missing_executable():
    result = run_fresh_artifact_refresh(which=lambda name: None)

    assert result.returncode == 127
    assert "reviewer не найден" in result.stderr
