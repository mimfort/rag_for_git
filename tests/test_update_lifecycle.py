import hashlib
import json

from reviewer.update_lifecycle import sync_compose_file


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
