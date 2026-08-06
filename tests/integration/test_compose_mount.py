import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


REPO_ROOT = Path(__file__).parents[2]
docker_available = bool(
    shutil.which("docker")
    and subprocess.run(
        ["docker", "info"], capture_output=True, check=False
    ).returncode
    == 0
)


@pytest.mark.skipif(not docker_available, reason="Docker daemon unavailable")
def test_compose_uses_one_absolute_data_path_for_host_and_server_container(tmp_path):
    data_dir = (tmp_path / "docker-manage-data").resolve()
    environment = os.environ.copy()
    environment["DOCKER_MANAGE_DATA_DIR"] = str(data_dir)
    environment["DOCKER_MANAGE_SERVER_PORT"] = "6308"

    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    server = config["services"]["server"]
    assert server["environment"]["DATA_DIR"] == str(data_dir)
    data_mounts = [
        mount
        for mount in server["volumes"]
        if mount.get("target") == str(data_dir)
    ]
    assert data_mounts == [
        {"type": "bind", "source": str(data_dir), "target": str(data_dir)}
    ]
    assert server["ports"] == [
        {
            "mode": "ingress",
            "protocol": "tcp",
            "published": "6308",
            "target": 8000,
        }
    ]
