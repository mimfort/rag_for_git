from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from http.client import RemoteDisconnected
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        cwd=ROOT,
        check=check,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def web_image() -> Iterator[str]:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI недоступен")
    info = _docker("info", check=False)
    if info.returncode != 0:
        pytest.skip(f"Docker daemon недоступен: {info.stderr.strip()}")

    image = f"rag-reviewer-web-smoke:{uuid.uuid4().hex}"
    _docker("build", "-f", "web/Dockerfile", "-t", image, ".")
    try:
        yield image
    finally:
        _docker("image", "rm", "--force", image, check=False)


@pytest.mark.integration
@pytest.mark.parametrize("internal_port", [18080, 18081])
def test_same_image_serves_http_on_different_internal_ports(
    web_image: str,
    internal_port: int,
) -> None:
    name = f"reviewer-web-smoke-{uuid.uuid4().hex}"
    started = _docker(
        "run",
        "--detach",
        "--rm",
        "--name",
        name,
        "--env",
        "PG_DSN=postgresql://reviewer:reviewer@127.0.0.1:1/reviewer?connect_timeout=1",
        "--env",
        f"REVIEWER_WEB_PORT={internal_port}",
        "--publish",
        f"127.0.0.1::{internal_port}",
        web_image,
    )
    container_id = started.stdout.strip()
    try:
        published = _docker("port", container_id, f"{internal_port}/tcp").stdout.strip()
        host_port = int(published.rsplit(":", 1)[1])
        url = f"http://127.0.0.1:{host_port}/"
        last_error: Exception | None = None
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                with urlopen(url, timeout=1) as response:
                    body = response.read()
                assert response.status == 200
                assert body
                break
            except (RemoteDisconnected, URLError) as exc:
                last_error = exc
                time.sleep(0.25)
        else:
            logs = _docker("logs", container_id, check=False)
            pytest.fail(
                f"web image не ответил на {url}: {last_error}; "
                f"logs:\n{logs.stdout}\n{logs.stderr}"
            )
    finally:
        _docker("rm", "--force", container_id, check=False)
