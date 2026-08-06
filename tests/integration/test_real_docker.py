import shutil
import subprocess

import pytest


docker_available = bool(
    shutil.which("docker")
    and subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0
)


@pytest.mark.skipif(not docker_available, reason="Docker daemon unavailable")
def test_real_health_and_container_listing(tmp_path):
    from starlette.testclient import TestClient

    from docker_manage_server.api import create_app
    from docker_manage_server.config import Settings

    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        assert client.get("/api/health").status_code == 200
        assert isinstance(client.get("/api/containers").json()["items"], list)
